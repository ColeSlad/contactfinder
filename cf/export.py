"""Export the outreach queue."""
import csv
from pathlib import Path

COLUMNS = ["name", "title", "company_domain", "email", "email_confidence",
           "email_verified", "score", "role_tier", "seniority_tier",
           "source", "source_url", "found_at", "flags"]


def to_csv(rows: list[dict], path: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row = dict(r)
            row["flags"] = ",".join(row.get("flags") or [])
            row["email_confidence"] = f"{float(row.get('email_confidence') or 0):.3f}"
            w.writerow(row)
    return str(p)


def to_table(rows: list[dict], limit: int = 25) -> str:
    if not rows:
        return "(queue is empty)"
    out = [f"{'SCORE':>6}  {'CONF':>5}  {'NAME':22}  {'EMAIL':34}  {'ROLE TIER':22} TITLE"]
    out.append("-" * 130)
    for r in rows[:limit]:
        lock = "*" if r.get("email_verified") else " "
        out.append(
            f"{r.get('score', 0):>6.3f}  {float(r.get('email_confidence') or 0):>5.2f}{lock} "
            f"{(r.get('name') or '(role inbox)')[:22]:22}  {(r.get('email') or '')[:34]:34}  "
            f"{(r.get('role_tier') or '')[:22]:22} {(r.get('title') or '')[:36]}")
    if len(rows) > limit:
        out.append(f"... and {len(rows) - limit} more")
    out.append("\n* = verified address")
    return "\n".join(out)
