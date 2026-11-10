"""Optional enrichment via Apollo.io.

Entirely opt-in: with no APOLLO_API_KEY every call returns None and the rest of
the framework runs unchanged. Set APOLLO_API_KEY to enable.

Apollo's shape drives the design here:

* People Search is free but returns **masked last names** ("Do***e") and no
  addresses, so it can only be used for discovery.
* People Enrichment returns the real name and address but **costs credits**.

So we search first, gate on role relevance using our own scorer, and only spend
credits on people worth emailing. Everything goes through bulk_match (batches of
10) rather than the single-person endpoint -- one code path, fewer calls.

Unlike some providers, Apollo does not report a company's address format. We get
it anyway: a *verified* address plus a real name reverse-maps through
patterns.pattern_of(), which is what locks the company format.

Uses stdlib urllib so there is no dependency.
"""
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .score import title_relevance

API = "https://api.apollo.io/api/v1"
SEARCH_PATH = "mixed_people/api_search"   # /search 403s on lower plans
ENRICH_PATH = "people/bulk_match"
TIMEOUT = 30
BATCH_LIMIT = 10          # bulk_match hard limit
MAX_PER_PAGE = 100        # search hard limit

# person_titles is required by the API. These cover the roles that own intern
# requisitions; override per run if a company uses unusual wording.
DEFAULT_TITLES = [
    "university recruiter", "campus recruiter", "university recruiting",
    "campus recruiting", "early career recruiter", "early talent",
    "emerging talent", "intern program manager", "student programs",
    "technical recruiter", "recruiting coordinator",
]

# Inclusive filter. Deliberately omits c_suite, owner, founder, partner and vp:
# they do not answer internship cold email, so enriching them wastes credits.
DEFAULT_SENIORITIES = ["entry", "senior", "manager", "director", "head"]

# email_status -> (source, verified). "invalid" maps to None: a known-bad
# address must never enter the pipeline.
EMAIL_STATUS = {
    "verified": ("enrichment_verified", True),
    "unknown": ("enrichment_unverified", False),
    "likely to engage": ("enrichment_unverified", False),
    "invalid": None,
}

PLACEHOLDER_EMAILS = ("email_not_unlocked", "notunlocked", "noemail")


@dataclass
class ApolloReport:
    searched: int = 0
    skipped_low_value: int = 0
    skipped_no_email: int = 0
    enriched: int = 0
    unmatched: int = 0
    invalid_email: int = 0
    masked: int = 0
    credits: int = 0
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"apollo: {self.searched} found, {self.enriched} enriched, "
                 f"{self.credits} credits used"]
        if self.skipped_low_value:
            lines.append(f"  skipped {self.skipped_low_value} off-target role(s) "
                         f"before spending credits")
        if self.skipped_no_email:
            lines.append(f"  skipped {self.skipped_no_email} with no address on file")
        if self.unmatched:
            lines.append(f"  {self.unmatched} did not match on enrichment")
        if self.invalid_email:
            lines.append(f"  discarded {self.invalid_email} address(es) marked invalid")
        if self.masked:
            lines.append(f"  discarded {self.masked} still-masked name(s)")
        lines += [f"  error: {e}" for e in self.errors]
        return "\n".join(lines)


def api_key() -> str:
    return os.environ.get("APOLLO_API_KEY", "").strip()


def enabled() -> bool:
    return bool(api_key())


def is_masked(name: str) -> bool:
    """True for an obfuscated name like 'Do***e'.

    This matters more than it looks: slug('Do***e') is 'doe', which would
    produce a plausible but fabricated address. Masked names must never reach
    address inference.
    """
    return "*" in (name or "")


UPGRADE_URL = "https://www.apollo.io/pricing"


def _error_message(body: str, code: int) -> str:
    """Surface Apollo's own explanation rather than a bare status code.

    Apollo returns a structured `error_details` object; the flat `error` and
    `error_code` fields are deprecated (sunset 2027-02-16), so prefer the
    former and fall back to the latter.
    """
    msg = ""
    error_code = ""
    try:
        det = json.loads(body) or {}
        msg = ((det.get("error_details") or {}).get("message")
               or det.get("error") or "")
        error_code = det.get("error_code") or ""
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    msg = msg or f"HTTP {code} from Apollo"

    if code == 403 or error_code == "API_INACCESSIBLE":
        # Not a key-permissions problem: Apollo's people endpoints require a
        # paid plan and are refused even with a master key.
        return (f"{msg} (HTTP {code}) -- this endpoint needs a paid Apollo plan. "
                f"See {UPGRADE_URL}")
    if code == 401:
        return f"{msg} (HTTP 401) -- the API key was rejected."
    if code == 429:
        return f"{msg} (HTTP 429) -- rate limited; try again later."
    return f"{msg} (HTTP {code})"


def _post(path: str, payload: dict) -> dict | None:
    """Call Apollo, or return None when disabled / a dict with 'error'."""
    key = api_key()
    if not key:
        return None
    req = urllib.request.Request(
        f"{API}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "x-api-key": key},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode()
        except OSError:
            body = ""
        return {"error": _error_message(body, exc.code)}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"error": str(exc)}


# Probed by `enrich --check`. The person payload deliberately cannot match, so
# the probe costs no credits (Apollo charges only when data is found).
PROBE_ENDPOINTS = [
    ("people search", SEARCH_PATH,
     {"person_titles": ["university recruiter"],
      "q_organization_domains_list": ["example.com"], "per_page": 1}),
    ("people enrichment", ENRICH_PATH,
     {"details": [{"first_name": "Zzqx", "last_name": "Vmpwq", "domain": "example.com"}]}),
    ("organization enrichment", "organizations/enrich", {"domain": "example.com"}),
]


def check_access() -> list[tuple[str, bool, str]]:
    """Report which Apollo endpoints this key's plan actually allows.

    Returns (label, ok, detail) per endpoint. Spends no credits.
    """
    out = []
    for label, path, payload in PROBE_ENDPOINTS:
        res = _post(path, payload)
        if res is None:
            out.append((label, False, "APOLLO_API_KEY is not set"))
        elif "error" in res:
            out.append((label, False, res["error"]))
        else:
            out.append((label, True, "accessible"))
    return out


def search_people(domain: str, titles: list | None = None,
                  seniorities: list | None = None, page: int = 1,
                  per_page: int = 25) -> dict | None:
    """Discover people at a domain. Free, but names come back masked."""
    payload = {
        "person_titles": list(titles or DEFAULT_TITLES),
        "q_organization_domains_list": [domain],
        "page": max(1, int(page)),
        "per_page": min(max(1, int(per_page)), MAX_PER_PAGE),
    }
    if seniorities:
        payload["person_seniorities"] = list(seniorities)
    data = _post(SEARCH_PATH, payload)
    if not data or "error" in data:
        return data
    return {"total": data.get("total_entries", 0),
            "stubs": [_stub(p, domain) for p in (data.get("people") or [])]}


def _stub(p: dict, domain: str) -> dict:
    last = p.get("last_name") or p.get("last_name_obfuscated") or ""
    return {
        "apollo_id": p.get("id"),
        "first_name": p.get("first_name") or "",
        "last_name": last,
        "last_masked": is_masked(last),
        "title": p.get("title") or "",
        "has_email": bool(p.get("has_email")),
        "company": (p.get("organization") or {}).get("name") or "",
        "domain": domain,
    }


def worth_enriching(stub: dict, min_title_score: float = 0.45) -> bool:
    """Credit gate: only enrich people whose role is actually on target.

    Enrichment costs credits and search is free, so the scorer runs first.
    """
    if not stub.get("apollo_id"):
        return False
    return title_relevance(stub.get("title", ""))[0] >= min_title_score


def enrich(stubs: list[dict]) -> tuple[list[dict], int, list[str]]:
    """Enrich stubs via bulk_match. Returns (matches, credits, errors)."""
    matches: list[dict] = []
    credits = 0
    errors: list[str] = []
    for i in range(0, len(stubs or []), BATCH_LIMIT):
        batch = stubs[i:i + BATCH_LIMIT]
        details = []
        for s in batch:
            d = {"id": s["apollo_id"]} if s.get("apollo_id") else {}
            if s.get("domain"):
                d["domain"] = s["domain"]
            details.append(d)
        data = _post(ENRICH_PATH, {"details": details})
        if not data:
            return matches, credits, errors
        if "error" in data:
            errors.append(data["error"])
            continue
        credits += int(data.get("credits_consumed") or 0)
        matches.extend(data.get("matches") or [])
    return matches, credits, errors


def to_candidates(matches: list[dict], domain: str,
                  report: ApolloReport | None = None) -> list[dict]:
    """Shape enriched Apollo records into pipeline-ready candidates."""
    rep = report or ApolloReport()
    out = []
    for m in matches or []:
        if not m or (m.get("match_confidence") or "").lower() == "none":
            rep.unmatched += 1
            continue
        name = (m.get("name") or
                " ".join(x for x in [m.get("first_name"), m.get("last_name")] if x)).strip()
        if not name:
            rep.unmatched += 1
            continue
        if is_masked(name):
            # Should not happen after enrichment, but a masked name reaching
            # inference would fabricate a plausible-looking address.
            rep.masked += 1
            continue

        email = (m.get("email") or "").strip().lower()
        if email and any(p in email for p in PLACEHOLDER_EMAILS):
            email = ""
        mapped = EMAIL_STATUS.get((m.get("email_status") or "unknown").lower(),
                                  ("enrichment_unverified", False))
        if email and mapped is None:
            rep.invalid_email += 1
            continue
        source, verified = mapped or ("enrichment_unverified", False)

        row = {
            "name": name,
            "title": m.get("title") or m.get("headline") or "",
            "domain": domain,
            "company": m.get("organization_name") or
                       (m.get("organization") or {}).get("name") or "",
            "source": source if email else "inferred",
            "source_url": m.get("linkedin_url") or "https://app.apollo.io/",
            "notes": f"apollo match_confidence={m.get('match_confidence') or 'unknown'}",
        }
        if email:
            row["email"] = email
            row["email_verified"] = verified
        out.append(row)
        rep.enriched += 1
    return out


def find_contacts(domain: str, titles: list | None = None,
                  seniorities: list | None = None, per_page: int = 25,
                  min_title_score: float = 0.45,
                  dry_run: bool = False) -> tuple[list[dict], ApolloReport]:
    """Search, gate on role relevance, then enrich the people worth emailing.

    With dry_run=True no credits are spent: you get the masked search results
    so you can see who is there first.
    """
    rep = ApolloReport()
    res = search_people(domain, titles, seniorities or DEFAULT_SENIORITIES,
                        per_page=per_page)
    if res is None:
        rep.errors.append("APOLLO_API_KEY is not set")
        return [], rep
    if "error" in res:
        rep.errors.append(res["error"])
        return [], rep

    stubs = res.get("stubs") or []
    rep.searched = len(stubs)
    keep = []
    for s in stubs:
        if not worth_enriching(s, min_title_score):
            rep.skipped_low_value += 1
            continue
        if not s.get("has_email"):
            rep.skipped_no_email += 1
            continue
        keep.append(s)

    if dry_run:
        return [{"name": f"{s['first_name']} {s['last_name']}".strip(),
                 "title": s["title"], "domain": domain,
                 "masked": s["last_masked"], "apollo_id": s["apollo_id"]}
                for s in keep], rep

    matches, credits, errors = enrich(keep)
    rep.credits = credits
    rep.errors.extend(errors)
    return to_candidates(matches, domain, rep), rep
