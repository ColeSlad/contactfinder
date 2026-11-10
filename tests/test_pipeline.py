import pytest

from cf.config import DEFAULT_CONFIG, load_config
from cf.pipeline import ingest_candidates, queue, rescore_all

TODAY = "2026-11-10"
CFG = {**DEFAULT_CONFIG, "run": {"min_score": 0.45, "max_contacts_per_company": 2}}


def _c(name, domain, **kw):
    base = {"name": name, "domain": domain, "title": "University Recruiter",
            "source": "careers_page", "found_at": "2026-11-01"}
    return {**base, **kw}


def test_ingest_infers_an_address_and_scores(store):
    rep = ingest_candidates(store, [_c("Jane Doe", "acme.com")], CFG, TODAY)
    assert rep.added == 1
    got = store.contacts()[0]
    assert got["email"] == "jane.doe@acme.com"
    assert got["email_pattern"] == "first.last"
    assert got["score"] > 0
    assert got["role_tier"] == "university-recruiting"


def test_ingest_skips_rows_without_a_name_or_domain(store):
    rep = ingest_candidates(store, [
        _c("", "acme.com"), _c("Jane Doe", ""), _c("Valid Person", "acme.com")], CFG, TODAY)
    assert rep.added == 1 and len(rep.skipped) == 2


def test_ingest_is_idempotent(store):
    ingest_candidates(store, [_c("Jane Doe", "acme.com")], CFG, TODAY)
    rep = ingest_candidates(store, [_c("Jane Doe", "acme.com")], CFG, TODAY)
    assert rep.added == 0 and rep.updated == 1
    assert len(store.contacts()) == 1


def test_a_supplied_address_beats_inference(store):
    ingest_candidates(store, [_c("Jane Doe", "acme.com", email="jd@acme.com")], CFG, TODAY)
    got = store.contacts()[0]
    assert got["email"] == "jd@acme.com"
    assert got["email_confidence"] >= 0.85


def test_a_verified_address_confirms_the_company_format(store):
    ingest_candidates(store, [
        _c("Jane Doe", "acme.com", email="jdoe@acme.com", email_verified=True)], CFG, TODAY)
    assert store.evidence_for("acme.com")["flast"]["hits"] == 1
    assert "acme.com" in store.db.execute(
        "SELECT domain FROM confirmations").fetchone()[0]


def test_a_confirmed_format_propagates_to_everyone_else(store):
    ingest_candidates(store, [_c("Jane Doe", "acme.com"),
                              _c("Bob Smith", "acme.com")], CFG, TODAY)
    assert store.contact_by_email("bob.smith@acme.com") is not None

    store.add_confirmation("acme.com", "flast", "jdoe@acme.com", "Jane Doe", "manual")
    rescore_all(store, CFG, TODAY)

    assert store.contact_by_email("bsmith@acme.com") is not None
    bob = store.contact_by_email("bsmith@acme.com")
    assert bob["email_confidence"] >= 0.9


def test_rescore_leaves_verified_addresses_alone(store):
    ingest_candidates(store, [
        _c("Jane Doe", "acme.com", email="jane.doe@acme.com", email_verified=True)],
        CFG, TODAY)
    # Contradictory evidence arrives; the verified address must not be rewritten.
    store.add_confirmation("acme.com", "lastfirst", "x@acme.com", "X Y", "manual")
    rescore_all(store, CFG, TODAY)
    assert store.contacts()[0]["email"] == "jane.doe@acme.com"


def test_queue_respects_the_per_company_cap(store):
    cands = [_c(f"Person{i} Last{i}", "acme.com") for i in range(5)]
    ingest_candidates(store, cands, CFG, TODAY)
    assert len(queue(store, CFG)) == 2


def test_queue_respects_the_minimum_score(store):
    # Both roles are on-target, so only min_score can separate them.
    strict = {**CFG, "run": {**CFG["run"], "min_score": 0.75}}
    ingest_candidates(store, [
        _c("Strong Match", "acme.com", title="University Recruiter"),
        _c("Weaker Match", "beta.com", title="Technical Recruiter")], strict, TODAY)
    names = [c["name"] for c in queue(store, strict)]
    assert names == ["Strong Match"]


def test_queue_excludes_people_already_handled(store):
    ingest_candidates(store, [_c("Jane Doe", "acme.com")], CFG, TODAY)
    cid = store.contacts()[0]["id"]
    assert len(queue(store, CFG)) == 1
    store.set_status(cid, "contacted")
    assert len(queue(store, CFG)) == 0


def test_queue_is_ordered_best_first(store):
    ingest_candidates(store, [
        _c("Good Person", "acme.com", title="University Recruiter"),
        _c("Okay Person", "beta.com", title="Technical Recruiter")], CFG, TODAY)
    rows = queue(store, CFG)
    assert rows[0]["name"] == "Good Person"
    assert rows[0]["score"] > rows[1]["score"]


def test_config_pinned_format_is_used_immediately(store):
    cfg = {**CFG, "known_formats": {"acme.com": "flast"}}
    ingest_candidates(store, [_c("Jane Doe", "acme.com")], cfg, TODAY)
    got = store.contacts()[0]
    assert got["email"] == "jdoe@acme.com"
    assert got["email_confidence"] >= 0.9


def test_ingest_registers_the_company(store):
    ingest_candidates(store, [_c("Jane Doe", "newco.com")], CFG, TODAY)
    assert any(c["domain"] == "newco.com" for c in store.companies())


def test_messy_domain_input_is_normalised(store):
    ingest_candidates(store, [_c("Jane Doe", "https://www.Acme.com/careers")], CFG, TODAY)
    assert store.contacts()[0]["company_domain"] == "acme.com"


def test_shipped_config_loads():
    cfg = load_config("config.yaml")
    assert "run" in cfg and "min_score" in cfg["run"]
    assert isinstance(cfg["known_formats"], dict)


def test_queue_hard_excludes_off_target_roles(store):
    # An engineer with a pristine source and fresh date still clears min_score
    # on the weighted mean alone, so role relevance has to be a hard gate.
    ingest_candidates(store, [_c("Eng Person", "acme.com",
                                 title="Senior Software Engineer",
                                 source="company_directory", found_at=TODAY)], CFG, TODAY)
    scored = store.contacts()[0]
    assert scored["score"] > CFG["run"]["min_score"]
    assert "off_target_role" in scored["flags"]
    assert queue(store, CFG) == []


def test_exclude_flags_are_configurable(store):
    ingest_candidates(store, [_c("Eng Person", "acme.com",
                                 title="Senior Software Engineer",
                                 source="company_directory", found_at=TODAY)], CFG, TODAY)
    permissive = {**CFG, "run": {**CFG["run"], "exclude_flags": []}}
    assert len(queue(store, permissive)) == 1
