"""Classify bounce notices and replies pulled from Gmail.

The learning loop only trusts *hard* bounces as evidence that an address format
is wrong. A policy/spam block (5.7.x) or a full mailbox means the address is
fine and the delivery wasn't -- counting those as format errors would poison the
per-domain priors, so they are kept as separate kinds.
"""
import re
from dataclasses import dataclass

HARD_PHRASES = [
    "user unknown", "no such user", "no such recipient", "unknown recipient",
    "recipient address rejected", "address not found", "does not exist",
    "invalid recipient", "mailbox not found", "mailbox unavailable",
    "user not found", "no mailbox", "recipient not found",
    "account that you tried to reach does not exist",
]
SOFT_PHRASES = [
    "mailbox full", "over quota", "quota exceeded", "insufficient storage",
    "try again later", "temporarily", "temporary failure", "greylist",
    "rate limited", "too many messages", "deferred", "timed out",
]
BLOCKED_PHRASES = [
    "spam", "blocked", "blacklist", "blocklist", "policy", "reputation",
    "rejected due to", "not authorized", "dmarc", "spf", "message refused",
    "bulk mail", "content rejected",
]

AUTO_SUBJECT = re.compile(
    r"^\s*(auto(matic)?[\s_-]*(reply|response)|out of (the )?office|ooo\b|"
    r"re:\s*out of office|away from)", re.I)
AUTO_BODY = [
    "out of the office", "out of office", "currently away", "on parental leave",
    "on vacation", "limited access to email", "will be back on",
    "away from my desk", "this is an automated", "do not reply to this",
    "i am currently on leave",
]
BOUNCE_SENDERS = re.compile(
    r"(mailer-daemon|postmaster|no-?reply|donotreply|do-not-reply|"
    r"mail-?delivery|bounce)", re.I)
BOUNCE_SUBJECT = re.compile(
    r"(undelivered mail|returned to sender|delivery status notification|"
    r"mail delivery (failed|subsystem)|undeliverable|failure notice|"
    r"delivery (has )?failed)", re.I)

ENHANCED = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")
BASIC = re.compile(r"\b([245]\d{2})\b\s")
RECIPIENT = re.compile(
    r"(?:final-recipient|original-recipient|x-failed-recipients|"
    r"failed recipient)\s*:\s*(?:rfc822\s*;\s*)?<?([^\s<>,;]+@[^\s<>,;]+)>?", re.I)
ANY_EMAIL = re.compile(r"<?([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})>?", re.I)


@dataclass(frozen=True)
class BounceVerdict:
    kind: str          # "hard" | "soft" | "blocked" | "none"
    reason: str
    address: str = ""

    @property
    def is_format_evidence(self) -> bool:
        """Only a hard bounce says the address format was wrong."""
        return self.kind == "hard"


def extract_recipient(text: str) -> str:
    """Pull the failed recipient out of a bounce notice."""
    t = text or ""
    m = RECIPIENT.search(t)
    if m:
        return m.group(1).strip().lower()
    for cand in ANY_EMAIL.findall(t):
        c = cand.lower()
        if not BOUNCE_SENDERS.search(c):
            return c
    return ""


def classify_bounce(subject: str = "", body: str = "", smtp_status: str = "") -> BounceVerdict:
    """Decide whether a bounce notice proves the address is invalid."""
    blob = f"{subject or ''}\n{body or ''}\n{smtp_status or ''}"
    low = blob.lower()
    addr = extract_recipient(blob)

    if not (BOUNCE_SUBJECT.search(subject or "") or smtp_status
            or any(p in low for p in HARD_PHRASES + SOFT_PHRASES + BLOCKED_PHRASES)):
        return BounceVerdict("none", "no bounce indicators", addr)

    # Phrases are checked before status codes: a 5.7.1 spam rejection and a
    # 5.1.1 unknown user are both 5.x.x but mean opposite things for learning.
    if any(p in low for p in HARD_PHRASES):
        return BounceVerdict("hard", "recipient does not exist", addr)
    if any(p in low for p in SOFT_PHRASES):
        return BounceVerdict("soft", "temporary delivery failure", addr)
    if any(p in low for p in BLOCKED_PHRASES):
        return BounceVerdict("blocked", "delivery blocked by policy", addr)

    m = ENHANCED.search(blob)
    if m:
        cls, subj, det = m.group(1), int(m.group(2)), int(m.group(3))
        code = f"{cls}.{subj}.{det}"
        if cls == "4":
            return BounceVerdict("soft", f"transient status {code}", addr)
        if cls == "5":
            if subj == 7:
                return BounceVerdict("blocked", f"policy status {code}", addr)
            if subj == 2 and det in (2, 3):
                return BounceVerdict("soft", f"mailbox issue {code}", addr)
            if subj in (1, 2):
                return BounceVerdict("hard", f"permanent status {code}", addr)
            return BounceVerdict("blocked", f"permanent status {code}", addr)

    b = BASIC.search(blob + " ")
    if b:
        code = b.group(1)
        if code.startswith("4"):
            return BounceVerdict("soft", f"transient code {code}", addr)
        if code in ("550", "551", "553"):
            return BounceVerdict("hard", f"permanent code {code}", addr)
        if code.startswith("5"):
            return BounceVerdict("blocked", f"permanent code {code}", addr)

    return BounceVerdict("soft", "bounce of undetermined type", addr)


def classify_reply(from_addr: str = "", subject: str = "", body: str = "",
                   headers: dict | None = None) -> str:
    """Return "bounce", "auto" or "human".

    An auto-reply still proves the address is deliverable, so it counts as
    format evidence -- but it is not a human reply and must not inflate the
    reply-rate signal that tunes scoring weights.
    """
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    low_body = (body or "").lower()

    if BOUNCE_SUBJECT.search(subject or ""):
        return "bounce"
    if BOUNCE_SENDERS.search(from_addr or "") and (
            BOUNCE_SUBJECT.search(subject or "") or "delivery" in low_body):
        return "bounce"

    auto_submitted = h.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted != "no":
        return "auto"
    if h.get("x-autoreply") or h.get("x-autorespond") or h.get("x-auto-response-suppress"):
        return "auto"
    if h.get("precedence", "").lower() in ("bulk", "auto_reply", "junk", "list"):
        return "auto"
    if AUTO_SUBJECT.search(subject or ""):
        return "auto"
    if any(p in low_body for p in AUTO_BODY):
        return "auto"
    if BOUNCE_SENDERS.search(from_addr or ""):
        return "auto"
    return "human"
