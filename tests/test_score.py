import pytest

from cf.score import (DEFAULT_WEIGHTS, rank, recency, score_contact,
                      seniority_fit, title_relevance)

BASE = {"email": "a@b.test", "email_confidence": 0.4,
        "source": "careers_page", "months_old": 2}


@pytest.mark.parametrize("title,tier", [
    ("University Recruiter", "university-recruiting"),
    ("Campus Recruiting Coordinator", "university-recruiting"),
    ("Univ. Recruiting Partner", "university-recruiting"),
    ("Recruiter (Campus)", "university-recruiting"),
    ("Sr. Recruiter, University Programs", "university-recruiting"),
    ("Early Career Talent Partner", "early-career"),
    ("Early-Career Programs Lead", "early-career"),
    ("Manager, Emerging Talent", "early-career"),
    ("Student Programs Specialist", "intern-program"),
    ("Intern & New Grad Hiring", "intern-program"),
    ("Technical Recruiter, Engineering", "technical-recruiting"),
    ("Technical Sourcer, University", "university-sourcing"),
    ("Talent Sourcer", "sourcing"),
    ("Recruiting Coordinator", "recruiting-coordinator"),
    ("People Partner", "people-ops"),
    ("HR Generalist", "people-ops"),
    ("Chief People Officer", "people-leadership"),
    ("Engineering Manager", "eng-hiring-manager"),
    ("Senior Software Engineer", "unrelated"),
    ("", "unknown"),
])
def test_title_tiers(title, tier):
    assert title_relevance(title)[1] == tier


def test_recruiting_word_forms_all_match():
    # A trailing \b after 'recruit' silently broke 'recruiter'/'recruiting'.
    for t in ["Campus Recruiter", "Campus Recruiting", "Campus Recruitment"]:
        assert title_relevance(t)[0] >= 0.9, t


@pytest.mark.parametrize("title,tier", [
    ("Chief People Officer", "c-level"),
    ("SVP, Talent", "svp"),
    ("VP of Talent Acquisition", "vp"),
    ("Head of University Recruiting", "director"),
    ("Director, Campus Recruiting", "director"),
    ("Senior Manager, Recruiting", "senior-manager"),
    ("Recruiting Manager", "manager"),
    ("Senior Technical Recruiter", "senior-ic"),
    ("Recruiting Coordinator", "ic"),
])
def test_seniority_tiers(title, tier):
    assert seniority_fit(title)[1] == tier


def test_role_and_seniority_are_independent_axes():
    # A campus coordinator is a strong target; a VP of the same function is not.
    coord = score_contact({**BASE, "title": "Campus Recruiting Coordinator"})
    vp = score_contact({**BASE, "title": "VP of Campus Recruiting"})
    assert coord.score > vp.score
    assert coord.role_tier == vp.role_tier == "university-recruiting"
    assert "too_senior_for_cold_email" in vp.flags
    assert "too_senior_for_cold_email" not in coord.flags


def test_recency_decays_and_has_a_floor():
    assert recency(0) == 1.0
    assert recency(12) == pytest.approx(0.5)
    assert recency(None) == 0.5
    assert recency(600) == pytest.approx(0.15)
    assert recency(-5) == 1.0


def test_score_is_bounded():
    best = score_contact({"title": "University Recruiter", "email": "a@b.test",
                          "email_confidence": 1.0, "source": "company_directory",
                          "months_old": 0})
    worst = score_contact({"title": "", "email": "", "email_confidence": 0.0,
                           "source": "inferred", "months_old": 999})
    assert 0.0 <= worst.score < best.score <= 1.0


@pytest.mark.parametrize("flag,contact", [
    ("no_email", {"title": "University Recruiter", "email": ""}),
    ("low_email_confidence", {"title": "X", "email": "a@b.test", "email_confidence": 0.1}),
    ("stale_source", {"title": "X", "email": "a@b.test", "months_old": 30}),
    ("too_senior_for_cold_email", {"title": "Chief People Officer", "email": "a@b.test"}),
    ("off_target_role", {"title": "Senior Software Engineer", "email": "a@b.test"}),
])
def test_flags(flag, contact):
    assert flag in score_contact(contact).flags


def test_weights_change_the_ordering():
    rows = [{"title": "Senior Software Engineer", "email": "a@b.test",
             "email_confidence": 1.0, "source": "company_directory", "months_old": 0},
            {"title": "University Recruiter", "email": "b@b.test",
             "email_confidence": 0.1, "source": "social_profile", "months_old": 20}]
    by_title = rank(rows, {**DEFAULT_WEIGHTS, "title": 10.0})
    by_conf = rank(rows, {**DEFAULT_WEIGHTS, "title": 0.0, "email_confidence": 10.0})
    assert by_title[0]["title"] == "University Recruiter"
    assert by_conf[0]["title"] == "Senior Software Engineer"


def test_unknown_source_is_treated_as_weak():
    known = score_contact({**BASE, "title": "X", "source": "company_directory"})
    unknown = score_contact({**BASE, "title": "X", "source": "nonsense-source"})
    assert unknown.score < known.score
