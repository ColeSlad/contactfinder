# ContactFinder

Finds and ranks **early-career / university-recruiting contacts** at target
companies, for SWE internship cold outreach — then learns from what happens to
the emails you send.

Python owns everything deterministic (address inference, scoring, storage,
learning). Web research is done by Claude Code via the
[`contact-finder` skill](.claude/skills/contact-finder/SKILL.md), because the
code cannot search.

## Setup

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./cf-run init
```

## The core idea

Companies use one email address format. So **one confirmed address at a company
unlocks everyone else there.**

```
$ ./cf-run queue                       # addresses inferred from a prior
 SCORE   CONF  NAME                    EMAIL                        ROLE TIER
 0.822   0.39  Jane Doe                jane.doe@acme.com            university-recruiting
 0.800   0.39  Bob Smith               bob.smith@acme.com           early-career
 0.795   0.39  Dr. Priya Raghunathan   priya.raghunathan@acme.com   university-recruiting

$ ./cf-run confirm jdoe@acme.com --name "Jane Doe"
confirmed jdoe@acme.com as flast for acme.com
format locked: flast
rescored 6 contacts with the new evidence

$ ./cf-run queue                       # one confirmation rewrote all of them
 0.963   0.95  Jane Doe                jdoe@acme.com                university-recruiting
 0.940   0.95  Bob Smith               bsmith@acme.com              early-career
 0.935   0.95  Dr. Priya Raghunathan   praghunathan@acme.com        university-recruiting
```

## Commands

| | |
|---|---|
| `init` | create the database, load `companies.yaml` |
| `companies` | targets, locked formats, contacts on file |
| `add <file.json>` | ingest researched candidates |
| `confirm <email> --name "..."` | record a verified address; **locks the company format** |
| `queue` / `export` | the outreach list, best first |
| `mark-sent <email>...` | record that outreach went out |
| `gmail-ingest <file.json>` | fold bounces and replies into learning |
| `learn` | retune formats and scoring weights, then rescore everyone |
| `check` | unit tests **+** golden-set regression gate |
| `eval` | scorecard only |
| `stats` | reply rate, locked formats, totals |
| `enrich <domain>` | Apollo.io (optional, **paid plan**, needs `APOLLO_API_KEY`); `--check` tests access, `--dry-run` spends no credits |
| `lessons --add "..."` | record a research heuristic that worked |

## How ranking works

Five features, weighted, with role relevance and seniority on **separate axes**
— a Campus Recruiting Coordinator is a great target while a VP of Campus
Recruiting is not, and one axis cannot express that.

| feature | weight | notes |
|---|---|---|
| `title` | 0.40 | tiered: university/early-career 1.00 → technical recruiter 0.70 → sourcer 0.45 → engineer 0.10 |
| `email_confidence` | 0.25 | from the format prior; 0.95 once a company's format is locked |
| `source` | 0.15 | `careers_page` 0.90 → `job_posting` 0.80 → `social_profile` 0.55 → `inferred` 0.30 |
| `recency` | 0.12 | 12-month half-life; recruiters move constantly |
| `seniority` | 0.08 | coordinator 1.00 → director 0.60 → VP 0.35 → C-level 0.20 |

Role relevance is also a **hard gate**, not only a weighted term: without it a
software engineer with a pristine source and a fresh date clears `min_score` on
source quality and recency alone.

## How it improves itself

Three mechanisms, each **gated on sample size** so thin data cannot make it
thrash. `learn` reports what it is still waiting for.

1. **Per-company formats.** A reply (or auto-reply) proves an address is
   deliverable and confirms the format; a hard bounce counts against it. One
   confirmation locks the company.
2. **The global format prior.** Retuned from confirmations across companies,
   one vote per company so a heavily-mined employer cannot dominate. Needs 5+
   distinct companies.
3. **Scoring weights.** Coordinate ascent on reply-ranking AUC, regularized
   toward the shipped defaults. Needs 25+ contacted and 3+ replies.

Plus `LESSONS.md`, where research heuristics accumulate for future runs.

### Judgment encoded in the learning loop

These distinctions are the difference between learning and corrupting itself:

- **A `5.7.1` spam block is not a wrong address.** Only *hard* bounces
  (`5.1.1`, "user unknown") count as format evidence. A full mailbox doesn't
  either.
- **An auto-reply proves deliverability but is not a reply.** It confirms the
  format without inflating the reply rate that tunes the weights.
- **A hard bounce may mean the person left, not that the format is wrong.** So a
  company stays locked while `hits > 2 × misses`, rather than one departed
  employee vetoing a well-evidenced format.
- **One person replying three times is one piece of evidence.** Evidence counts
  distinct proven addresses, not outcome rows.
- **A hard bounce must vacate first place.** Otherwise the next address tried at
  that company repeats a format already proven to bounce.

## How it tests itself

```bash
./cf-run check     # 230 unit tests + the golden-set gate
```

`eval` scores nine sections against `eval/golden.yaml` and appends the result to
`eval/history.jsonl`. `--gate` fails on any failing case or any score below the
best ever recorded.

The golden set is deliberately **synthetic** — invented companies on the
reserved `.test` TLD. Real recruiters' inferred addresses never belong in a
fixture file in a public repo, and the machinery under test is format-level, so
synthetic fixtures exercise it just as well.

`known_limitations` in the golden set are **reported but not scored**, so the
overall number can't be inflated by listing them. If one starts passing, eval
flags it as promotable. Currently documented: truncated surnames, surname-first
name ordering, the `lastf` format, hyphen-preserving generation, and compound
given names.

The adversarial golden cases earned their place — on first run they caught four
real defects: honorifics parsed as first names (`Dr. Elena Vasquez` →
first=`Dr.`), a regex that couldn't match "recruit**er**" (so "Recruiter
(Campus)" scored 0.55 generic instead of 0.95 university), no `univ`/`uni`
abbreviation support, and hyphen-lossy reverse mapping that learned nothing
from a confirmed `mary-jane.watson@`.

## Apollo.io (optional)

`export APOLLO_API_KEY=...` enables it, or put it in `.env` (gitignored, see
`.env.example`). Everything else works without it.

> **Apollo's people endpoints require a paid plan.** On the Free plan
> `mixed_people/api_search`, `mixed_people/search`, `people/match` and
> `people/bulk_match` all return `403 API_INACCESSIBLE` — refused *even with a
> master key*. Only `organizations/enrich` is available, and it returns no
> addresses, no people and no format, so it cannot help here. Check any key
> with:
>
> ```bash
> ./cf-run enrich <domain> --check    # spends no credits
> ```
>
> Until the plan is upgraded, web research via the `contact-finder` skill is
> the working path — and it is the primary path by design.

Apollo's shape drives how it's used:

- **People Search is free but masks last names** (`Do***e`) and returns no
  addresses — it can only discover who exists.
- **People Enrichment returns the real name and address but costs credits.**

So search runs first, the existing role scorer gates which people are worth
paying for, and only those get enriched — batched 10 at a time through
`bulk_match`. Look before you pay:

```bash
./cf-run enrich stripe.com --dry-run     # masked names, no credits spent
./cf-run enrich stripe.com               # enriches only on-target roles
```

```
apollo: 24 found, 6 enriched, 6 credits used
  skipped 15 off-target role(s) before spending credits
  skipped 3 with no address on file
```

Two safeguards worth knowing about:

- **A masked name is refused, never cleaned.** `slug("Do***e")` is `"doe"` — the
  asterisks vanish and it becomes a confident-looking lie that could be Doyle,
  Dodge or Doke. Both the adapter and the pipeline reject names containing `*`.
- **`email_status: invalid` is discarded**, not ingested with low confidence.

Apollo doesn't report a company's address format, but we get it anyway: a
verified address plus a real name reverse-maps through `pattern_of()`, which
locks the company.

Note: People Search needs a **master API key**; a 403 is reported with that
hint rather than a bare status code.

## Privacy

The GitHub remote is public, so `data/`, `exports/`, and any contact dumps are
gitignored. **Never commit a real contact list.** Only code, config schema, and
synthetic fixtures are tracked.

These are real people. Keep volume sane, keep to work addresses, and keep to
people whose job is to hear from candidates.

## Layout

```
cf/normalize.py   names, domains, titles          cf/pipeline.py   research -> scored contacts
cf/patterns.py    address formats + priors        cf/export.py     CSV / table output
cf/score.py       ranking                         cf/apollo.py     Apollo.io (optional)
cf/mail.py        bounce + reply classification   cf/evaluate.py   self-evaluation
cf/store.py       sqlite                          cf/cli.py        commands
cf/learn.py       self-improvement                cf/config.py     config precedence
```
