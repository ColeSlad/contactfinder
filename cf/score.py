"""Rank candidate contacts for a SWE-internship cold email.

Four orthogonal axes: how relevant the role is, how reachable the person is
(seniority), how trustworthy the source was, and how fresh it is -- plus how
confident we are in the address itself. Weights live in config and are retuned
by learn.py once enough reply outcomes exist.
"""
import re
from dataclasses import dataclass, field

from .normalize import normalize_title

# Ordered: first match wins, so the most specific pattern must come first.
# Scores encode who actually owns intern requisitions and answers cold email.
TITLE_TIERS: list[tuple[str, float, str]] = [
    # UNI matches 'university', 'univ', 'uni'; recruit\w* matches
    # 'recruiter'/'recruiting' -- a trailing \b would miss both.
    (r"\b(universit\w*|univ|uni|campus)\b.*\b(recruit|talent|relations|program)", 1.00, "university-recruiting"),
    (r"\b(early[- ]?career|early talent|emerging talent)\b", 1.00, "early-career"),
    (r"\b(intern\w*|new grad\w*|new graduate\w*|student\w*)\b.*\b(program|recruit|talent|hiring)", 0.95, "intern-program"),
    (r"\b(recruit\w*|talent)\b.*\b(universit\w*|univ|uni|campus|early[- ]?career|student|intern|new grad)", 0.95, "university-recruiting"),
    (r"\b(universit\w*|campus) relations\b", 0.90, "university-relations"),
    (r"\b(sourcer|sourcing)\b.*\b(universit\w*|campus|student|intern|early[- ]?career|new grad)", 0.60, "university-sourcing"),
    (r"\b(universit\w*|campus|student|intern|early[- ]?career)\b.*\b(sourcer|sourcing)\b", 0.60, "university-sourcing"),
    (r"\b(technical|software|engineering|tech)\b.*\brecruit", 0.70, "technical-recruiting"),
    (r"\brecruit\w*\b.*\b(engineer|technical|software)", 0.70, "technical-recruiting"),
    (r"\b(talent acquisition|ta)\b.*\b(partner|specialist|manager|lead|associate)", 0.55, "talent-acquisition"),
    (r"\b(recruiting|talent) (coordinator|associate|assistant|operations|ops)\b", 0.50, "recruiting-coordinator"),
    (r"\b(sourcer|sourcing)\b", 0.45, "sourcing"),
    (r"\brecruit\w*\b", 0.55, "recruiting-generic"),
    (r"\btalent acquisition\b", 0.55, "talent-acquisition"),
    (r"\b(people operations|people ops|human resources|hr)\b", 0.35, "people-ops"),
    (r"\bpeople (partner|business partner|advisor|team)\b", 0.35, "people-ops"),
    (r"\b(engineering|software) (manager|lead|director)\b", 0.30, "eng-hiring-manager"),
    (r"\b(head|vp|vice president|director) of (people|talent|hr|human resources)\b", 0.40, "people-leadership"),
    (r"\bchief (people|talent|hr|human resources)\b", 0.40, "people-leadership"),
]

# How likely the person is to personally read and answer a cold email.
SENIORITY_TIERS: list[tuple[str, float, str]] = [
    (r"\b(chief|cxo|c-level|founder|co-founder|owner|president)\b", 0.20, "c-level"),
    (r"\b(svp|evp|senior vice president|executive vice president)\b", 0.30, "svp"),
    (r"\b(vp|vice president)\b", 0.35, "vp"),
    (r"\b(global head|head of|director)\b", 0.60, "director"),
    (r"\b(senior manager|sr manager)\b", 0.75, "senior-manager"),
    (r"\b(manager|lead|principal)\b", 0.85, "manager"),
    (r"\b(senior|sr|staff)\b", 0.95, "senior-ic"),
    (r"\b(coordinator|associate|assistant|specialist|partner|analyst)\b", 1.00, "ic"),
]

SOURCE_QUALITY = {
    "company_directory": 1.00,
    "enrichment_verified": 0.95,
    "careers_page": 0.90,
    "job_posting": 0.80,
    "university_career_fair": 0.80,
    "conference_or_event": 0.75,
    "enrichment_unverified": 0.70,
    "press_or_blog": 0.60,
    "social_profile": 0.55,
    "inferred": 0.30,
    "unknown": 0.30,
}

DEFAULT_WEIGHTS = {
    "title": 0.40,
    "email_confidence": 0.25,
    "source": 0.15,
    "recency": 0.12,
    "seniority": 0.08,
}

RECENCY_HALF_LIFE_MONTHS = 12.0
RECENCY_FLOOR = 0.15


@dataclass
class ScoreResult:
    score: float
    role_tier: str
    seniority_tier: str
    features: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)


def title_relevance(title: str) -> tuple[float, str]:
    t = normalize_title(title)
    if not t:
        return 0.10, "unknown"
    for pat, val, label in TITLE_TIERS:
        if re.search(pat, t):
            return val, label
    return 0.10, "unrelated"


def seniority_fit(title: str) -> tuple[float, str]:
    t = normalize_title(title)
    if not t:
        return 0.90, "unknown"
    for pat, val, label in SENIORITY_TIERS:
        if re.search(pat, t):
            return val, label
    return 0.90, "ic"


def recency(months_old: float | None) -> float:
    if months_old is None:
        return 0.50
    m = max(0.0, float(months_old))
    return max(RECENCY_FLOOR, 0.5 ** (m / RECENCY_HALF_LIFE_MONTHS))


def score_contact(contact: dict, weights: dict | None = None) -> ScoreResult:
    """Score one candidate. `contact` keys: title, email_confidence, source,
    months_old, email."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    title = contact.get("title", "") or ""
    t_val, t_label = title_relevance(title)
    s_val, s_label = seniority_fit(title)
    src = SOURCE_QUALITY.get(contact.get("source") or "unknown", SOURCE_QUALITY["unknown"])
    rec = recency(contact.get("months_old"))
    econf = float(contact.get("email_confidence") or 0.0)

    feats = {"title": t_val, "email_confidence": econf, "source": src,
             "recency": rec, "seniority": s_val}
    total_w = sum(w.get(k, 0.0) for k in feats) or 1.0
    score = sum(feats[k] * w.get(k, 0.0) for k in feats) / total_w

    flags = []
    if not contact.get("email"):
        flags.append("no_email")
    if econf < 0.35:
        flags.append("low_email_confidence")
    if (contact.get("months_old") or 0) > 24:
        flags.append("stale_source")
    if s_val <= 0.35:
        flags.append("too_senior_for_cold_email")
    if t_val <= 0.30:
        flags.append("off_target_role")

    return ScoreResult(round(score, 4), t_label, s_label, feats, flags)


def rank(contacts: list[dict], weights: dict | None = None) -> list[dict]:
    """Attach scores and return contacts sorted best-first."""
    out = []
    for c in contacts:
        r = score_contact(c, weights)
        out.append({**c, "score": r.score, "role_tier": r.role_tier,
                    "seniority_tier": r.seniority_tier, "flags": r.flags})
    return sorted(out, key=lambda c: -c["score"])
