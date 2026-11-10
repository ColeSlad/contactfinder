"""Self-test harness: scores the framework against a golden set and tracks it.

`cf eval` prints a scorecard and appends it to eval/history.jsonl. `cf eval
--gate` additionally fails when the overall score drops below the best run
recorded so far, which turns accidental regressions into a hard error.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_yaml
from .mail import classify_bounce, classify_reply
from .normalize import parse_name
from .patterns import candidates, pattern_of
from .score import alias_relevance, score_contact, title_relevance

HISTORY_PATH = "eval/history.jsonl"

# Overall score is a weighted mean of the sections. Lock transfer carries the
# most weight: it is the mechanism the whole framework is built around.
SECTION_WEIGHTS = {
    "name_parse": 0.10,
    "format_at_1": 0.20,
    "format_at_3": 0.05,
    "pattern_id": 0.05,
    "reverse_map": 0.10,
    "lock_transfer": 0.25,
    "title_tier": 0.10,
    "alias_tier": 0.05,
    "ranking": 0.05,
    "mail": 0.05,
}


@dataclass
class Scorecard:
    sections: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)
    # Documented limitations that unexpectedly started working, and so can be
    # promoted into the scored set.
    promotable: list = field(default_factory=list)
    limitations: int = 0

    @property
    def overall(self) -> float:
        total = sum(SECTION_WEIGHTS.get(k, 0.0) for k in self.sections)
        if not total:
            return 0.0
        return round(sum(self.sections[k] * SECTION_WEIGHTS.get(k, 0.0)
                         for k in self.sections) / total, 4)

    def render(self) -> str:
        lines = ["ContactFinder self-evaluation", "=" * 46]
        for name, val in self.sections.items():
            bar = "#" * int(round(val * 20)) + "." * (20 - int(round(val * 20)))
            lines.append(f"  {name:16} {val:6.1%}  [{bar}]")
        lines += ["=" * 46, f"  {'OVERALL':16} {self.overall:6.1%}"]
        if self.failures:
            lines.append(f"\n{len(self.failures)} failing case(s):")
            lines += [f"  - {f}" for f in self.failures[:15]]
            if len(self.failures) > 15:
                lines.append(f"  ... and {len(self.failures) - 15} more")
        if self.limitations:
            lines.append(f"\n{self.limitations} known limitation(s), not scored "
                         f"(see known_limitations in the golden set)")
        if self.promotable:
            lines.append("\nKnown limitations that now PASS -- promote them:")
            lines += [f"  + {p}" for p in self.promotable]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"sections": self.sections, "overall": self.overall,
                "failures": self.failures, "promotable": self.promotable,
                "limitations": self.limitations}


def _fraction(ok: int, total: int) -> float:
    return round(ok / total, 4) if total else 0.0


def evaluate(golden_path: str = "eval/golden.yaml") -> Scorecard:
    g = load_yaml(golden_path, {})
    card = Scorecard()

    # --- name parsing in isolation ---
    ok = n = 0
    for case in g.get("name_cases", []):
        n += 1
        got = parse_name(case["raw"])
        if got.first == case["first"] and got.last == case["last"]:
            ok += 1
        else:
            card.failures.append(
                f"name_parse {case['raw']!r}: got ({got.first!r}, {got.last!r}), "
                f"want ({case['first']!r}, {case['last']!r})")
    card.sections["name_parse"] = _fraction(ok, n)

    # --- format inference from name + domain, with the format already known ---
    ok1 = ok3 = okpat = n = 0
    for case in g.get("format_cases", []):
        domain, fmt = case["domain"], case["format"]
        evidence = {fmt: {"hits": 1, "misses": 0}}
        for person in case.get("people", []):
            n += 1
            ranked = candidates(person["name"], domain, evidence)
            emails = [c.email for c in ranked]
            if emails[:1] == [person["email"]]:
                ok1 += 1
            else:
                card.failures.append(
                    f"format@1 {person['name']}@{domain}: got "
                    f"{emails[0] if emails else 'none'}, want {person['email']}")
            if person["email"] in emails[:3]:
                ok3 += 1
            if pattern_of(person["email"], person["name"]) == fmt:
                okpat += 1
            else:
                card.failures.append(
                    f"pattern_id {person['name']}: {person['email']} did not "
                    f"reverse-map to {fmt}")
    card.sections["format_at_1"] = _fraction(ok1, n)
    card.sections["format_at_3"] = _fraction(ok3, n)
    card.sections["pattern_id"] = _fraction(okpat, n)

    # --- reverse-mapping a confirmed address to the format it proves ---
    ok = n = 0
    for case in g.get("reverse_cases", []):
        n += 1
        got = pattern_of(case["email"], case["name"])
        if got == case["format"]:
            ok += 1
        else:
            card.failures.append(
                f"reverse_map {case['email']} ({case['name']}): got {got}, "
                f"want {case['format']}")
    card.sections["reverse_map"] = _fraction(ok, n)

    # --- does confirming one person unlock the next? ---
    ok = n = 0
    for case in g.get("lock_transfer_cases", []):
        n += 1
        domain = case["domain"]
        conf, want = case["confirm"], case["then_predict"]
        learned = pattern_of(conf["email"], conf["name"])
        if not learned:
            card.failures.append(f"lock_transfer {domain}: could not learn format "
                                 f"from {conf['email']}")
            continue
        ranked = candidates(want["name"], domain, {learned: {"hits": 1, "misses": 0}})
        if ranked and ranked[0].email == want["email"] and ranked[0].locked:
            ok += 1
        else:
            got = ranked[0].email if ranked else "none"
            card.failures.append(
                f"lock_transfer {domain}: after confirming {conf['email']}, "
                f"predicted {got}, want {want['email']}")
    card.sections["lock_transfer"] = _fraction(ok, n)

    # --- role tiering: correct label and score band ---
    ok = n = 0
    for case in g.get("title_cases", []):
        n += 1
        val, label = title_relevance(case["title"])
        lo, hi = case.get("min", 0.0), case.get("max", 1.0)
        if label == case["tier"] and lo <= val <= hi:
            ok += 1
        else:
            card.failures.append(
                f"title_tier {case['title']!r}: got ({label}, {val:.2f}), want "
                f"({case['tier']}, {lo:.2f}-{hi:.2f})")
    card.sections["title_tier"] = _fraction(ok, n)

    # --- role-inbox tiering ---
    ok = n = 0
    for case in g.get("alias_cases", []):
        n += 1
        val, label = alias_relevance(case["local"])
        lo, hi = case.get("min", 0.0), case.get("max", 1.0)
        if label == case["tier"] and lo <= val <= hi:
            ok += 1
        else:
            card.failures.append(
                f"alias_tier {case['local']!r}: got ({label}, {val:.2f}), want "
                f"({case['tier']}, {lo:.2f}-{hi:.2f})")
    card.sections["alias_tier"] = _fraction(ok, n)

    # --- pairwise ranking: does the better target outrank the worse one? ---
    ok = n = 0
    for better, worse in g.get("ranking_pairs", []):
        n += 1
        base = {"email": "a@b.test", "email_confidence": 0.4,
                "source": "careers_page", "months_old": 2}
        hi = score_contact({**base, "title": better}).score
        lo = score_contact({**base, "title": worse}).score
        if hi > lo:
            ok += 1
        else:
            card.failures.append(
                f"ranking: {better!r} ({hi:.3f}) did not outrank {worse!r} ({lo:.3f})")
    card.sections["ranking"] = _fraction(ok, n)

    # --- bounce / reply classification ---
    ok = n = 0
    for case in g.get("mail_cases", []):
        n += 1
        got = classify_bounce(case.get("subject", ""), case.get("body", "")).kind
        if got == case["expect"]:
            ok += 1
        else:
            card.failures.append(
                f"bounce {case.get('subject','')!r}: got {got}, want {case['expect']}")
    for case in g.get("reply_cases", []):
        n += 1
        got = classify_reply(case.get("from", ""), case.get("subject", ""),
                             case.get("body", ""))
        if got == case["expect"]:
            ok += 1
        else:
            card.failures.append(
                f"reply {case.get('subject','')!r}: got {got}, want {case['expect']}")
    card.sections["mail"] = _fraction(ok, n)

    # --- known limitations: reported, never scored ---
    for lim in g.get("known_limitations", []):
        card.limitations += 1
        if _limitation_passes(lim.get("probe") or {}):
            card.promotable.append(f"{lim.get('what')}: {lim.get('detail','')}")

    return card


def _limitation_passes(probe: dict) -> bool:
    """True when a documented limitation unexpectedly works now."""
    kind = probe.get("kind")
    if kind == "reverse":
        got = pattern_of(probe.get("email", ""), probe.get("name", ""))
        want = probe.get("format")
        return bool(got) if want == "any" else got == want
    if kind == "generate":
        fmt = probe.get("format")
        ranked = candidates(probe.get("name", ""), probe.get("domain", ""),
                            {fmt: {"hits": 1, "misses": 0}} if fmt else None)
        return bool(ranked) and ranked[0].email == probe.get("email")
    return False


def history(path: str = HISTORY_PATH) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def record(card: Scorecard, stamp: str, path: str = HISTORY_PATH, note: str = "") -> dict:
    """Append this run to the tracked score history."""
    entry = {"at": stamp, "overall": card.overall, "sections": card.sections,
             "failures": len(card.failures), "note": note}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def best_overall(path: str = HISTORY_PATH) -> float | None:
    runs = history(path)
    return max((r.get("overall", 0.0) for r in runs), default=None)
