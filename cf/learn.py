"""Self-improvement: turn outreach outcomes back into better predictions.

Three mechanisms, each gated on sample size so a handful of data points cannot
make the framework thrash:

1. Per-domain email formats   -- derived in store.evidence_for(); one confirmed
   address locks a company's format (the highest-leverage signal).
2. The global format prior    -- retuned from confirmations across domains, one
   vote per domain so a heavily-mined company can't dominate.
3. Scoring weights            -- coordinate ascent on reply-ranking AUC,
   regularized toward the shipped defaults.
"""
from dataclasses import dataclass, field

from .mail import classify_bounce, classify_reply
from .normalize import domain_of
from .patterns import GLOBAL_PRIOR, pattern_of
from .score import DEFAULT_WEIGHTS, score_contact
from .store import months_between

MIN_SENT_FOR_WEIGHTS = 25
MIN_REPLIES_FOR_WEIGHTS = 3
MIN_DOMAINS_FOR_PRIOR = 5
PRIOR_PSEUDOCOUNTS = 20.0
REGULARIZATION = 0.15
WEIGHT_GRID = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60]

EVENT_TO_OUTCOME = {"hard": "hard_bounce", "soft": "soft_bounce", "blocked": "blocked"}


@dataclass
class LearnReport:
    events_ingested: int = 0
    outcomes_written: int = 0
    confirmations_added: int = 0
    domains_locked: list = field(default_factory=list)
    prior_updated: bool = False
    weights_updated: bool = False
    auc_before: float | None = None
    auc_after: float | None = None
    notes: list = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"ingested {self.events_ingested} events -> {self.outcomes_written} outcomes, "
            f"{self.confirmations_added} confirmations",
        ]
        if self.domains_locked:
            lines.append(f"formats locked: {', '.join(self.domains_locked)}")
        lines.append(f"global prior: {'retuned' if self.prior_updated else 'unchanged'}")
        if self.weights_updated:
            lines.append(f"weights retuned (AUC {self.auc_before:.3f} -> {self.auc_after:.3f})")
        else:
            lines.append("weights: unchanged")
        lines += [f"note: {n}" for n in self.notes]
        return "\n".join(lines)


# ---------------------------------------------------------------- ingestion
def ingest_events(store, events: list[dict], report: LearnReport | None = None) -> LearnReport:
    """Fold Gmail-derived events into outcomes and format confirmations.

    Each event: {kind: sent|reply|bounce, address, from, subject, body,
    headers, date}. Addresses are matched back to contacts to learn which
    format was used.
    """
    rep = report or LearnReport()
    confirmed: set[str] = set()
    for ev in events or []:
        rep.events_ingested += 1
        kind = (ev.get("kind") or "").lower()
        addr = (ev.get("address") or "").lower()
        when = ev.get("date") or ""
        subject, body = ev.get("subject", ""), ev.get("body", "")

        if kind == "sent":
            c = store.contact_by_email(addr)
            if c:
                store.add_outcome(c["id"], "sent", addr, "outreach sent", when)
                store.set_status(c["id"], "contacted")
                rep.outcomes_written += 1
            else:
                rep.notes.append(f"sent to unknown address {addr}")
            continue

        verdict = classify_reply(ev.get("from", ""), subject, body, ev.get("headers"))
        if kind == "bounce" or verdict == "bounce":
            b = classify_bounce(subject, body, ev.get("smtp_status", ""))
            target = addr or b.address
            c = store.contact_by_email(target)
            outcome = EVENT_TO_OUTCOME.get(b.kind)
            if not outcome:
                rep.notes.append(f"no bounce indicators for {target or 'unknown'}")
                continue
            store.add_outcome(c["id"] if c else None, outcome, target, b.reason, when)
            rep.outcomes_written += 1
            if c and b.kind == "hard":
                # Format may still be right -- the person may simply have left.
                store.set_status(c["id"], "bounced")
            continue

        # A reply of either sort proves the address is deliverable.
        target = addr or _sender(ev)
        c = store.contact_by_email(target)
        outcome = "human_reply" if verdict == "human" else "auto_reply"
        store.add_outcome(c["id"] if c else None, outcome, target, verdict, when)
        rep.outcomes_written += 1
        if c:
            if verdict == "human":
                store.set_status(c["id"], "replied")
            pat = c.get("email_pattern") or pattern_of(target, c.get("name", ""))
            if pat and target not in confirmed:
                store.add_confirmation(c["company_domain"], pat, target,
                                       c.get("name", ""), f"{outcome}", when)
                confirmed.add(target)
                rep.confirmations_added += 1
        else:
            rep.notes.append(f"reply from unknown address {target}")
    return rep


def _sender(ev: dict) -> str:
    raw = (ev.get("from") or "").lower()
    return raw.split("<")[-1].strip("> ") if "<" in raw else raw.strip()


# ------------------------------------------------------------- global prior
def retune_global_prior(store, base: dict | None = None,
                        min_domains: int = MIN_DOMAINS_FOR_PRIOR) -> dict | None:
    """Blend observed formats into the shipped prior. One vote per domain."""
    base = dict(base or GLOBAL_PRIOR)
    seen: dict[str, str] = {}
    for r in store.db.execute("SELECT domain, pattern FROM confirmations"):
        seen.setdefault(r["domain"], r["pattern"])
    if len(seen) < min_domains:
        return None

    counts = {p: 0.0 for p in base}
    for pattern in seen.values():
        if pattern in counts:
            counts[pattern] += 1.0
    total = sum(counts.values())
    if total <= 0:
        return None
    blended = {p: (counts[p] + PRIOR_PSEUDOCOUNTS * base[p]) / (total + PRIOR_PSEUDOCOUNTS)
               for p in base}
    norm = sum(blended.values()) or 1.0
    return {p: round(v / norm, 5) for p, v in blended.items()}


# ----------------------------------------------------------------- weights
def auc(pos: list[float], neg: list[float]) -> float:
    """Probability a replier outranks a non-replier (ties count half)."""
    if not pos or not neg:
        return 0.5
    wins = sum((1.0 if p > n else 0.5 if p == n else 0.0) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def training_rows(store, today: str | None = None) -> list[tuple[dict, int]]:
    """Contacts we actually emailed, labelled 1 if a human replied."""
    sent, replied = set(), set()
    for o in store.outcomes():
        if o["contact_id"] is None:
            continue
        if o["kind"] == "sent":
            sent.add(o["contact_id"])
        elif o["kind"] == "human_reply":
            replied.add(o["contact_id"])
    rows = []
    for c in store.contacts():
        if c["id"] not in sent:
            continue
        feat = score_contact({
            "title": c.get("title", ""),
            "email_confidence": c.get("email_confidence") or 0.0,
            "source": c.get("source"),
            "months_old": months_between(c.get("found_at"), today),
            "email": c.get("email"),
        }).features
        rows.append((feat, 1 if c["id"] in replied else 0))
    return rows


def _objective(rows, weights) -> float:
    total = sum(weights.values()) or 1.0
    scored = [(sum(f[k] * weights.get(k, 0.0) for k in f) / total, y) for f, y in rows]
    pos = [s for s, y in scored if y == 1]
    neg = [s for s, y in scored if y == 0]
    penalty = REGULARIZATION * sum(
        abs(weights.get(k, 0.0) - DEFAULT_WEIGHTS[k]) for k in DEFAULT_WEIGHTS)
    return auc(pos, neg) - penalty


def tune_weights(store, today: str | None = None, passes: int = 3):
    """Coordinate ascent on reply-ranking AUC. Returns (weights, before, after)
    or (None, reason, None) when there isn't enough data to learn from."""
    rows = training_rows(store, today)
    n_pos = sum(y for _, y in rows)
    if len(rows) < MIN_SENT_FOR_WEIGHTS:
        return None, f"only {len(rows)} contacted (need {MIN_SENT_FOR_WEIGHTS})", None
    if n_pos < MIN_REPLIES_FOR_WEIGHTS:
        return None, f"only {n_pos} replies (need {MIN_REPLIES_FOR_WEIGHTS})", None
    if len(rows) - n_pos < MIN_REPLIES_FOR_WEIGHTS:
        return None, "not enough non-replies to contrast against", None

    best = dict(DEFAULT_WEIGHTS)
    before = _objective(rows, best)
    score = before
    for _ in range(passes):
        improved = False
        for key in DEFAULT_WEIGHTS:
            for val in WEIGHT_GRID:
                trial = {**best, key: val}
                if sum(trial.values()) <= 0:
                    continue
                s = _objective(rows, trial)
                if s > score + 1e-9:
                    best, score, improved = trial, s, True
        if not improved:
            break
    total = sum(best.values()) or 1.0
    best = {k: round(v / total, 4) for k, v in best.items()}
    return best, before, score


# --------------------------------------------------------------- orchestration
def learn(store, events: list[dict] | None = None, today: str | None = None) -> LearnReport:
    """Ingest any new events, then retune the prior and the scoring weights."""
    rep = ingest_events(store, events or [])

    prior = retune_global_prior(store, store.get_setting("global_prior"))
    if prior:
        store.set_setting("global_prior", prior)
        rep.prior_updated = True
    else:
        rep.notes.append(
            f"prior needs confirmations from {MIN_DOMAINS_FOR_PRIOR}+ distinct domains")

    weights, before, after = tune_weights(store, today)
    if weights:
        store.set_setting("weights", weights)
        rep.weights_updated = True
        rep.auc_before, rep.auc_after = before, after
    else:
        rep.notes.append(f"weights not retuned: {before}")

    from .patterns import locked_pattern
    for row in store.db.execute("SELECT DISTINCT domain FROM confirmations"):
        d = row["domain"]
        pat = locked_pattern(store.evidence_for(d))
        if pat:
            rep.domains_locked.append(f"{d}={pat}")
    return rep
