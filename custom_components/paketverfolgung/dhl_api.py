"""Minimal async client for DHL's public shipment-tracking search.

Looking up a shipment by its tracking number (piececode) works fully
anonymously - no DHL account/login needed. Verified against the real
endpoint. The account-linked "auto-detect my shipments" endpoint that
ioBroker.parcel uses returns empty results (as of 2026-08), which is why
this integration tracks explicitly-added tracking numbers instead.
"""
from __future__ import annotations

import logging

from aiohttp import ClientError, ClientSession

from .const import SEARCH_URL, USER_AGENT

_LOGGER = logging.getLogger(__name__)


class DhlApiError(Exception):
    """Error talking to the DHL tracking search endpoint."""


class DhlApiClient:
    """Talks to DHL's public shipment-tracking search endpoint."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def fetch_shipments(self, tracking_numbers: list[str]) -> list[dict]:
        """Fetch current details for the given tracking numbers."""
        if not tracking_numbers:
            return []

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": USER_AGENT,
            "accept-language": "de-de",
        }
        params = {
            "piececode": ",".join(tracking_numbers),
            "noRedirect": "true",
            "language": "de",
            "cid": "app",
        }
        try:
            async with self._session.get(
                SEARCH_URL, headers=headers, params=params, timeout=15
            ) as resp:
                _LOGGER.debug("DHL search request -> status %s", resp.status)
                if resp.status != 200:
                    raise DhlApiError(
                        f"DHL shipment search failed with status {resp.status}"
                    )
                payload = await resp.json(content_type=None)
        except ClientError as err:
            raise DhlApiError(f"Network error fetching DHL shipments: {err}") from err

        _LOGGER.debug("DHL search response: %s", payload)
        return (payload or {}).get("sendungen", []) or []
