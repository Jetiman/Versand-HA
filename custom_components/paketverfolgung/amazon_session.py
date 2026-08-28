"""Helpers for persisting Amazon session cookies safely in Home Assistant."""
from __future__ import annotations

from typing import Any

from yarl import URL

from .amazon_api import AmazonApiClient

_COOKIE_DOMAINS = ("amazon.de", "www.amazon.de")
_COOKIE_FORMAT = "domain_v1"


def export_cookie_store(client: AmazonApiClient) -> dict[str, Any]:
    """Export the cookie sets used for the relevant Amazon hosts."""
    domains: dict[str, dict[str, str]] = {}
    for domain in _COOKIE_DOMAINS:
        domains[domain] = {
            name: morsel.value
            for name, morsel in client._jar.filter_cookies(URL(f"https://{domain}/")).items()
        }
    return {"_format": _COOKIE_FORMAT, "domains": domains}


def load_cookie_store(client: AmazonApiClient, store: Any) -> bool:
    """Restore a persisted Amazon cookie store into a client."""
    if not isinstance(store, dict) or store.get("_format") != _COOKIE_FORMAT:
        return False
    domains = store.get("domains") or {}
    if not isinstance(domains, dict):
        return False
    loaded = False
    for domain, cookies in domains.items():
        if domain not in _COOKIE_DOMAINS or not isinstance(cookies, dict) or not cookies:
            continue
        client._jar.update_cookies(cookies, response_url=URL(f"https://{domain}/"))
        loaded = True
    return loaded
