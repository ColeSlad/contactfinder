"""Optional paid enrichment (Hunter.io).

Entirely opt-in: with no API key every function returns None and the rest of
the framework runs unchanged. The highest-value call is domain_search, which
reports a company's address format directly -- that maps onto a format
confirmation and locks inference for everyone at the domain.

Set HUNTER_API_KEY to enable. Uses stdlib urllib so there is no dependency.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.hunter.io/v2"
TIMEOUT = 20

# Hunter expresses formats as templates; map them onto our pattern names.
HUNTER_FORMATS = {
    "{first}.{last}": "first.last",
    "{first}{last}": "firstlast",
    "{f}{last}": "flast",
    "{first}": "first",
    "{f}.{last}": "f.last",
    "{first}_{last}": "first_last",
    "{first}{l}": "firstl",
    "{first}.{l}": "first.l",
    "{last}.{first}": "last.first",
    "{last}{first}": "lastfirst",
    "{first}-{last}": "first-last",
    "{last}": "last",
    "{f}{l}": "fl",
    "{f}.{l}": "fl",
    "{first}{m}{last}": "firstmlast",
}


def hunter_format_to_pattern(fmt: str) -> str | None:
    """Translate a Hunter format template into one of our pattern names."""
    if not fmt:
        return None
    key = fmt.strip().lower().replace(" ", "")
    return HUNTER_FORMATS.get(key)


def api_key() -> str:
    return os.environ.get("HUNTER_API_KEY", "").strip()


def enabled() -> bool:
    return bool(api_key())


def _get(path: str, **params) -> dict | None:
    """Call Hunter, or return None if disabled or the call fails."""
    key = api_key()
    if not key:
        return None
    params["api_key"] = key
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as exc:
        return {"error": str(exc)}


def domain_search(domain: str, limit: int = 10) -> dict | None:
    """Company format plus any published recruiting addresses.

    Returns {"pattern": <our pattern name or None>, "emails": [...]}.
    """
    data = _get("domain-search", domain=domain, limit=limit, department="hr")
    if not data or "error" in data:
        return data
    d = data.get("data") or {}
    return {
        "pattern": hunter_format_to_pattern(d.get("pattern") or ""),
        "raw_pattern": d.get("pattern"),
        "emails": [
            {"email": e.get("value"), "first": e.get("first_name"),
             "last": e.get("last_name"), "title": e.get("position"),
             "confidence": (e.get("confidence") or 0) / 100.0,
             "source": "enrichment_verified" if e.get("verification", {}).get("status") == "valid"
                       else "enrichment_unverified"}
            for e in (d.get("emails") or [])
        ],
    }


def find_email(domain: str, first: str, last: str) -> dict | None:
    data = _get("email-finder", domain=domain, first_name=first, last_name=last)
    if not data or "error" in data:
        return data
    d = data.get("data") or {}
    return {"email": d.get("email"), "confidence": (d.get("score") or 0) / 100.0}


def verify(email: str) -> dict | None:
    data = _get("email-verifier", email=email)
    if not data or "error" in data:
        return data
    d = data.get("data") or {}
    return {"email": d.get("email"), "status": d.get("status"),
            "valid": d.get("status") == "valid",
            "confidence": (d.get("score") or 0) / 100.0}


def to_candidates(result: dict, domain: str) -> list[dict]:
    """Shape a domain_search result into pipeline-ready candidates."""
    out = []
    for e in (result or {}).get("emails", []):
        name = " ".join(x for x in [e.get("first"), e.get("last")] if x).strip()
        if not name or not e.get("email"):
            continue
        out.append({
            "name": name, "title": e.get("title") or "", "domain": domain,
            "email": e["email"], "email_verified": e.get("source") == "enrichment_verified",
            "source": e.get("source", "enrichment_unverified"),
            "source_url": f"https://hunter.io/search/{domain}",
        })
    return out
