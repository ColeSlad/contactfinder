import pytest

from cf.mail import classify_bounce, classify_reply, extract_recipient


@pytest.mark.parametrize("subject,body,kind", [
    ("Undelivered Mail Returned to Sender", "550 5.1.1 user unknown", "hard"),
    ("Mail delivery failed", "550 5.1.10 recipient not found", "hard"),
    ("Undeliverable", "The email account that you tried to reach does not exist", "hard"),
    ("Delivery Status Notification (Failure)", "550 5.7.1 rejected due to spam content", "blocked"),
    ("Undeliverable", "452 4.2.2 mailbox full", "soft"),
    ("Delivery incomplete", "421 4.7.0 try again later", "soft"),
    ("Delivery delayed", "4.4.7 message timed out", "soft"),
    ("Re: SWE internship", "Happy to chat next week.", "none"),
    ("", "", "none"),
])
def test_classify_bounce(subject, body, kind):
    assert classify_bounce(subject, body).kind == kind


def test_only_hard_bounces_are_format_evidence():
    # Treating a spam block as a wrong address would poison the priors.
    hard = classify_bounce("Undelivered Mail", "550 5.1.1 user unknown")
    blocked = classify_bounce("Undeliverable", "550 5.7.1 blocked by policy")
    soft = classify_bounce("Undeliverable", "452 4.2.2 mailbox full")
    assert hard.is_format_evidence
    assert not blocked.is_format_evidence
    assert not soft.is_format_evidence


def test_phrases_beat_status_codes():
    # 5.7.1 and 5.1.1 are both 5.x.x but mean opposite things.
    assert classify_bounce("Undeliverable", "5.1.1 no such user").kind == "hard"
    assert classify_bounce("Undeliverable", "5.7.1 spam").kind == "blocked"


@pytest.mark.parametrize("text,want", [
    ("Final-Recipient: rfc822; jane.doe@acme.com", "jane.doe@acme.com"),
    ("X-Failed-Recipients: bob@acme.com", "bob@acme.com"),
    ("Original-Recipient: rfc822;<a.b@acme.com>", "a.b@acme.com"),
    ("could not deliver to <x@acme.com>", "x@acme.com"),
    ("no address at all", ""),
])
def test_extract_recipient(text, want):
    assert extract_recipient(text) == want


def test_extract_recipient_skips_the_daemon():
    text = "From: MAILER-DAEMON@acme.com\nFinal-Recipient: rfc822; jane@acme.com"
    assert extract_recipient(text) == "jane@acme.com"


@pytest.mark.parametrize("sender,subject,body,headers,want", [
    ("jane@x.test", "Re: internship", "Send me your resume.", None, "human"),
    ("jane@x.test", "Automatic reply: Out of office", "Back Dec 1.", None, "auto"),
    ("jane@x.test", "Re: hi", "I am currently out of the office.", None, "auto"),
    ("jane@x.test", "Re: hi", "ok", {"Auto-Submitted": "auto-replied"}, "auto"),
    ("jane@x.test", "Re: hi", "ok", {"Precedence": "bulk"}, "auto"),
    ("jane@x.test", "Re: hi", "ok", {"Auto-Submitted": "no"}, "human"),
    ("noreply@x.test", "Your application", "Thanks for applying.", None, "auto"),
    ("MAILER-DAEMON@x.test", "Undelivered Mail Returned to Sender", "550", None, "bounce"),
])
def test_classify_reply(sender, subject, body, headers, want):
    assert classify_reply(sender, subject, body, headers) == want


def test_a_human_reply_quoting_an_out_of_office_is_a_known_tradeoff():
    # Documented behaviour: body phrases win, so a human quoting an OOO reads
    # as auto. Deliverability is unaffected either way; only reply-rate is.
    assert classify_reply("jane@x.test", "Re: hi",
                          "> I am out of the office\nBack now, send your resume") == "auto"
