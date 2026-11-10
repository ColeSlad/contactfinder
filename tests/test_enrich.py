import pytest

from cf.enrich import (domain_search, enabled, find_email,
                       hunter_format_to_pattern, to_candidates, verify)


@pytest.mark.parametrize("fmt,want", [
    ("{first}.{last}", "first.last"),
    ("{f}{last}", "flast"),
    ("{first}{last}", "firstlast"),
    ("{first}_{last}", "first_last"),
    ("{f}.{last}", "f.last"),
    ("{last}.{first}", "last.first"),
    ("{first}-{last}", "first-last"),
    ("{FIRST}.{LAST}", "first.last"),
    ("{first} . {last}", "first.last"),
    ("{unknown}", None),
    ("", None),
    (None, None),
])
def test_hunter_format_mapping(fmt, want):
    assert hunter_format_to_pattern(fmt) == want


def test_mapped_patterns_all_exist_in_the_catalog():
    from cf.enrich import HUNTER_FORMATS
    from cf.patterns import BUILDERS
    assert set(HUNTER_FORMATS.values()) <= set(BUILDERS)


def test_disabled_without_a_key(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    assert not enabled()
    # Every call must degrade to None rather than raising or hanging.
    assert domain_search("acme.com") is None
    assert find_email("acme.com", "Jane", "Doe") is None
    assert verify("jane@acme.com") is None


def test_to_candidates_shapes_rows_for_the_pipeline():
    res = {"pattern": "flast", "emails": [
        {"email": "jdoe@acme.com", "first": "Jane", "last": "Doe",
         "title": "University Recruiter", "source": "enrichment_verified"},
        {"email": "x@acme.com", "first": None, "last": None, "title": "",
         "source": "enrichment_unverified"},
        {"email": None, "first": "No", "last": "Email", "title": "",
         "source": "enrichment_unverified"},
    ]}
    got = to_candidates(res, "acme.com")
    assert len(got) == 1
    assert got[0]["name"] == "Jane Doe"
    assert got[0]["email_verified"] is True
    assert got[0]["domain"] == "acme.com"


def test_to_candidates_handles_empty_input():
    assert to_candidates({}, "acme.com") == []
    assert to_candidates(None, "acme.com") == []
