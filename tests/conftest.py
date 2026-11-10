import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from cf.normalize import contact_key  # noqa: E402
from cf.store import Store  # noqa: E402

TODAY = "2026-11-10"


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def add_contact(store):
    def _add(name, domain, email="", pattern="", title="University Recruiter",
             source="careers_page", found_at="2026-10-01", verified=False):
        store.upsert_company(domain)
        return store.upsert_contact({
            "key": contact_key(name, domain), "company_domain": domain,
            "name": name, "title": title, "email": email, "email_pattern": pattern,
            "email_confidence": 0.4, "email_verified": verified,
            "source": source, "found_at": found_at})
    return _add
