"""DAGmate — Kaspa Name Service (.kas domains) lookups.

Same third-party indexer Dagger uses (api.knsdomains.org): a wallet's KNS
name is a real on-chain property of its address, we just read it off their
public API (no key/auth). Every call here is defensive — short timeout,
never raises, DB-cached (see database.kns_*) — so a KNS outage degrades to
"no name shown", never a broken screen.

Two call shapes, matching how the frontend uses them:
  - `primary_name()` (network, cache-first) — fine for one-off, low-frequency
    spots (wallet connect, profile) since FastAPI runs sync routes in a
    threadpool.
  - `cached_name()`/`cached_names_for()` (cache-only, no network) — for hot,
    frequently-polled list endpoints (challenges, matches) that must never
    turn into N outbound HTTP calls per request.
"""
from __future__ import annotations

import logging

import httpx

import config
import database as db

log = logging.getLogger("dagmate.kns")


def _fetch(address: str) -> tuple[list[str], str]:
    """(domains, primary_name) straight from the KNS API. Raises on failure."""
    with httpx.Client(timeout=config.KNS_TIMEOUT) as client:
        r = client.get(f"{config.KNS_API_URL}/assets", params={"owner": address, "pageSize": 50})
        r.raise_for_status()
        payload = (r.json() or {}).get("data") or {}
        names = []
        for a in (payload.get("assets") or []):
            if not a.get("isDomain"):
                continue
            name = str(a.get("asset") or "").strip()
            if name:
                names.append(name)
        primary = ""
        try:
            pr = client.get(f"{config.KNS_API_URL}/primary-name/{address}")
            if pr.status_code == 200:
                primary = str(((pr.json() or {}).get("data") or {}).get("asset") or "").strip()
        except Exception:
            primary = ""  # optional extra — never fail the whole lookup for it
    if primary and primary in names:
        names = [primary] + [n for n in names if n != primary]
    return names, primary


def get_names(address: str, *, force: bool = False) -> list[str]:
    """The wallet's .kas domains, cache-first. Never raises."""
    if not (config.KNS_ENABLED and address):
        return []
    cached, age = db.kns_get(address)
    if cached is not None and not force and age < config.KNS_CACHE_TTL:
        return cached
    try:
        names, primary = _fetch(address)
    except Exception as e:
        log.debug(f"KNS lookup {address[:14]}: {e}")
        return cached or []  # stale beats broken
    db.kns_put(address, names, primary)
    return names


def primary_name(address: str) -> str | None:
    names = get_names(address)
    return names[0] if names else None


def cached_names_for(addresses: list[str]) -> dict:
    """{address: [names]} from the LOCAL cache only — no network."""
    if not config.KNS_ENABLED:
        return {}
    try:
        return db.kns_get_many(addresses)
    except Exception as e:
        log.debug(f"kns cached_names_for: {e}")
        return {}


def cached_name(address: str) -> str | None:
    if not address:
        return None
    names = cached_names_for([address]).get(address)
    return names[0] if names else None
