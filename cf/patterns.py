"""Email-format inference.

Companies use one address format. So a single *confirmed* address at a domain
reveals the format for every other person there -- that is the highest-leverage
thing this framework learns. `candidates()` blends a global prior over formats
with per-domain evidence (confirmed hits, hard bounces) accumulated by learn.py.
"""
from dataclasses import dataclass, replace

from .normalize import Name, parse_name

# Approximate real-world frequency of corporate address formats. Used only as a
# prior: per-domain evidence overrides it, and learn.py retunes it from outcomes.
GLOBAL_PRIOR = {
    "first.last": 0.38,
    "firstlast": 0.13,
    "flast": 0.11,
    "first": 0.08,
    "f.last": 0.06,
    "first_last": 0.05,
    "firstl": 0.04,
    "first.l": 0.03,
    "last.first": 0.025,
    "lastfirst": 0.02,
    "first-last": 0.02,
    "last": 0.015,
    "fl": 0.015,
    "firstmlast": 0.01,
}

BUILDERS = {
    "first.last":  lambda n: f"{n.f}.{n.l}" if n.l else None,
    "firstlast":   lambda n: f"{n.f}{n.l}" if n.l else None,
    "flast":       lambda n: f"{n.fi}{n.l}" if n.l else None,
    "first":       lambda n: n.f or None,
    "f.last":      lambda n: f"{n.fi}.{n.l}" if n.l else None,
    "first_last":  lambda n: f"{n.f}_{n.l}" if n.l else None,
    "firstl":      lambda n: f"{n.f}{n.li}" if n.l else None,
    "first.l":     lambda n: f"{n.f}.{n.li}" if n.l else None,
    "last.first":  lambda n: f"{n.l}.{n.f}" if n.l else None,
    "lastfirst":   lambda n: f"{n.l}{n.f}" if n.l else None,
    "first-last":  lambda n: f"{n.f}-{n.l}" if n.l else None,
    "last":        lambda n: n.l or None,
    "fl":          lambda n: f"{n.fi}{n.li}" if n.l else None,
    "firstmlast":  lambda n: (f"{n.f}{n.mi}{n.l}" if n.l and n.mi else None),
}

HIT_WEIGHT = 3.0    # a confirmed address is strong evidence
MISS_WEIGHT = 3.0   # a hard bounce is equally strong negative evidence
ALPHA = 4.0         # prior strength (in pseudo-observations)
LOCKED_CONFIDENCE = 0.95


@dataclass(frozen=True)
class Candidate:
    email: str
    pattern: str
    confidence: float
    locked: bool = False


def _mi(self) -> str:
    from .normalize import slug
    return slug(self.middle)[:1]


Name.mi = property(_mi)  # middle initial, only needed by the firstmlast format


def localpart(pattern: str, name) -> str | None:
    """Build the local part of an address for one format, or None if impossible."""
    n = parse_name(name) if isinstance(name, str) else name
    build = BUILDERS.get(pattern)
    if build is None or not n.ok:
        return None
    lp = build(n)
    return lp or None


def pattern_of(email: str, name) -> str | None:
    """Reverse-engineer which format a known-good address uses.

    Returns the highest-prior format that reproduces the address, so that
    confirming one email teaches us the company's format.
    """
    n = parse_name(name) if isinstance(name, str) else name
    local = (email or "").strip().lower().split("@")[0]
    if not local:
        return None
    matches = [p for p in BUILDERS if localpart(p, n) == local]
    if not matches:
        # Retry against hyphen-preserving slugs: plenty of companies spell
        # compound names 'mary-jane.watson' rather than flattening them.
        hy = replace(n, keep_hyphen=True)
        matches = [p for p in BUILDERS if localpart(p, hy) == local]
    if not matches:
        return None
    return max(matches, key=lambda p: GLOBAL_PRIOR.get(p, 0.0))


def locked_pattern(evidence: dict | None) -> str | None:
    """The domain's format, when evidence makes it near-certain.

    A hard bounce on a correctly-formatted address is ambiguous: the format may
    be right and the person simply gone. So misses must *outweigh* hits to block
    a lock (hits > 2*misses), rather than a single departed employee vetoing an
    otherwise well-evidenced format. A rival format with as many hits means
    genuinely contradictory evidence, and nothing locks.
    """
    if not evidence:
        return None
    scored = {p: (e.get("hits", 0), e.get("misses", 0)) for p, e in evidence.items()}
    best = max(scored.items(), key=lambda kv: kv[1][0], default=None)
    if not best:
        return None
    pattern, (hits, misses) = best
    if hits < 1 or hits <= misses * 2:
        return None
    rivals = [h for p, (h, _) in scored.items() if p != pattern and h >= hits]
    return None if rivals else pattern


def candidates(name, domain: str, evidence: dict | None = None,
               global_prior: dict | None = None, limit: int = 0) -> list[Candidate]:
    """Rank plausible addresses for a person at a domain, best first.

    With no evidence the returned confidences equal the global prior. Evidence
    shifts them; a locked format dominates.
    """
    n = parse_name(name) if isinstance(name, str) else name
    prior = dict(global_prior or GLOBAL_PRIOR)
    evidence = evidence or {}
    if not n.ok or not domain:
        return []

    locked = locked_pattern(evidence)
    raw: dict[str, float] = {}
    emails: dict[str, str] = {}
    for pattern, gp in prior.items():
        lp = localpart(pattern, n)
        if not lp:
            continue
        ev = evidence.get(pattern, {})
        hits = float(ev.get("hits", 0)) * HIT_WEIGHT
        misses = float(ev.get("misses", 0)) * MISS_WEIGHT
        a = hits + ALPHA * gp
        b = misses + ALPHA * (1.0 - gp)
        raw[pattern] = a / (a + b) if (a + b) else 0.0
        emails[pattern] = f"{lp}@{domain}"

    if not raw:
        return []

    if locked in raw:
        spare = 1.0 - LOCKED_CONFIDENCE
        others = {p: v for p, v in raw.items() if p != locked}
        tot = sum(others.values()) or 1.0
        conf = {p: v / tot * spare for p, v in others.items()}
        conf[locked] = LOCKED_CONFIDENCE
    else:
        tot = sum(raw.values()) or 1.0
        conf = {p: v / tot for p, v in raw.items()}

    # Two formats can produce the same address; keep one entry, pooled.
    merged: dict[str, Candidate] = {}
    for pattern, c in sorted(conf.items(), key=lambda kv: -kv[1]):
        email = emails[pattern]
        if email in merged:
            prev = merged[email]
            merged[email] = Candidate(email, prev.pattern,
                                      min(1.0, prev.confidence + c), prev.locked)
        else:
            merged[email] = Candidate(email, pattern, c, pattern == locked)

    out = sorted(merged.values(), key=lambda c: -c.confidence)
    return out[:limit] if limit else out
