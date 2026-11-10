import json

from cf.evaluate import (SECTION_WEIGHTS, Scorecard, best_overall, evaluate,
                         history, record)


def test_shipped_golden_set_passes_cleanly():
    card = evaluate("eval/golden.yaml")
    assert card.failures == [], f"golden set regressions: {card.failures}"
    assert card.overall == 1.0


def test_every_section_is_exercised_and_weighted():
    card = evaluate("eval/golden.yaml")
    assert set(card.sections) == set(SECTION_WEIGHTS)
    assert sum(SECTION_WEIGHTS.values()) == 1.0


def test_known_limitations_are_reported_but_not_scored():
    card = evaluate("eval/golden.yaml")
    assert card.limitations > 0
    # None currently pass; if one starts to, it is flagged for promotion
    # rather than quietly inflating the score.
    assert card.promotable == []
    assert "lock_transfer" in card.sections


def test_overall_is_a_weighted_mean():
    card = Scorecard(sections={"lock_transfer": 1.0, "mail": 0.0})
    expected = SECTION_WEIGHTS["lock_transfer"] / (
        SECTION_WEIGHTS["lock_transfer"] + SECTION_WEIGHTS["mail"])
    assert card.overall == round(expected, 4)


def test_empty_scorecard_does_not_divide_by_zero():
    assert Scorecard().overall == 0.0


def test_missing_golden_file_yields_a_zero_not_a_crash():
    card = evaluate("eval/does-not-exist.yaml")
    assert card.overall == 0.0


def test_history_round_trip(tmp_path):
    path = tmp_path / "history.jsonl"
    assert history(str(path)) == []
    assert best_overall(str(path)) is None
    record(Scorecard(sections={"mail": 0.5}), "2026-11-10", str(path), "first")
    record(Scorecard(sections={"mail": 1.0}), "2026-11-11", str(path), "second")
    runs = history(str(path))
    assert len(runs) == 2
    assert best_overall(str(path)) == 1.0
    assert runs[0]["note"] == "first"


def test_history_tolerates_a_corrupt_line(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text('{"overall": 0.5}\nnot json\n{"overall": 0.9}\n')
    assert len(history(str(path))) == 2
    assert best_overall(str(path)) == 0.9


def test_render_is_readable(tmp_path):
    out = evaluate("eval/golden.yaml").render()
    assert "OVERALL" in out and "lock_transfer" in out
