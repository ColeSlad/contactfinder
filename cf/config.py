"""Config loading and precedence.

Effective values are: learned (in the DB) > config.yaml > shipped defaults.
Learned values win because they came from real outcomes; config.yaml is where
you pin something you know for a fact.
"""
from pathlib import Path

import yaml

from .normalize import domain_of
from .patterns import GLOBAL_PRIOR
from .score import DEFAULT_WEIGHTS

DEFAULT_CONFIG = {
    "weights": {},
    "global_prior": {},
    "known_formats": {},
    "run": {"min_score": 0.45, "max_contacts_per_company": 5,
            # Hard exclusions. Role relevance is a gate, not a preference: a
            # software engineer with a fresh, well-sourced profile can other-
            # wise clear min_score on source and recency alone.
            "exclude_flags": ["off_target_role", "no_email"]},
}


def load_yaml(path: str | Path, default=None):
    p = Path(path)
    if not p.exists():
        return default if default is not None else {}
    with p.open() as fh:
        return yaml.safe_load(fh) or (default if default is not None else {})


def load_config(path: str | Path = "config.yaml") -> dict:
    raw = load_yaml(path, {})
    cfg = {**DEFAULT_CONFIG, **raw}
    cfg["run"] = {**DEFAULT_CONFIG["run"], **(raw.get("run") or {})}
    cfg["known_formats"] = {domain_of(k): v for k, v in (raw.get("known_formats") or {}).items()}
    return cfg


def load_companies(path: str | Path = "companies.yaml") -> list[dict]:
    """Read the target company list. `domain` is required; it anchors everything."""
    raw = load_yaml(path, {})
    entries = raw.get("companies", raw if isinstance(raw, list) else [])
    out = []
    for e in entries or []:
        if isinstance(e, str):
            e = {"domain": e}
        domain = domain_of(e.get("domain") or e.get("website") or "")
        if not domain:
            continue
        out.append({
            "domain": domain,
            "name": e.get("name") or domain.split(".")[0].title(),
            "careers_url": e.get("careers_url", ""),
            "priority": (e.get("priority") or "normal").lower(),
            "notes": e.get("notes", ""),
        })
    return out


def effective_weights(store=None, cfg: dict | None = None) -> dict:
    w = dict(DEFAULT_WEIGHTS)
    w.update((cfg or {}).get("weights") or {})
    if store is not None:
        w.update(store.get_setting("weights") or {})
    return w


def effective_prior(store=None, cfg: dict | None = None) -> dict:
    p = dict(GLOBAL_PRIOR)
    p.update((cfg or {}).get("global_prior") or {})
    if store is not None:
        p.update(store.get_setting("global_prior") or {})
    return p


def evidence_with_overrides(store, domain: str, cfg: dict | None = None) -> dict:
    """Per-domain evidence, with a config-pinned known format treated as a hit."""
    ev = dict(store.evidence_for(domain)) if store else {}
    pinned = (cfg or {}).get("known_formats", {}).get(domain)
    if pinned:
        cur = dict(ev.get(pinned, {"hits": 0, "misses": 0}))
        cur["hits"] = max(1, cur.get("hits", 0))
        ev[pinned] = cur
    return ev
