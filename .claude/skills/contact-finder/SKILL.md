---
name: contact-finder
description: Find and rank early-career / university recruiting contacts at target companies for SWE internship cold outreach, then learn from the results. Use when asked to find recruiters, build an outreach list, add companies to the pipeline, process bounces or replies, or improve the contact finder.
---

# Contact finder

A framework for finding early-career and university recruiting contacts for SWE
internship cold email. Python handles everything deterministic (address
inference, scoring, storage, learning); **you** do the web research, because the
code cannot search.

Run everything through `./cf-run` from the project root (it uses the venv).

## Before anything else

```bash
./cf-run lessons          # research heuristics that have actually worked
./cf-run companies        # targets, known formats, contacts on file
./cf-run stats            # reply rate, locked formats, totals
```

`lessons` first. It is where past runs recorded which searches paid off.

## The one thing that matters most

Companies use a single email format. **One confirmed address at a company
unlocks everyone else there.** So when researching a company, finding *any* one
real published address is worth more than finding five more names.

```bash
./cf-run confirm jdoe@acme.com --name "Jane Doe"
```

That locks `acme.com` to `flast` at 0.95 confidence and immediately rewrites
every other inferred address at that company. Always do this the moment you see
a genuine address.

## Research protocol, per company

Work high-priority companies first. For each one:

1. **Check what is already known** — `./cf-run companies` shows a locked format
   if we have one. If a format is locked, you only need *names and titles*.
2. **Search, roughly in order of yield:**
   - University career-fair and campus-event pages listing company reps. Often
     publish a named recruiter *with an address* — the highest-value source.
   - `site:<domain> ("university recruit" OR "campus recruit" OR "early career")`
   - The careers page's students/university section.
   - The internship job posting itself — sometimes names the recruiter.
   - Diversity-conference partner pages (NSBE, SHPE, Grace Hopper, ColorStack).
   - `site:linkedin.com/in "<Company>" ("university recruiter" OR "campus recruiting")`
   - Company engineering blog or press mentioning the recruiting team.
3. **Record the exact title verbatim.** Scoring tiers on it, so "Campus
   Recruiting Coordinator" and "Technical Recruiter" are scored very
   differently. Do not paraphrase or tidy titles.
4. **Write a JSON file and ingest it.**

```json
[
  {
    "name": "Jane Doe",
    "title": "University Recruiter",
    "domain": "acme.com",
    "company": "Acme",
    "source": "careers_page",
    "source_url": "https://acme.com/careers/university",
    "found_at": "2026-11-10",
    "email": "jdoe@acme.com",
    "email_verified": true,
    "notes": "listed on the 2027 internship page"
  }
]
```

```bash
./cf-run add candidates.json
./cf-run queue
```

`name` and `domain` are the only required fields. Omit `email` and it is
inferred.

### `source` must be one of these exact values

`company_directory`, `enrichment_verified`, `careers_page`, `job_posting`,
`university_career_fair`, `conference_or_event`, `enrichment_unverified`,
`press_or_blog`, `social_profile`, `inferred`

They carry different weight, so pick honestly. Guessing a source upward
corrupts the weight tuning later.

### Set `email_verified: true` only for an address published on a page you read

An address you *inferred* is not verified. Marking a guess as verified writes a
false format confirmation and poisons every future prediction for that company.
When unsure, leave it false.

## Apollo (optional, costs credits)

Only if `APOLLO_API_KEY` is set **and the account is on a paid plan**. Apollo's
people endpoints return `403 API_INACCESSIBLE` on the Free plan — refused even
with a master key — so check before relying on it:

```bash
./cf-run enrich stripe.com --check     # spends no credits
```

If that reports no people endpoints, do not keep trying Apollo. Web research is
the working path, and it is the primary path anyway.

When it *is* available: People Search is free but masks last names and returns
no addresses; enrichment returns both but costs credits. The command handles
that split — search first, the role scorer gates spending, then enrich only the
people worth emailing.

```bash
./cf-run enrich stripe.com --dry-run   # see who is there, spend nothing
./cf-run enrich stripe.com             # enrich on-target roles and ingest
./cf-run enrich stripe.com --titles "university recruiter,campus recruiting"
```

Always `--dry-run` first on a company you have not tried. If the roles coming
back look wrong, fix `--titles` before paying for anything.

Apollo complements web research rather than replacing it: it is strong on names
and titles at well-known companies, and weaker on small or very recent
employers where a careers page or career-fair listing still wins. A 403 means
the key lacks master-key permissions.

## Processing outcomes (the feedback loop)

This is what makes the framework improve. After outreach, read the mailbox via
the Gmail tools and turn it into events.

Search for:
- bounces — `from:mailer-daemon OR subject:(undeliverable OR "delivery status")`
- replies from the domains you contacted

Write one event per message:

```json
[
  {"kind": "sent",   "address": "jdoe@acme.com", "date": "2026-11-11"},
  {"kind": "reply",  "address": "jdoe@acme.com", "from": "Jane Doe <jdoe@acme.com>",
   "subject": "Re: SWE internship", "body": "Send me your resume.", "date": "2026-11-12"},
  {"kind": "bounce", "address": "carla@beta.com",
   "subject": "Undelivered Mail Returned to Sender",
   "body": "550 5.1.1 user unknown", "date": "2026-11-12"}
]
```

```bash
./cf-run gmail-ingest events.json
```

Include the raw `subject` and `body` — classification depends on them.
Specifically, a `5.7.1` spam rejection must **not** be treated as a bad address,
and the classifier only gets that right if it can see the text.

Mark sends directly if easier: `./cf-run mark-sent a@x.com b@y.com`

## Self-improvement

```bash
./cf-run learn     # retune formats + scoring weights from outcomes, then rescore
./cf-run check     # unit tests + golden-set regression gate
./cf-run eval      # scorecard only
```

Run `check` after **any** change to the Python. It gates on both the unit tests
and the golden-set score, and it refuses a score below the best ever recorded.

`learn` is gated on purpose: weights need 25+ contacted and 3+ replies, and the
global prior needs confirmations from 5+ distinct companies. It will tell you
what it is still waiting for instead of thrashing on thin data.

### When you improve the framework itself

1. Add a *failing* case to `eval/golden.yaml` first, or a unit test in `tests/`.
2. Fix the code.
3. `./cf-run check` — the new case passes and nothing regressed.
4. Commit.

If you hit something that genuinely cannot be fixed now, add it to
`known_limitations` in `eval/golden.yaml` with a `probe`. It is reported but not
scored, and eval tells you if it ever starts passing.

### Record what worked

```bash
./cf-run lessons --add "Career-fair PDFs on .edu sites list recruiter emails directly; higher yield than LinkedIn"
```

Do this whenever a search approach clearly works or clearly wastes time. It is
the part of the loop that improves *your* research, not just the scoring.

## Judgment to apply

- **These are real people.** Keep volume sane, keep it to work addresses, and
  keep it to people whose job is to hear from candidates. Someone who has
  hard-bounced or asked to be left alone is done — the status field records that.
- **Never invent an address and present it as real.** Inferred addresses carry a
  confidence for exactly this reason; pass them on with it.
- **Contact data stays out of git.** `data/` and `exports/` are gitignored and
  the remote is public. Never commit a real contact list, and never put real
  addresses in `eval/golden.yaml` — it uses synthetic `.test` fixtures.
- A VP or Chief People Officer will not answer an internship cold email. The
  scorer flags them; trust it and go for coordinators and university recruiters.
