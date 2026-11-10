import pytest

from cf.learn import (MIN_SENT_FOR_WEIGHTS, auc, ingest_events, learn,
                      retune_global_prior, training_rows, tune_weights)
from cf.normalize import contact_key
from cf.score import DEFAULT_WEIGHTS

TODAY = "2026-11-10"


def test_a_human_reply_confirms_the_format(store, add_contact):
    add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    rep = ingest_events(store, [{
        "kind": "reply", "address": "jane.doe@acme.com",
        "from": "Jane Doe <jane.doe@acme.com>", "subject": "Re: internship",
        "body": "Send your resume.", "date": "2026-10-03"}])
    assert rep.confirmations_added == 1
    assert store.evidence_for("acme.com") == {"first.last": {"hits": 1, "misses": 0}}
    assert store.contacts()[0]["status"] == "replied"


def test_an_auto_reply_confirms_the_format_but_is_not_a_reply(store, add_contact):
    add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    ingest_events(store, [{
        "kind": "reply", "address": "jane.doe@acme.com", "from": "jane.doe@acme.com",
        "subject": "Automatic reply: Out of office", "body": "Back Dec 1.",
        "date": "2026-10-03"}])
    assert store.evidence_for("acme.com")["first.last"]["hits"] == 1
    assert store.stats()["human_replies"] == 0
    assert store.contacts()[0]["status"] != "replied"


def test_a_hard_bounce_counts_against_the_format(store, add_contact):
    add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    ingest_events(store, [{
        "kind": "bounce", "address": "jane.doe@acme.com",
        "subject": "Undelivered Mail Returned to Sender",
        "body": "550 5.1.1 user unknown", "date": "2026-10-04"}])
    assert store.evidence_for("acme.com") == {"first.last": {"hits": 0, "misses": 1}}
    assert store.contacts()[0]["status"] == "bounced"


def test_a_spam_block_does_not_count_against_the_format(store, add_contact):
    add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    ingest_events(store, [{
        "kind": "bounce", "address": "jane.doe@acme.com",
        "subject": "Delivery Status Notification (Failure)",
        "body": "550 5.7.1 rejected due to spam content", "date": "2026-10-04"}])
    assert store.evidence_for("acme.com") == {}
    assert store.contacts()[0]["status"] != "bounced"


def test_a_full_mailbox_does_not_count_against_the_format(store, add_contact):
    add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    ingest_events(store, [{
        "kind": "bounce", "address": "jane.doe@acme.com", "subject": "Undeliverable",
        "body": "452 4.2.2 mailbox full", "date": "2026-10-04"}])
    assert store.evidence_for("acme.com") == {}


def test_one_person_replying_repeatedly_is_one_confirmation(store, add_contact):
    add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    ev = [{"kind": "reply", "address": "jane.doe@acme.com", "from": "jane.doe@acme.com",
           "subject": "Re: internship", "body": "Hi", "date": f"2026-10-0{i}"}
          for i in range(1, 4)]
    rep = ingest_events(store, ev)
    assert rep.confirmations_added == 1
    assert store.evidence_for("acme.com")["first.last"]["hits"] == 1


def test_events_for_unknown_addresses_are_noted_not_dropped_silently(store):
    rep = ingest_events(store, [{"kind": "reply", "address": "ghost@nowhere.test",
                                 "from": "ghost@nowhere.test", "subject": "Re: hi",
                                 "body": "hello"}])
    assert any("ghost@nowhere.test" in n for n in rep.notes)


def test_sent_events_mark_contacts_as_contacted(store, add_contact):
    add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    ingest_events(store, [{"kind": "sent", "address": "jane.doe@acme.com",
                           "date": "2026-10-01"}])
    assert store.contacts()[0]["status"] == "contacted"


def test_auc():
    assert auc([1.0], [0.0]) == 1.0
    assert auc([0.0], [1.0]) == 0.0
    assert auc([0.5], [0.5]) == 0.5
    assert auc([], [1.0]) == 0.5
    assert auc([1.0, 0.0], [0.5, 0.5]) == 0.5


def test_prior_retuning_is_gated_on_distinct_domains(store):
    for i in range(3):
        store.add_confirmation(f"co{i}.test", "flast", f"a@co{i}.test", "A B", "manual")
    assert retune_global_prior(store) is None


def test_prior_retuning_shifts_toward_observed_formats(store):
    for i in range(8):
        store.add_confirmation(f"co{i}.test", "flast", f"a@co{i}.test", "A B", "manual")
    from cf.patterns import GLOBAL_PRIOR
    new = retune_global_prior(store)
    assert new is not None
    assert new["flast"] > GLOBAL_PRIOR["flast"]
    assert sum(new.values()) == pytest.approx(1.0, abs=1e-3)


def test_prior_retuning_counts_one_vote_per_domain(store):
    # One heavily-mined company must not dominate the global prior.
    for i in range(20):
        store.add_confirmation("big.test", "lastfirst", f"a{i}@big.test", "A B", "manual")
    for i in range(6):
        store.add_confirmation(f"co{i}.test", "first.last", f"a@co{i}.test", "A B", "manual")
    new = retune_global_prior(store)
    assert new["first.last"] > new["lastfirst"]


def _populate(store, groups, rounds=15):
    n = 0
    for i in range(rounds):
        for title, source, label in groups:
            n += 1
            dom = f"co{i}.test"
            store.upsert_company(dom)
            cid = store.upsert_contact({
                "key": contact_key(f"P{n} L{n}", dom), "company_domain": dom,
                "name": f"P{n} L{n}", "title": title, "email": f"p{n}@{dom}",
                "email_pattern": "first.last", "email_confidence": 0.4,
                "source": source, "found_at": "2026-10-01"})
            store.add_outcome(cid, "sent", f"p{n}@{dom}", "", "2026-10-15")
            if label:
                store.add_outcome(cid, "human_reply", f"p{n}@{dom}", "", "2026-10-16")
    return n


def test_weight_tuning_is_gated_on_sample_size(store, add_contact):
    cid = add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    store.add_outcome(cid, "sent", "jane.doe@acme.com", "", "2026-10-01")
    store.add_outcome(cid, "human_reply", "jane.doe@acme.com", "", "2026-10-02")
    weights, reason, _ = tune_weights(store, today=TODAY)
    assert weights is None and "need" in reason


def test_weight_tuning_needs_both_classes(store):
    _populate(store, [("University Recruiter", "careers_page", 1)], rounds=30)
    weights, reason, _ = tune_weights(store, today=TODAY)
    assert weights is None and "non-replies" in reason


def test_weight_tuning_recovers_a_signal_the_defaults_get_backwards(store):
    # Repliers have LOW role relevance but HIGH source quality; non-repliers the
    # reverse. Default weights rank this backwards, so a working tuner must
    # demote `title` and promote `source`.
    _populate(store, [("Senior Software Engineer", "company_directory", 1),
                      ("University Recruiter", "social_profile", 0)])
    weights, before, after = tune_weights(store, today=TODAY)
    assert weights is not None
    assert after > before
    assert weights["title"] < DEFAULT_WEIGHTS["title"]
    assert weights["source"] > DEFAULT_WEIGHTS["source"]
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_training_rows_only_include_contacted_people(store, add_contact):
    add_contact("Never Contacted", "acme.com", "a@acme.com", "first")
    cid = add_contact("Was Contacted", "beta.com", "b@beta.com", "first")
    store.add_outcome(cid, "sent", "b@beta.com", "", "2026-10-01")
    assert len(training_rows(store, TODAY)) == 1


def test_learn_reports_why_it_did_nothing(store, add_contact):
    add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    rep = learn(store, [], today=TODAY)
    assert not rep.weights_updated and not rep.prior_updated
    assert any("distinct domains" in n for n in rep.notes)
    assert any("weights not retuned" in n for n in rep.notes)


def test_learn_reports_locked_domains(store, add_contact):
    add_contact("Jane Doe", "acme.com", "jane.doe@acme.com", "first.last")
    rep = learn(store, [{"kind": "reply", "address": "jane.doe@acme.com",
                         "from": "jane.doe@acme.com", "subject": "Re: hi",
                         "body": "Send your resume."}], today=TODAY)
    assert "acme.com=first.last" in rep.domains_locked


def test_tuned_weights_persist_and_take_effect(store):
    _populate(store, [("Senior Software Engineer", "company_directory", 1),
                      ("University Recruiter", "social_profile", 0)])
    learn(store, [], today=TODAY)
    saved = store.get_setting("weights")
    assert saved and saved["title"] < DEFAULT_WEIGHTS["title"]
