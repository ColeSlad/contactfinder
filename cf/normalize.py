"""Name, domain and title normalization. Pure functions, no I/O."""
import re
import unicodedata
from dataclasses import dataclass

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "mba", "md", "esq"}
# Surname particles that belong to the last name ("van der Berg").
PARTICLES = {"van", "von", "de", "del", "della", "di", "da", "dos", "du",
             "la", "le", "bin", "ibn", "al", "st", "ter", "ten",
             "der", "den", "het", "bint", "abu"}


def strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def slug(s: str) -> str:
    """Lowercase ASCII alphanumerics only. 'van der Berg' -> 'vanderberg'."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(s).lower())


@dataclass(frozen=True)
class Name:
    first: str = ""
    middle: str = ""
    last: str = ""
    raw: str = ""

    @property
    def f(self) -> str:
        return slug(self.first)

    @property
    def l(self) -> str:  # noqa: E743 - mirrors email-pattern vocabulary
        return slug(self.last)

    @property
    def fi(self) -> str:
        return self.f[:1]

    @property
    def li(self) -> str:
        return self.l[:1]

    @property
    def ok(self) -> bool:
        return bool(self.f)


def parse_name(full: str) -> Name:
    """Parse a human name. Handles 'Doe, Jane', nicknames, suffixes, particles."""
    raw = (full or "").strip()
    if not raw:
        return Name()

    s = re.sub(r"\s+", " ", raw)
    s = re.sub(r"[\"“”']([^\"“”']+)[\"“”']", " ", s)   # drop "JJ" nicknames
    s = re.sub(r"\(([^)]*)\)", " ", s)                  # drop (JJ) nicknames
    s = s.split(" - ")[0]                               # 'Jane Doe - Recruiter'
    s = s.split(",")[0] if _is_suffix_tail(s) else s
    s = re.sub(r"\s+", " ", s).strip()

    if "," in s:                                        # 'Doe, Jane Q.'
        last_part, _, rest = s.partition(",")
        s = f"{rest.strip()} {last_part.strip()}".strip()

    tokens = [t for t in s.split(" ") if t]
    tokens = [t for t in tokens if slug(t) not in SUFFIXES]
    tokens = [t for t in tokens if slug(t)]
    if not tokens:
        return Name(raw=raw)
    if len(tokens) == 1:
        return Name(first=tokens[0], raw=raw)

    # Absorb trailing-surname particles: 'Jane van der Berg'.
    last_start = len(tokens) - 1
    while last_start > 1 and slug(tokens[last_start - 1]) in PARTICLES:
        last_start -= 1

    first = tokens[0]
    last = " ".join(tokens[last_start:])
    middle = " ".join(tokens[1:last_start])
    return Name(first=first, middle=middle, last=last, raw=raw)


def _is_suffix_tail(s: str) -> bool:
    """True for 'Jane Doe, PhD' (comma introduces a suffix, not Last, First)."""
    if "," not in s:
        return False
    tail = s.split(",", 1)[1].strip()
    return bool(tail) and slug(tail.split(" ")[0]) in SUFFIXES


def domain_of(value: str) -> str:
    """Extract a bare domain from a URL, email, or domain string."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0]
    v = v.split(":")[0]
    if v.startswith("www."):
        v = v[4:]
    return v.strip(".")


def normalize_title(title: str) -> str:
    t = strip_accents(title or "").lower()
    t = t.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def contact_key(name: str, domain: str) -> str:
    """Stable dedup key for a person at a company."""
    n = parse_name(name)
    return f"{n.f}|{n.l}|{domain_of(domain)}"
