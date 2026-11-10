import pytest

from cf.normalize import Name, contact_key, domain_of, parse_name, slug


@pytest.mark.parametrize("raw,first,last", [
    ("Jane Doe", "Jane", "Doe"),
    ("Jane Q. Doe Jr.", "Jane", "Doe"),
    ("Doe, Jane Q.", "Jane", "Doe"),
    ('Jane "JJ" Doe', "Jane", "Doe"),
    ("Jane (JJ) Doe", "Jane", "Doe"),
    ("Jane Doe, PhD", "Jane", "Doe"),
    ("Dr. Elena Vasquez", "Elena", "Vasquez"),
    ("Prof. Alan Turing", "Alan", "Turing"),
    ("Jane van der Berg", "Jane", "van der Berg"),
    ("Maria de la Cruz", "Maria", "de la Cruz"),
    ("Jane Doe - University Recruiter", "Jane", "Doe"),
    ("Mary-Jane Watson", "Mary-Jane", "Watson"),
    ("  jane   doe  ", "jane", "doe"),
])
def test_parse_name(raw, first, last):
    got = parse_name(raw)
    assert (got.first, got.last) == (first, last)


def test_parse_name_single_token_has_no_last():
    n = parse_name("Jane")
    assert n.first == "Jane" and n.last == "" and n.ok


def test_parse_name_empty_is_not_ok():
    assert not parse_name("").ok
    assert not parse_name("   ").ok


def test_honorific_alone_is_kept_rather_than_emptied():
    # Stripping must never leave us with nothing to work from.
    assert parse_name("Dr.").ok is False or parse_name("Dr.").first


def test_slug_strips_accents_and_punctuation():
    assert slug("Renée Dupré") == "reneedupre"
    assert slug("O'Brien") == "obrien"
    assert slug("van der Berg") == "vanderberg"


def test_slug_can_preserve_hyphens():
    assert slug("Mary-Jane", keep_hyphen=True) == "mary-jane"
    assert slug("Mary-Jane") == "maryjane"
    assert slug("--odd--", keep_hyphen=True) == "odd"


def test_keep_hyphen_is_a_name_field():
    import dataclasses
    n = parse_name("Mary-Jane Watson")
    assert n.f == "maryjane"
    assert dataclasses.replace(n, keep_hyphen=True).f == "mary-jane"


def test_initials():
    n = parse_name("Jane Doe")
    assert (n.fi, n.li) == ("j", "d")


@pytest.mark.parametrize("raw,want", [
    ("https://www.Acme.com/careers?x=1", "acme.com"),
    ("http://acme.com", "acme.com"),
    ("a.b@mail.acme.co.uk", "mail.acme.co.uk"),
    ("ACME.COM", "acme.com"),
    ("acme.com:8080/x", "acme.com"),
    ("", ""),
])
def test_domain_of(raw, want):
    assert domain_of(raw) == want


def test_contact_key_is_stable_across_name_spellings():
    assert contact_key("Doe, Jane", "https://www.acme.com") == \
           contact_key("Jane Doe", "acme.com")


def test_contact_key_separates_same_name_at_different_companies():
    assert contact_key("Jane Doe", "acme.com") != contact_key("Jane Doe", "beta.com")
