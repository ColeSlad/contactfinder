# Research lessons

What has actually worked when hunting for early-career recruiting contacts.
Append with `./cf-run lessons --add "..."`.

Seeded from the framework's own design; everything below should be replaced by
real observations as runs accumulate.

- Finding one *real published* address at a company beats finding five more
  names: it locks the company's format and rewrites every other guess there.
- Record titles verbatim. Scoring tiers on the exact wording, and "Campus
  Recruiting Coordinator" scores far above "Technical Recruiter".
- A `5.7.1` rejection is a spam filter, not a wrong address. Keep the raw bounce
  body so the classifier can tell them apart.

- 2026-11-10: Companies publish their real email domain in anti-phishing notices on contact/careers pages ('HRT emails will always come from @hudson-trading.com'). This is the most reliable way to catch website-domain != email-domain, which silently breaks every inferred address. Check the contact page before inferring anything.

- 2026-11-10: Published role inboxes (campusrecruiting@, careers@) turned up at 3 of the first 3 companies researched, and at Jump Trading it was the only contact found. Always check the contact/careers page for one before hunting for named people.

- 2026-11-10: Searching '<Company> campus recruiting university recruiter LinkedIn' reliably surfaces named recruiters with titles in the result snippets. Jane Street yielded 6 named people this way.
