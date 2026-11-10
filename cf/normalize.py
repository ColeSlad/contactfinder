"""Name, domain and title normalization. Pure functions, no I/O."""
import re
import unicodedata
from dataclasses import dataclass

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "mba", "md", "esq"}
PREFIXES = {"dr", "mr", "mrs", "ms", "miss", "mx", "prof", "professor", "sir", "rev"}
# Surname particles that belong to the last name ("van der Berg").
PARTICLES = {"van", "von", "de", "del", "della", "di", "da", "dos", "du",
             "la", "le", "bin", "ibn", "al", "st", "ter", "ten",
             "der", "den", "het", "bint", "abu"}


def strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def slug(s: str, keep_hyphen: bool = False) -> str:
    """Lowercase ASCII alphanumerics. 'van der Berg' -> 'vanderberg'.

    Some companies preserve hyphens in compound names ('mary-jane.watson'), so
    `keep_hyphen` produces that variant for reverse-mapping a known address.
    """
    allowed = r"[^a-z0-9-]" if keep_hyphen else r"[^a-z0-9]"
    out = re.sub(allowed, "", strip_accents(s).lower())
    return re.sub(r"-{2,}", "-", out).strip("-") if keep_hyphen else out


@dataclass(frozen=True)
class Name:
    first: str = ""
    middle: str = ""
    last: str = ""
    raw: str = ""
    # Some companies spell compound names 'mary-jane.watson' instead of
    # flattening them. dataclasses.replace(n, keep_hyphen=True) yields that
    # variant without a parallel set of accessors.
    keep_hyphen: bool = False

    @property
    def f(self) -> str:
        return slug(self.first, self.keep_hyphen)

    @property
    def l(self) -> str:  # noqa: E743 - mirrors email-pattern vocabulary
        return slug(self.last, self.keep_hyphen)

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
    while len(tokens) > 1 and slug(tokens[0]) in PREFIXES:
        tokens = tokens[1:]
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
