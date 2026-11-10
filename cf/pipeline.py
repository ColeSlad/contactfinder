"""Turn researched candidates into scored, stored, outreach-ready contacts.

This is the seam between research and machinery: web research produces a list of
{name, title, domain, source, source_url}, and this module infers the address,
scores the person and persists them.
"""
from dataclasses import dataclass, field
from datetime import date

from .config import (DEFAULT_CONFIG, effective_prior, effective_weights,
                     evidence_with_overrides)
from .normalize import contact_key, domain_of
from .patterns import candidates, pattern_of
from .score import is_role_row, score_contact
from .store import months_between

REQUIRED = ("name", "domain")


@dataclass
class IngestReport:
    added: int = 0
    updated: int = 0
    skipped: list = field(default_factory=list)
    per_company: dict = field(default_factory=dict)
    locked: list = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"ingested {self.added + self.updated} contacts "
                 f"({self.added} new, {self.updated} merged)"]
        for dom, n in sorted(self.per_company.items(), key=lambda kv: -kv[1]):
            tag = " [format locked]" if dom in self.locked else ""
            lines.append(f"  {dom:28} {n}{tag}")
        if self.skipped:
            lines.append(f"skipped {len(self.skipped)}:")
            lines += [f"  - {s}" for s in self.skipped[:10]]
        return "\n".join(lines)


def ingest_candidates(store, cands: list[dict], cfg: dict | None = None,
                      today: str | None = None) -> IngestReport:
    """Infer addresses, score, and persist researched candidates."""
    cfg = cfg or {}
    rep = IngestReport()
    weights = effective_weights(store, cfg)
    prior = effective_prior(store, cfg)
    stamp = today or date.today().isoformat()

    for raw in cands or []:
        name = (raw.get("name") or "").strip()
        given_email = (raw.get("email") or "").strip().lower()
        domain = domain_of(raw.get("domain") or raw.get("company_domain")
                           or given_email)
        # A published role inbox has no person behind it but is still a valid
        # target -- often the only one a company publishes.
        role = bool(given_email) and not name
        if (not name and not role) or not domain:
            rep.skipped.append(f"missing name or domain: {raw!r:.80}")
            continue
        if "*" in name:
            # Obfuscated names ('Do***e') arrive from provider search endpoints.
            # slug() would strip the asterisks and yield 'doe', fabricating a
            # plausible-looking address, so refuse them outright.
            rep.skipped.append(f"obfuscated name, cannot infer an address: {name!r}")
            continue

        store.upsert_company(domain, raw.get("company", ""), raw.get("careers_url", ""))
        evidence = evidence_with_overrides(store, domain, cfg)

        given = given_email
        verified = bool(raw.get("email_verified"))
        if given:
            email = given
            pattern = pattern_of(given, name) or ""
            # A verified address is near-certain; an unverified one that someone
            # published is still far better than a guess.
            confidence = 0.99 if verified else 0.85
        else:
            ranked = candidates(name, domain, evidence, prior, limit=1)
            if not ranked:
                rep.skipped.append(f"could not infer an address for {name} @ {domain}")
                continue
            email, pattern, confidence = ranked[0].email, ranked[0].pattern, ranked[0].confidence

        found_at = raw.get("found_at") or stamp
        scored = score_contact({
            "title": raw.get("title", ""), "email": email, "role": role,
            "email_confidence": confidence, "source": raw.get("source", "unknown"),
            "months_old": months_between(found_at, stamp),
        }, weights)

        # Role inboxes all have an empty name, so they key on the address.
        key = f"role|{email}" if role else contact_key(name, domain)
        existed = bool(store.db.execute(
            "SELECT 1 FROM contacts WHERE key=?", (key,)).fetchone())
        store.upsert_contact({
            "key": key, "company_domain": domain, "name": name,
            "title": raw.get("title", ""), "email": email, "email_pattern": pattern,
            "email_confidence": confidence, "email_verified": verified,
            "source": raw.get("source", "unknown"), "source_url": raw.get("source_url", ""),
            "found_at": found_at, "score": scored.score, "role_tier": scored.role_tier,
            "seniority_tier": scored.seniority_tier, "flags": scored.flags,
            "notes": raw.get("notes", ""),
        })
        # A role inbox must never teach a format: there is no name to
        # reverse-map from, so careers@ would imply nothing about first.last.
        if verified and pattern and not role:
            store.add_confirmation(domain, pattern, email, name,
                                   "research_verified", stamp)

        rep.added += 0 if existed else 1
        rep.updated += 1 if existed else 0
        rep.per_company[domain] = rep.per_company.get(domain, 0) + 1

    from .patterns import locked_pattern
    for dom in rep.per_company:
        if locked_pattern(store.evidence_for(dom)):
            rep.locked.append(dom)
    return rep


def rescore_all(store, cfg: dict | None = None, today: str | None = None) -> int:
    """Re-apply current weights and per-domain evidence to every contact.

    Run after `cf learn`: newly locked formats and retuned weights should change
    both the addresses and the ranking of contacts already on file.
    """
    cfg = cfg or {}
    weights = effective_weights(store, cfg)
    prior = effective_prior(store, cfg)
    stamp = today or date.today().isoformat()
    n = 0
    for c in store.contacts():
        domain = c["company_domain"]
        evidence = evidence_with_overrides(store, domain, cfg)
        email = c.get("email") or ""
        pattern = c.get("email_pattern") or ""
        confidence = c.get("email_confidence") or 0.0

        role = is_role_row(c)
        if not c.get("email_verified") and not role:
            ranked = candidates(c["name"], domain, evidence, prior, limit=1)
            if ranked:
                email, pattern, confidence = ranked[0].email, ranked[0].pattern, ranked[0].confidence

        scored = score_contact({
            "title": c.get("title", ""), "email": email, "role": role,
            "email_confidence": confidence, "source": c.get("source"),
            "months_old": months_between(c.get("found_at"), stamp),
        }, weights)
        store.db.execute(
            """UPDATE contacts SET email=?, email_pattern=?, email_confidence=?,
                 score=?, role_tier=?, seniority_tier=? WHERE id=?""",
            (email, pattern, confidence, scored.score, scored.role_tier,
             scored.seniority_tier, c["id"]))
        n += 1
    store.db.commit()
    return n


def queue(store, cfg: dict | None = None, limit: int = 0) -> list[dict]:
    """The outreach queue: best contacts first, capped per company."""
    run = (cfg or {}).get("run", {})
    min_score = float(run.get("min_score", 0.0))
    per_company = int(run.get("max_contacts_per_company", 0) or 0)
    # .get with a default (not `or`) so an explicit [] stays permissive.
    excluded = set(run.get("exclude_flags", DEFAULT_CONFIG["run"]["exclude_flags"]))
    seen: dict[str, int] = {}
    out = []
    for c in store.contacts(min_score=min_score):
        if c.get("status") in ("contacted", "replied", "bounced", "dead"):
            continue
        if excluded & set(c.get("flags") or []):
            continue
        dom = c["company_domain"]
        if per_company and seen.get(dom, 0) >= per_company:
            continue
        seen[dom] = seen.get(dom, 0) + 1
        out.append(c)
        if limit and len(out) >= limit:
            break
    return out
