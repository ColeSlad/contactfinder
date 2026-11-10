"""Command line interface. Run via ./cf <command>."""
import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from . import apollo, evaluate as ev
from .config import effective_weights, load_companies, load_config
from .export import to_csv, to_table
from .learn import learn as run_learn
from .patterns import locked_pattern, pattern_of
from .pipeline import ingest_candidates, queue, rescore_all
from .store import Store

LESSONS_PATH = "LESSONS.md"


def _load_json(path: str):
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        for key in ("candidates", "contacts", "events", "items"):
            if key in data:
                return data[key]
        return [data]
    return data


def cmd_init(a, store, cfg):
    comps = load_companies(a.companies)
    for c in comps:
        store.upsert_company(c["domain"], c["name"], c["careers_url"], c["notes"])
    print(f"database ready at {store.path}")
    print(f"loaded {len(comps)} companies from {a.companies}")
    return 0


def cmd_companies(a, store, cfg):
    comps = load_companies(a.companies)
    by_domain = {c["domain"]: c for c in comps}
    print(f"{'DOMAIN':26} {'PRIORITY':9} {'FORMAT':12} {'CONTACTS':>8}  CAREERS URL")
    print("-" * 110)
    for row in store.companies():
        dom = row["domain"]
        meta = by_domain.get(dom, {})
        n = len(store.contacts(domain=dom))
        fmt = locked_pattern(store.evidence_for(dom)) or "-"
        print(f"{dom:26} {meta.get('priority', '-'):9} {fmt:12} {n:>8}  "
              f"{meta.get('careers_url', '') or row['careers_url'] or ''}")
    return 0


def cmd_add(a, store, cfg):
    cands = _load_json(a.file)
    rep = ingest_candidates(store, cands, cfg, today=a.today)
    print(rep.summary())
    return 0


def cmd_confirm(a, store, cfg):
    """Record a verified address. This locks the company's format."""
    from .normalize import domain_of
    email = a.email.strip().lower()
    domain = domain_of(email)
    pattern = a.format or pattern_of(email, a.name or "")
    if not pattern:
        print(f"could not work out the format of {email} from the name "
              f"{a.name!r}.\nPass --format explicitly (e.g. --format first.last).")
        return 1
    store.add_confirmation(domain, pattern, email, a.name or "", "manual")
    # The person is usually on file under a *guessed* address, so look them up
    # by name as well -- otherwise confirming the real address never marks them
    # verified.
    existing = store.contact_by_email(email)
    if not existing and a.name:
        from .normalize import contact_key
        row = store.db.execute("SELECT * FROM contacts WHERE key=?",
                               (contact_key(a.name, domain),)).fetchone()
        existing = dict(row) if row else None
    if existing:
        store.db.execute(
            """UPDATE contacts SET email=?, email_pattern=?, email_verified=1,
                 email_confidence=0.99 WHERE id=?""",
            (email, pattern, existing["id"]))
        store.db.commit()
        print(f"marked {existing['name']} as verified at {email}")
    else:
        print(f"no contact on file for {email}; recorded the format only")
    n = rescore_all(store, cfg, today=a.today)
    print(f"confirmed {email} as {pattern} for {domain}")
    print(f"format locked: {locked_pattern(store.evidence_for(domain))}")
    print(f"rescored {n} contacts with the new evidence")
    return 0


def cmd_queue(a, store, cfg):
    rows = queue(store, cfg, limit=a.limit)
    print(to_table(rows, limit=a.limit or 25))
    return 0


def cmd_export(a, store, cfg):
    rows = queue(store, cfg, limit=a.limit)
    path = to_csv(rows, a.out)
    print(f"wrote {len(rows)} contacts to {path}")
    print("(exports/ is gitignored -- contact data stays off the public repo)")
    return 0


def cmd_mark_sent(a, store, cfg):
    sent = 0
    for email in a.emails:
        c = store.contact_by_email(email.strip().lower())
        if not c:
            print(f"  unknown address: {email}")
            continue
        store.add_outcome(c["id"], "sent", c["email"], "marked via cli", a.today)
        store.set_status(c["id"], "contacted")
        sent += 1
    print(f"marked {sent} contact(s) as contacted")
    return 0


def cmd_gmail_ingest(a, store, cfg):
    events = _load_json(a.file)
    rep = run_learn(store, events, today=a.today)
    print(rep.summary())
    n = rescore_all(store, cfg, today=a.today)
    print(f"rescored {n} contacts")
    return 0


def cmd_learn(a, store, cfg):
    rep = run_learn(store, [], today=a.today)
    print(rep.summary())
    n = rescore_all(store, cfg, today=a.today)
    print(f"rescored {n} contacts")
    print("\ncurrent weights:")
    for k, v in effective_weights(store, cfg).items():
        print(f"  {k:18} {v:.4f}")
    return 0


def cmd_eval(a, store, cfg):
    card = ev.evaluate(a.golden)
    print(card.render())
    best = ev.best_overall(a.history)
    stamp = a.today or date.today().isoformat()
    if not a.no_record:
        ev.record(card, stamp, a.history, note=a.note or "")
    if best is not None:
        delta = card.overall - best
        print(f"\nbest previous: {best:.1%}  (this run: {delta:+.1%})")
    if a.gate and best is not None and card.overall < best - 1e-9:
        print(f"\nGATE FAILED: {card.overall:.1%} is below the best recorded "
              f"{best:.1%}")
        return 1
    if a.gate and card.failures:
        print(f"\nGATE FAILED: {len(card.failures)} failing case(s)")
        return 1
    return 0


def cmd_test(a, store, cfg):
    """Run the unit test suite."""
    cmd = [sys.executable, "-m", "pytest", "-q", "tests"]
    if a.verbose:
        cmd[-2:] = ["-v", "tests"]
    return subprocess.call(cmd)


def cmd_check(a, store, cfg):
    """Unit tests plus the golden-set gate: the full self-test."""
    print("=== unit tests ===")
    rc = subprocess.call([sys.executable, "-m", "pytest", "-q", "tests"])
    print("\n=== golden-set evaluation ===")
    card = ev.evaluate(a.golden)
    print(card.render())
    best = ev.best_overall(a.history)
    if not a.no_record:
        ev.record(card, a.today or date.today().isoformat(), a.history,
                  note=a.note or "cf check")
    gate_failed = bool(card.failures) or (
        best is not None and card.overall < best - 1e-9)
    print(f"\nunit tests: {'PASS' if rc == 0 else 'FAIL'}   "
          f"golden set: {'PASS' if not gate_failed else 'FAIL'}")
    return 0 if rc == 0 and not gate_failed else 1


def cmd_stats(a, store, cfg):
    st = store.stats()
    width = max(len(k) for k in st)
    for k, v in st.items():
        print(f"  {k:{width}} {v}")
    locked = []
    for row in store.db.execute("SELECT DISTINCT domain FROM confirmations"):
        pat = locked_pattern(store.evidence_for(row["domain"]))
        if pat:
            locked.append(f"{row['domain']}={pat}")
    print(f"\n  locked formats: {', '.join(locked) if locked else '(none yet)'}")
    if st["contacted"]:
        rate = st["human_replies"] / st["contacted"]
        print(f"  reply rate: {rate:.1%} ({st['human_replies']}/{st['contacted']})")
    return 0


def cmd_enrich(a, store, cfg):
    """Find contacts at a domain via Apollo. Search is free; enrichment costs
    credits, so role relevance gates spending."""
    if not apollo.enabled():
        print("APOLLO_API_KEY is not set, so enrichment is disabled.")
        print("Everything else works without it; export APOLLO_API_KEY to enable")
        print("or put it in a .env file (gitignored).")
        return 1
    if a.check:
        print("Apollo endpoint access for this key (no credits spent):\n")
        rows = apollo.check_access()
        for label, ok, detail in rows:
            print(f"  [{'OK ' if ok else 'NO '}] {label}")
            if not ok:
                print(f"         {detail}")
        people_ok = any(ok for label, ok, _ in rows if "people" in label)
        if not people_ok:
            print("\nNo people endpoints are available, so `enrich` cannot find "
                  "contacts.\nWeb research via the contact-finder skill is the "
                  "working path on this plan.")
            return 1
        return 0
    ap = cfg.get("apollo", {})
    titles = a.titles.split(",") if a.titles else ap.get("titles")
    cands, rep = apollo.find_contacts(
        a.domain, titles=titles, seniorities=ap.get("seniorities"),
        per_page=a.per_page or ap.get("per_page", 25),
        min_title_score=ap.get("min_title_score", 0.45),
        dry_run=a.dry_run)
    print(rep.summary())
    if rep.errors and not cands:
        if any("paid Apollo plan" in e for e in rep.errors):
            print("\nRun `enrich --check` to see which endpoints this key allows.")
        return 1
    if a.dry_run:
        print("\n-- dry run, no credits spent, last names still masked --")
        for c in cands:
            flag = " (masked)" if c.get("masked") else ""
            print(f"  {c['name']}{flag:9}  {c['title']}")
        print(f"\n{len(cands)} would be enriched. Re-run without --dry-run to "
              f"spend credits.")
        return 0
    if not cands:
        print("no contacts to ingest")
        return 0
    print()
    print(ingest_candidates(store, cands, cfg, today=a.today).summary())
    rescore_all(store, cfg, today=a.today)
    return 0


def cmd_lessons(a, store, cfg):
    p = Path(LESSONS_PATH)
    if a.add:
        stamp = a.today or date.today().isoformat()
        if not p.exists():
            p.write_text("# Research lessons\n\nWhat has actually worked when "
                         "hunting for early-career recruiting contacts.\n")
        with p.open("a") as fh:
            fh.write(f"\n- {stamp}: {a.add}\n")
        print(f"recorded to {LESSONS_PATH}")
        return 0
    print(p.read_text() if p.exists() else f"{LESSONS_PATH} is empty")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cf", description="Find and rank early-career recruiting contacts.")
    p.add_argument("--db", default="data/contacts.db")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--companies", default="companies.yaml")
    p.add_argument("--today", default="", help="override today's date (ISO) for reproducible runs")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        return s

    add("init", cmd_init, "create the database and load companies.yaml")
    add("companies", cmd_companies, "list target companies and what we know")

    s = add("add", cmd_add, "ingest researched candidates from a JSON file")
    s.add_argument("file")

    s = add("confirm", cmd_confirm, "record a verified address; locks the company format")
    s.add_argument("email")
    s.add_argument("--name", default="", help="the person's full name")
    s.add_argument("--format", default="", help="override the inferred format")

    s = add("queue", cmd_queue, "show the outreach queue")
    s.add_argument("--limit", type=int, default=0)

    s = add("export", cmd_export, "write the outreach queue to CSV")
    s.add_argument("--out", default="exports/outreach.csv")
    s.add_argument("--limit", type=int, default=0)

    s = add("mark-sent", cmd_mark_sent, "record that outreach was sent")
    s.add_argument("emails", nargs="+")

    s = add("gmail-ingest", cmd_gmail_ingest, "fold Gmail bounce/reply events into learning")
    s.add_argument("file")

    add("learn", cmd_learn, "retune formats and scoring weights from outcomes")

    for name, fn, help_ in (("eval", cmd_eval, "score the framework against the golden set"),
                            ("check", cmd_check, "unit tests plus the golden-set gate")):
        s = add(name, fn, help_)
        s.add_argument("--golden", default="eval/golden.yaml")
        s.add_argument("--history", default=ev.HISTORY_PATH)
        s.add_argument("--gate", action="store_true",
                       help="fail if the score regressed or any case fails")
        s.add_argument("--no-record", action="store_true")
        s.add_argument("--note", default="")

    s = add("test", cmd_test, "run the unit test suite")
    s.add_argument("-v", "--verbose", action="store_true")

    add("stats", cmd_stats, "summarise what the database holds")

    s = add("enrich", cmd_enrich, "find contacts via Apollo (needs APOLLO_API_KEY)")
    s.add_argument("domain")
    s.add_argument("--dry-run", action="store_true",
                   help="search only; spend no credits")
    s.add_argument("--check", action="store_true",
                   help="report which Apollo endpoints this key's plan allows")
    s.add_argument("--titles", default="",
                   help="comma-separated job titles to search (overrides config)")
    s.add_argument("--per-page", type=int, default=0)

    s = add("lessons", cmd_lessons, "read or append research lessons")
    s.add_argument("--add", default="", help="append a lesson")
    return p


# Commands that never touch the database.
NO_DB = {"eval", "test", "lessons", "check"}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    args.today = args.today or None
    if args.cmd in NO_DB:
        return args.fn(args, None, cfg)
    store = Store(args.db)
    try:
        return args.fn(args, store, cfg)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
