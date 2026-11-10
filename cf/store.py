"""SQLite persistence.

Pattern evidence is derived on read from confirmations + outcomes rather than
cached in its own table, so the learned priors can never drift out of sync with
the underlying facts.
"""
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  domain TEXT PRIMARY KEY, name TEXT, careers_url TEXT,
  notes TEXT, added_at TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL, company_domain TEXT NOT NULL,
  name TEXT, title TEXT, email TEXT, email_pattern TEXT,
  email_confidence REAL, email_verified INTEGER DEFAULT 0,
  source TEXT, source_url TEXT, found_at TEXT,
  score REAL, role_tier TEXT, seniority_tier TEXT, flags TEXT,
  status TEXT DEFAULT 'new', notes TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id INTEGER, address TEXT, kind TEXT NOT NULL,
  detail TEXT, occurred_at TEXT,
  UNIQUE(contact_id, kind, occurred_at, address)
);
CREATE TABLE IF NOT EXISTS confirmations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain TEXT NOT NULL, pattern TEXT NOT NULL, email TEXT UNIQUE,
  name TEXT, source TEXT, confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_contacts_domain ON contacts(company_domain);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS idx_outcomes_contact ON outcomes(contact_id);
"""

# Outcome kinds that prove an address is deliverable vs. wrongly formatted.
HIT_KINDS = {"human_reply", "auto_reply", "delivered"}
MISS_KINDS = {"hard_bounce"}
NEUTRAL_KINDS = {"sent", "soft_bounce", "blocked", "no_response"}


def months_between(then: str | None, today: str | None = None) -> float | None:
    """Whole-ish months between an ISO date and `today` (default: real today)."""
    if not then:
        return None
    try:
        a = datetime.fromisoformat(str(then)[:10]).date()
    except ValueError:
        return None
    b = date.fromisoformat(today[:10]) if today else date.today()
    return max(0.0, (b - a).days / 30.44)


class Store:
    def __init__(self, path: str = "data/contacts.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # ---------- companies ----------
    def upsert_company(self, domain: str, name: str = "", careers_url: str = "",
                       notes: str = "") -> str:
        self.db.execute(
            """INSERT INTO companies(domain,name,careers_url,notes,added_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(domain) DO UPDATE SET
                 name=COALESCE(NULLIF(excluded.name,''),companies.name),
                 careers_url=COALESCE(NULLIF(excluded.careers_url,''),companies.careers_url),
                 notes=COALESCE(NULLIF(excluded.notes,''),companies.notes)""",
            (domain, name, careers_url, notes, date.today().isoformat()))
        self.db.commit()
        return domain

    def companies(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM companies ORDER BY domain")]

    # ---------- contacts ----------
    def upsert_contact(self, c: dict) -> int:
        """Insert or merge a contact, keyed on person+domain. Non-empty incoming
        fields win; a verified email is never overwritten by an inferred one."""
        flags = c.get("flags")
        row = (
            c["key"], c["company_domain"], c.get("name", ""), c.get("title", ""),
            c.get("email", ""), c.get("email_pattern", ""),
            float(c.get("email_confidence") or 0.0),
            int(bool(c.get("email_verified"))),
            c.get("source", "unknown"), c.get("source_url", ""),
            c.get("found_at") or date.today().isoformat(),
            float(c.get("score") or 0.0), c.get("role_tier", ""),
            c.get("seniority_tier", ""),
            json.dumps(flags) if isinstance(flags, (list, dict)) else (flags or "[]"),
            c.get("status", "new"), c.get("notes", ""),
        )
        self.db.execute(
            """INSERT INTO contacts(key,company_domain,name,title,email,email_pattern,
                 email_confidence,email_verified,source,source_url,found_at,score,
                 role_tier,seniority_tier,flags,status,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 name=COALESCE(NULLIF(excluded.name,''),contacts.name),
                 title=COALESCE(NULLIF(excluded.title,''),contacts.title),
                 email=CASE WHEN contacts.email_verified=1 THEN contacts.email
                            ELSE COALESCE(NULLIF(excluded.email,''),contacts.email) END,
                 email_pattern=CASE WHEN contacts.email_verified=1 THEN contacts.email_pattern
                            ELSE COALESCE(NULLIF(excluded.email_pattern,''),contacts.email_pattern) END,
                 email_confidence=MAX(excluded.email_confidence,contacts.email_confidence),
                 email_verified=MAX(excluded.email_verified,contacts.email_verified),
                 source=COALESCE(NULLIF(excluded.source,'unknown'),contacts.source),
                 source_url=COALESCE(NULLIF(excluded.source_url,''),contacts.source_url),
                 score=excluded.score, role_tier=excluded.role_tier,
                 seniority_tier=excluded.seniority_tier, flags=excluded.flags""",
            row)
        self.db.commit()
        got = self.db.execute("SELECT id FROM contacts WHERE key=?", (c["key"],)).fetchone()
        return int(got["id"])

    def contacts(self, domain: str = "", status: str = "", min_score: float = 0.0,
                 limit: int = 0) -> list[dict]:
        q = "SELECT * FROM contacts WHERE score >= ?"
        args: list = [min_score]
        if domain:
            q += " AND company_domain = ?"
            args.append(domain)
        if status:
            q += " AND status = ?"
            args.append(status)
        q += " ORDER BY score DESC, name ASC"
        if limit:
            q += f" LIMIT {int(limit)}"
        out = []
        for r in self.db.execute(q, args):
            d = dict(r)
            try:
                d["flags"] = json.loads(d.get("flags") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["flags"] = []
            out.append(d)
        return out

    def contact_by_email(self, email: str) -> dict | None:
        r = self.db.execute("SELECT * FROM contacts WHERE lower(email)=?",
                            ((email or "").lower(),)).fetchone()
        return dict(r) if r else None

    def set_status(self, contact_id: int, status: str):
        self.db.execute("UPDATE contacts SET status=? WHERE id=?", (status, contact_id))
        self.db.commit()

    # ---------- outcomes & confirmations ----------
    def add_outcome(self, contact_id: int | None, kind: str, address: str = "",
                    detail: str = "", occurred_at: str = "") -> int:
        at = occurred_at or date.today().isoformat()
        cur = self.db.execute(
            """INSERT OR IGNORE INTO outcomes(contact_id,address,kind,detail,occurred_at)
               VALUES(?,?,?,?,?)""", (contact_id, address, kind, detail, at))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def outcomes(self, kind: str = "") -> list[dict]:
        if kind:
            rows = self.db.execute("SELECT * FROM outcomes WHERE kind=?", (kind,))
        else:
            rows = self.db.execute("SELECT * FROM outcomes")
        return [dict(r) for r in rows]

    def add_confirmation(self, domain: str, pattern: str, email: str, name: str = "",
                         source: str = "manual", confirmed_at: str = "") -> int:
        cur = self.db.execute(
            """INSERT INTO confirmations(domain,pattern,email,name,source,confirmed_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(email) DO UPDATE SET pattern=excluded.pattern,
                 source=excluded.source""",
            (domain, pattern, (email or "").lower(), name, source,
             confirmed_at or date.today().isoformat()))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def evidence_for(self, domain: str) -> dict:
        """Per-pattern {hits, misses} for a domain, derived from facts on hand.

        Counts *distinct proven addresses*, not outcome rows: one recruiter who
        replies three times is one piece of evidence about the format, and a
        person who appears in both confirmations and replies is counted once.
        """
        hits: dict[str, set] = {}
        misses: dict[str, set] = {}

        def bump(bucket, pattern, identity):
            if pattern and identity:
                bucket.setdefault(pattern, set()).add(identity.lower())

        for r in self.db.execute(
                "SELECT pattern, email FROM confirmations WHERE domain=?", (domain,)):
            bump(hits, r["pattern"], r["email"])

        rows = self.db.execute(
            """SELECT o.kind AS kind, c.email_pattern AS pattern,
                      COALESCE(NULLIF(o.address,''), c.email) AS address
                 FROM outcomes o JOIN contacts c ON c.id = o.contact_id
                WHERE c.company_domain = ?""", (domain,))
        for r in rows:
            if r["kind"] in HIT_KINDS:
                bump(hits, r["pattern"], r["address"])
            elif r["kind"] in MISS_KINDS:
                bump(misses, r["pattern"], r["address"])

        ev: dict[str, dict[str, int]] = {}
        for pattern in set(hits) | set(misses):
            h = hits.get(pattern, set())
            m = misses.get(pattern, set()) - h   # a proven address isn't a miss
            ev[pattern] = {"hits": len(h), "misses": len(m)}
        return ev

    # ---------- settings ----------
    def get_setting(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set_setting(self, key: str, value):
        self.db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))
        self.db.commit()

    def stats(self) -> dict:
        one = lambda q: self.db.execute(q).fetchone()[0]  # noqa: E731
        return {
            "companies": one("SELECT COUNT(*) FROM companies"),
            "contacts": one("SELECT COUNT(*) FROM contacts"),
            "with_email": one("SELECT COUNT(*) FROM contacts WHERE email != ''"),
            "verified": one("SELECT COUNT(*) FROM contacts WHERE email_verified=1"),
            "contacted": one("SELECT COUNT(*) FROM outcomes WHERE kind='sent'"),
            "human_replies": one("SELECT COUNT(*) FROM outcomes WHERE kind='human_reply'"),
            "hard_bounces": one("SELECT COUNT(*) FROM outcomes WHERE kind='hard_bounce'"),
            "locked_domains": one("SELECT COUNT(DISTINCT domain) FROM confirmations"),
        }
