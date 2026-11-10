"""Apollo adapter tests.

All pure/offline: HTTP is stubbed, so these run with no key and no network.
"""
import json

import pytest

from cf import apollo
from cf.apollo import (BATCH_LIMIT, DEFAULT_SENIORITIES, DEFAULT_TITLES,
                       ApolloReport, enabled, enrich, find_contacts, is_masked,
                       search_people, to_candidates, worth_enriching)

# Shapes recorded from the documented API responses.
SEARCH_RESPONSE = {
    "total_entries": 3,
    "people": [
        {"id": "p1", "first_name": "Jane", "last_name_obfuscated": "Do***e",
         "title": "University Recruiter", "has_email": True,
         "organization": {"name": "Acme"}},
        {"id": "p2", "first_name": "Sam", "last_name_obfuscated": "N***g",
         "title": "Senior Software Engineer", "has_email": True,
         "organization": {"name": "Acme"}},
        {"id": "p3", "first_name": "Bob", "last_name_obfuscated": "Sm***h",
         "title": "Campus Recruiting Coordinator", "has_email": False,
         "organization": {"name": "Acme"}},
    ],
}

ENRICH_RESPONSE = {
    "status": "success",
    "credits_consumed": 1,
    "matches": [
        {"id": "p1", "first_name": "Jane", "last_name": "Doe", "name": "Jane Doe",
         "email": "jdoe@acme.com", "email_status": "verified",
         "title": "University Recruiter", "match_confidence": "high",
         "linkedin_url": "https://linkedin.com/in/janedoe",
         "organization_name": "Acme"},
    ],
}


@pytest.fixture
def stub_post(monkeypatch):
    """Record calls and return canned responses, keyed by endpoint path."""
    calls = []

    def _fake(path, payload):
        calls.append((path, payload))
        if path == apollo.SEARCH_PATH:
            return json.loads(json.dumps(SEARCH_RESPONSE))
        if path == apollo.ENRICH_PATH:
            return json.loads(json.dumps(ENRICH_RESPONSE))
        return {"error": f"unexpected path {path}"}

    monkeypatch.setattr(apollo, "_post", _fake)
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    return calls


# ---------------------------------------------------------------- masked names
@pytest.mark.parametrize("name,masked", [
    ("Do***e", True), ("N***g", True), ("*", True),
    ("Doe", False), ("", False), (None, False),
])
def test_is_masked(name, masked):
    assert is_masked(name) is masked


def test_a_masked_surname_would_slug_to_a_plausible_lie():
    # This is why masked names are refused rather than cleaned: the asterisks
    # vanish and 'Do***e' becomes 'doe', which could be Doyle, Dodge, Doke...
    from cf.normalize import slug
    assert slug("Do***e") == "doe"


def test_masked_names_never_become_candidates():
    matches = [{"name": "Jane Do***e", "email": "x@acme.com",
                "email_status": "verified", "match_confidence": "high"}]
    rep = ApolloReport()
    assert to_candidates(matches, "acme.com", rep) == []
    assert rep.masked == 1


def test_pipeline_refuses_obfuscated_names(store):
    from cf.config import DEFAULT_CONFIG
    from cf.pipeline import ingest_candidates
    rep = ingest_candidates(store, [
        {"name": "Jane Do***e", "title": "University Recruiter", "domain": "acme.com"},
        {"name": "Jane Doe", "title": "University Recruiter", "domain": "acme.com"},
    ], DEFAULT_CONFIG, "2026-11-10")
    assert rep.added == 1
    assert any("obfuscated" in s for s in rep.skipped)
    assert store.contacts()[0]["name"] == "Jane Doe"


# ------------------------------------------------------------------ credit gate
@pytest.mark.parametrize("title,keep", [
    ("University Recruiter", True),
    ("Campus Recruiting Coordinator", True),
    ("Technical Recruiter", True),
    ("Talent Sourcer", True),
    ("Senior Software Engineer", False),
    ("", False),
])
def test_worth_enriching_gates_on_role(title, keep):
    assert worth_enriching({"apollo_id": "p1", "title": title}) is keep


def test_worth_enriching_needs_an_apollo_id():
    assert not worth_enriching({"title": "University Recruiter"})


def test_min_title_score_is_adjustable():
    stub = {"apollo_id": "p1", "title": "Talent Sourcer"}
    assert worth_enriching(stub, min_title_score=0.40)
    assert not worth_enriching(stub, min_title_score=0.90)


# --------------------------------------------------------------------- search
def test_search_shapes_stubs_and_flags_masking(stub_post):
    res = search_people("acme.com")
    assert res["total"] == 3
    first = res["stubs"][0]
    assert first["apollo_id"] == "p1"
    assert first["last_name"] == "Do***e" and first["last_masked"] is True
    assert first["has_email"] is True


def test_search_sends_required_parameters(stub_post):
    search_people("acme.com", seniorities=["manager"], per_page=10)
    path, payload = stub_post[0]
    assert path == apollo.SEARCH_PATH
    # person_titles is required by the API.
    assert payload["person_titles"] == DEFAULT_TITLES
    assert payload["q_organization_domains_list"] == ["acme.com"]
    assert payload["person_seniorities"] == ["manager"]
    assert payload["per_page"] == 10


def test_search_clamps_per_page(stub_post):
    search_people("acme.com", per_page=5000)
    assert stub_post[0][1]["per_page"] == apollo.MAX_PER_PAGE


def test_default_seniorities_exclude_unreachable_levels():
    for level in ("c_suite", "owner", "founder", "partner", "vp"):
        assert level not in DEFAULT_SENIORITIES


# ------------------------------------------------------------------ enrichment
def test_enrich_batches_at_the_api_limit(stub_post):
    stubs = [{"apollo_id": f"p{i}", "domain": "acme.com"} for i in range(23)]
    _, _, errors = enrich(stubs)
    assert errors == []
    assert len(stub_post) == 3                      # 10 + 10 + 3
    assert all(len(p["details"]) <= BATCH_LIMIT for _, p in stub_post)


def test_enrich_accumulates_credits(stub_post):
    stubs = [{"apollo_id": f"p{i}", "domain": "acme.com"} for i in range(11)]
    _, credits, _ = enrich(stubs)
    assert credits == 2                             # 1 per batch in the fixture


@pytest.mark.parametrize("status,source,verified", [
    ("verified", "enrichment_verified", True),
    ("unknown", "enrichment_unverified", False),
])
def test_email_status_maps_to_source_and_verification(status, source, verified):
    matches = [{"name": "Jane Doe", "email": "jdoe@acme.com", "email_status": status,
                "match_confidence": "high", "title": "University Recruiter"}]
    got = to_candidates(matches, "acme.com")[0]
    assert got["source"] == source
    assert got["email_verified"] is verified


def test_an_invalid_address_is_discarded_not_ingested():
    rep = ApolloReport()
    matches = [{"name": "Jane Doe", "email": "jdoe@acme.com",
                "email_status": "invalid", "match_confidence": "high"}]
    assert to_candidates(matches, "acme.com", rep) == []
    assert rep.invalid_email == 1


def test_unmatched_records_are_counted_not_ingested():
    rep = ApolloReport()
    matches = [None,
               {"name": "X Y", "match_confidence": "none"},
               {"email": "a@b.com", "match_confidence": "high"}]
    assert to_candidates(matches, "acme.com", rep) == []
    assert rep.unmatched == 3


def test_placeholder_addresses_are_dropped_but_the_person_is_kept():
    matches = [{"name": "Jane Doe", "email": "email_not_unlocked@domain.com",
                "email_status": "unknown", "match_confidence": "high",
                "title": "University Recruiter"}]
    got = to_candidates(matches, "acme.com")[0]
    assert "email" not in got
    assert got["source"] == "inferred"


def test_name_is_assembled_when_the_name_field_is_absent():
    matches = [{"first_name": "Jane", "last_name": "Doe", "email": "j@acme.com",
                "email_status": "verified", "match_confidence": "high"}]
    assert to_candidates(matches, "acme.com")[0]["name"] == "Jane Doe"


# --------------------------------------------------------------- orchestration
def test_find_contacts_gates_credits_before_enriching(stub_post):
    cands, rep = find_contacts("acme.com")
    assert rep.searched == 3
    assert rep.skipped_low_value == 1               # the software engineer
    assert rep.skipped_no_email == 1                # Bob, has_email False
    assert len(cands) == 1 and cands[0]["name"] == "Jane Doe"
    # Exactly one search plus one enrichment call for the single survivor.
    assert [p for p, _ in stub_post] == [apollo.SEARCH_PATH, apollo.ENRICH_PATH]
    assert len(stub_post[1][1]["details"]) == 1


def test_dry_run_spends_no_credits(stub_post):
    cands, rep = find_contacts("acme.com", dry_run=True)
    assert rep.credits == 0
    assert [p for p, _ in stub_post] == [apollo.SEARCH_PATH]
    assert cands[0]["masked"] is True


def test_a_verified_apollo_address_locks_the_company_format(store, stub_post):
    # Apollo reports no company format, so we derive it: a verified address
    # plus a real name reverse-maps and locks the domain.
    from cf.config import DEFAULT_CONFIG
    from cf.patterns import locked_pattern
    from cf.pipeline import ingest_candidates
    cands, _ = find_contacts("acme.com")
    ingest_candidates(store, cands, DEFAULT_CONFIG, "2026-11-10")
    assert store.evidence_for("acme.com")["flast"]["hits"] == 1
    assert locked_pattern(store.evidence_for("acme.com")) == "flast"


# ---------------------------------------------------------------- degradation
def test_disabled_without_a_key(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    assert not enabled()
    assert search_people("acme.com") is None
    cands, rep = find_contacts("acme.com")
    assert cands == [] and any("APOLLO_API_KEY" in e for e in rep.errors)


def test_api_errors_surface_rather_than_crash(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    monkeypatch.setattr(apollo, "_post",
                        lambda p, d: {"error": "403 from Apollo. People Search needs "
                                               "a master API key."})
    cands, rep = find_contacts("acme.com")
    assert cands == []
    assert any("master API key" in e for e in rep.errors)


def test_report_summary_is_readable(stub_post):
    _, rep = find_contacts("acme.com")
    out = rep.summary()
    assert "credits" in out and "off-target role" in out


# ------------------------------------------------------- plan / error handling
def test_plan_errors_surface_apollos_own_explanation():
    from cf.apollo import _error_message
    body = json.dumps({
        "error": "The api/v1/mixed_people/api_search API is not included in your "
                 "Free plan and is not accessible, even with a master key.",
        "error_code": "API_INACCESSIBLE",
        "error_details": {"message": "Your team is not permitted to call this endpoint."},
    })
    msg = _error_message(body, 403)
    # The structured field wins over the deprecated flat one...
    assert "not permitted to call this endpoint" in msg
    # ...and the cause must be stated as a plan limit, not a key-permissions
    # problem: Apollo refuses these endpoints even with a master key.
    assert "paid Apollo plan" in msg
    assert "master" not in msg.lower()


def test_error_details_is_preferred_over_the_deprecated_error_field():
    from cf.apollo import _error_message
    body = json.dumps({"error": "old flat message",
                       "error_details": {"message": "structured message"}})
    assert "structured message" in _error_message(body, 400)


def test_error_message_falls_back_when_body_is_not_json():
    from cf.apollo import _error_message
    assert "HTTP 500" in _error_message("<html>gateway</html>", 500)
    assert "HTTP 500" in _error_message("", 500)


@pytest.mark.parametrize("code,needle", [
    (401, "rejected"), (429, "rate limited"), (403, "paid Apollo plan"),
])
def test_status_codes_get_actionable_messages(code, needle):
    from cf.apollo import _error_message
    assert needle in _error_message(json.dumps({"error_details": {"message": "x"}}), code)


def test_check_access_reports_per_endpoint(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    allowed = {"organizations/enrich"}
    monkeypatch.setattr(apollo, "_post",
                        lambda path, payload: {"organization": {}} if path in allowed
                        else {"error": "needs a paid Apollo plan"})
    rows = apollo.check_access()
    assert [ok for _, ok, _ in rows] == [False, False, True]
    assert all("paid Apollo plan" in d for _, ok, d in rows if not ok)


def test_check_access_probes_cannot_match_so_cost_nothing():
    # Credits are charged only when data is found, so the probe person must be
    # guaranteed not to match.
    for label, _, payload in apollo.PROBE_ENDPOINTS:
        for d in payload.get("details", []):
            assert d["domain"] == "example.com"
            assert d["last_name"] == "Vmpwq"


def test_check_access_without_a_key(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    assert all(not ok and "APOLLO_API_KEY" in d for _, ok, d in apollo.check_access())
