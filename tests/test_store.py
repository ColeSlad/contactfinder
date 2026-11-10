from cf.normalize import contact_key
from cf.store import months_between

TODAY = "2026-11-10"


def test_contacts_dedup_on_person_and_domain(store, add_contact):
    a = add_contact("Jane Doe", "acme.com")
    b = add_contact("Doe, Jane", "acme.com")
    assert a == b
    assert len(store.contacts()) == 1


def test_same_name_at_two_companies_are_distinct(store, add_contact):
    add_contact("Jane Doe", "acme.com")
    add_contact("Jane Doe", "beta.com")
    assert len(store.contacts()) == 2


def test_merge_fills_blanks_without_erasing_known_fields(store):
    key = contact_key("Jane Doe", "acme.com")
    store.upsert_company("acme.com")
    store.upsert_contact({"key": key, "company_domain": "acme.com", "name": "Jane Doe",
                          "title": "University Recruiter", "source": "careers_page"})
    store.upsert_contact({"key": key, "company_domain": "acme.com", "name": "Jane Doe",
                          "title": "", "source_url": "https://acme.com/team"})
    got = store.contacts()[0]
    assert got["title"] == "University Recruiter"
    assert got["source_url"] == "https://acme.com/team"


def test_a_verified_email_is_never_overwritten_by_a_guess(store):
    key = contact_key("Jane Doe", "acme.com")
    store.upsert_company("acme.com")
    store.upsert_contact({"key": key, "company_domain": "acme.com", "name": "Jane Doe",
                          "email": "jdoe@acme.com", "email_pattern": "flast",
                          "email_verified": True, "email_confidence": 0.99})
    store.upsert_contact({"key": key, "company_domain": "acme.com", "name": "Jane Doe",
                          "email": "jane.doe@acme.com", "email_pattern": "first.last",
                          "email_verified": False, "email_confidence": 0.4})
    got = store.contacts()[0]
    assert got["email"] == "jdoe@acme.com"
    assert got["email_pattern"] == "flast"


def test_evidence_counts_distinct_addresses_not_outcome_rows(store, add_contact):
    cid = add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    for i in range(3):
        store.add_outcome(cid, "human_reply", "jane.doe@acme.com", "r", f"2026-10-0{i + 1}")
    assert store.evidence_for("acme.com") == {"first.last": {"hits": 1, "misses": 0}}


def test_evidence_combines_confirmations_and_outcomes(store, add_contact):
    cid = add_contact("Bob Smith", "acme.com", "bsmith@acme.com", "flast")
    store.add_outcome(cid, "human_reply", "bsmith@acme.com", "r", "2026-10-01")
    store.add_confirmation("acme.com", "flast", "other@acme.com", "Other", "manual")
    assert store.evidence_for("acme.com")["flast"]["hits"] == 2


def test_a_proven_address_is_not_also_counted_as_a_miss(store, add_contact):
    cid = add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    store.add_outcome(cid, "human_reply", "jane.doe@acme.com", "r", "2026-10-01")
    # She later leaves and the same address hard-bounces.
    store.add_outcome(cid, "hard_bounce", "jane.doe@acme.com", "550", "2026-11-01")
    ev = store.evidence_for("acme.com")["first.last"]
    assert ev == {"hits": 1, "misses": 0}


def test_neutral_outcomes_are_not_evidence(store, add_contact):
    cid = add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    for kind in ("sent", "soft_bounce", "blocked", "no_response"):
        store.add_outcome(cid, kind, "jane.doe@acme.com", "", "2026-10-01")
    assert store.evidence_for("acme.com") == {}


def test_outcomes_are_idempotent(store, add_contact):
    cid = add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    for _ in range(3):
        store.add_outcome(cid, "sent", "jane.doe@acme.com", "x", "2026-10-01")
    assert len(store.outcomes("sent")) == 1


def test_settings_round_trip(store):
    assert store.get_setting("missing", "fallback") == "fallback"
    store.set_setting("weights", {"title": 0.5})
    store.set_setting("weights", {"title": 0.6})
    assert store.get_setting("weights") == {"title": 0.6}


def test_contacts_filters(store, add_contact):
    add_contact("A B", "acme.com", title="University Recruiter")
    cid = add_contact("C D", "beta.com", title="Recruiter")
    store.db.execute("UPDATE contacts SET score=0.9 WHERE id=?", (cid,))
    store.db.commit()
    assert len(store.contacts(domain="acme.com")) == 1
    assert len(store.contacts(min_score=0.5)) == 1
    assert len(store.contacts(limit=1)) == 1


def test_status_transitions(store, add_contact):
    cid = add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    store.set_status(cid, "contacted")
    assert store.contacts()[0]["status"] == "contacted"


def test_contact_by_email_is_case_insensitive(store, add_contact):
    add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    assert store.contact_by_email("JANE.DOE@ACME.COM")["name"] == "Jane Doe"
    assert store.contact_by_email("nobody@acme.com") is None


def test_months_between():
    assert months_between("2026-11-10", "2026-11-10") == 0.0
    assert round(months_between("2026-05-10", "2026-11-10"), 1) == 6.0
    assert months_between(None) is None
    assert months_between("not-a-date") is None
    # Future dates clamp to zero rather than going negative.
    assert months_between("2027-01-01", "2026-11-10") == 0.0


def test_stats_counts(store, add_contact):
    cid = add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    store.add_outcome(cid, "sent", "jane.doe@acme.com", "", "2026-10-01")
    store.add_outcome(cid, "human_reply", "jane.doe@acme.com", "", "2026-10-02")
    st = store.stats()
    assert st["contacts"] == 1 and st["contacted"] == 1 and st["human_replies"] == 1
