import pytest

from cf.patterns import (GLOBAL_PRIOR, candidates, localpart, locked_pattern,
                         pattern_of)


@pytest.mark.parametrize("pattern,want", [
    ("first.last", "jane.doe"), ("firstlast", "janedoe"), ("flast", "jdoe"),
    ("first", "jane"), ("f.last", "j.doe"), ("first_last", "jane_doe"),
    ("firstl", "janed"), ("first.l", "jane.d"), ("last.first", "doe.jane"),
    ("lastfirst", "doejane"), ("first-last", "jane-doe"), ("last", "doe"),
    ("fl", "jd"),
])
def test_localpart(pattern, want):
    assert localpart(pattern, "Jane Doe") == want


def test_localpart_needs_a_surname_where_the_format_does():
    assert localpart("first.last", "Jane") is None
    assert localpart("first", "Jane") == "jane"


def test_no_evidence_reproduces_the_global_prior():
    got = candidates("Jane Doe", "acme.com")
    by_pattern = {c.pattern: c.confidence for c in got}
    # firstmlast is unbuildable without a middle name, so its mass is
    # redistributed; the rest must keep the prior's ordering and rough values.
    assert by_pattern["first.last"] == pytest.approx(GLOBAL_PRIOR["first.last"], abs=0.02)
    assert [c.pattern for c in got][:3] == ["first.last", "firstlast", "flast"]


def test_confidences_sum_to_one():
    assert sum(c.confidence for c in candidates("Jane Doe", "acme.com")) == \
           pytest.approx(1.0)


def test_one_confirmation_locks_the_domain():
    got = candidates("Jane Doe", "acme.com", {"flast": {"hits": 1, "misses": 0}})
    assert got[0].email == "jdoe@acme.com"
    assert got[0].locked and got[0].confidence >= 0.9


def test_lock_transfers_to_a_different_person():
    ev = {"flast": {"hits": 1, "misses": 0}}
    assert candidates("Carla Reyes", "acme.com", ev)[0].email == "creyes@acme.com"


def test_hard_bounce_demotes_a_format():
    base = candidates("Jane Doe", "acme.com")[0].confidence
    after = {c.pattern: c.confidence
             for c in candidates("Jane Doe", "acme.com",
                                 {"first.last": {"hits": 0, "misses": 2}})}
    assert after["first.last"] < base


@pytest.mark.parametrize("evidence,want", [
    ({"flast": {"hits": 1, "misses": 0}}, "flast"),
    ({"flast": {"hits": 3, "misses": 1}}, "flast"),      # one departed employee
    ({"flast": {"hits": 1, "misses": 1}}, None),         # genuinely ambiguous
    ({"flast": {"hits": 2, "misses": 1}}, None),         # conservative
    ({"a": {"hits": 2, "misses": 0}, "b": {"hits": 2, "misses": 0}}, None),  # tie
    ({"first.last": {"hits": 0, "misses": 3}}, None),
    ({}, None),
    (None, None),
])
def test_locked_pattern(evidence, want):
    assert locked_pattern(evidence) == want


@pytest.mark.parametrize("email,name,want", [
    ("jane.doe@x.test", "Jane Doe", "first.last"),
    ("jdoe@x.test", "Jane Doe", "flast"),
    ("janedoe@x.test", "Jane Doe", "firstlast"),
    ("jane@x.test", "Jane Doe", "first"),
    ("j.doe@x.test", "Jane Doe", "f.last"),
    ("doe.jane@x.test", "Jane Doe", "last.first"),
    ("mary-jane.watson@x.test", "Mary-Jane Watson", "first.last"),
    ("maryjane.watson@x.test", "Mary-Jane Watson", "first.last"),
    ("nope@x.test", "Jane Doe", None),
])
def test_pattern_of(email, name, want):
    assert pattern_of(email, name) == want


def test_pattern_of_prefers_the_likelier_format_on_collision():
    # For a one-letter first name several formats collide; the likelier wins.
    assert pattern_of("j.doe@x.test", "J Doe") in ("first.last", "f.last")


def test_unusable_input_yields_no_candidates():
    assert candidates("", "acme.com") == []
    assert candidates("Jane Doe", "") == []


def test_a_hard_bounce_removes_a_format_from_first_place():
    # Otherwise the next address tried at that company repeats a format we
    # have already proved bounces.
    ev = {"first.last": {"hits": 0, "misses": 1}}
    top = candidates("Nina Alvarez", "beta.com", ev)[0]
    assert top.pattern != "first.last"
    assert top.email == "ninaalvarez@beta.com"


def test_repeated_bounces_keep_pushing_a_format_down():
    order = []
    for misses in (0, 1, 3):
        ev = {"first.last": {"hits": 0, "misses": misses}}
        ranked = candidates("Nina Alvarez", "beta.com", ev)
        order.append(next(c.confidence for c in ranked if c.pattern == "first.last"))
    assert order[0] > order[1] > order[2]


def test_a_confirmation_still_beats_prior_bounces_elsewhere():
    # flast confirmed, first.last bounced: the lock must win outright.
    ev = {"flast": {"hits": 1, "misses": 0}, "first.last": {"hits": 0, "misses": 1}}
    top = candidates("Nina Alvarez", "beta.com", ev)[0]
    assert top.pattern == "flast" and top.locked
