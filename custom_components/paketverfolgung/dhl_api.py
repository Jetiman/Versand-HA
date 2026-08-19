"""Minimal async client for the unofficial DHL app API.

Reverse-engineered from the open-source ioBroker.parcel adapter
(https://github.com/TA2k/ioBroker.parcel). Uses the same PKCE login flow
as the official DHL Paket app so that all shipments visible in the app
show up automatically, without entering tracking numbers manually.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientError, ClientSession

from .const import (
    ARCHIVED_STATUS,
    BASIC_AUTH_HEADER,
    CODE_VERIFIER,
    REDIRECT_URI,
    SEARCH_URL,
    TOKEN_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class DhlApiError(Exception):
    """Generic error talking to the DHL API."""


class DhlAuthError(DhlApiError):
    """Raised when login/token exchange or refresh fails."""


@dataclass
class TokenSet:
    """Tokens returned by the DHL login endpoint."""

    access_token: str
    id_token: str
    refresh_token: str
    expires_at: float

    @staticmethod
    def from_response(data: dict) -> "TokenSet":
        expires_in = data.get("expires_in", 1800)
        return TokenSet(
            access_token=data["access_token"],
            id_token=data["id_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + float(expires_in),
        )


def extract_code(dhl_login_url_or_code: str) -> str:
    """Extract the authorization code from a pasted dhllogin:// URL.

    Also accepts the bare code value directly (no "://" in it). Anything
    that looks like a URL but has no "code" query parameter (e.g. the
    browser's address bar, which never shows the dhllogin:// redirect) is
    rejected explicitly instead of being misinterpreted as the code.
    """
    value = dhl_login_url_or_code.strip()
    if "://" not in value:
        return value
    parsed = urlparse(value)
    params = parse_qs(parsed.query)
    codes = params.get("code")
    if not codes or not codes[0]:
        raise ValueError("no_code_in_url")
    return codes[0]


class DhlApiClient:
    """Talks to the DHL login and shipment-search endpoints."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def exchange_code(self, code: str) -> TokenSet:
        return await self._request_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": CODE_VERIFIER,
                "redirect_uri": REDIRECT_URI,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._request_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    async def _request_token(self, data: dict) -> TokenSet:
        headers = {
            "Host": "login.dhl.de",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://login.dhl.de",
            "Authorization": BASIC_AUTH_HEADER,
            "User-Agent": USER_AGENT,
            "Accept-Language": "de-de",
        }
        try:
            async with self._session.post(
                TOKEN_URL, data=data, headers=headers
            ) as resp:
                if resp.status != 200:
                    raise DhlAuthError(
                        f"DHL login failed with status {resp.status}"
                    )
                payload = await resp.json(content_type=None)
        except ClientError as err:
            raise DhlAuthError(f"Network error during DHL login: {err}") from err

        try:
            return TokenSet.from_response(payload)
        except KeyError as err:
            raise DhlAuthError(
                f"Unexpected DHL login response, missing {err}"
            ) from err

    async def fetch_shipments(self, id_token: str) -> list[dict]:
        """Fetch the active (non-archived) shipments with full details.

        Mirrors ioBroker.parcel's two-step approach: first fetch the
        overview to get the list of active shipment ids, then fetch the
        details (status, progress, ...) for exactly those ids.
        """
        overview = await self._search(id_token)
        _LOGGER.debug("DHL overview returned %d shipment(s): %s", len(overview), overview)
        active_ids = [
            s["id"]
            for s in overview
            if s.get("sendungsinfo", {}).get("sendungsliste") != ARCHIVED_STATUS
        ]
        if not active_ids:
            return []
        details = await self._search(id_token, piececode=",".join(active_ids))
        _LOGGER.debug("DHL details for %s: %s", active_ids, details)
        return details

    async def _search(self, id_token: str, piececode: str | None = None) -> list[dict]:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 14_8 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            ),
            "accept-language": "de-de",
            "cookie": f"dhli={id_token}",
        }
        params = {"noRedirect": "true", "language": "de", "cid": "app"}
        if piececode:
            params["piececode"] = piececode
        try:
            async with self._session.get(
                SEARCH_URL, headers=headers, params=params, timeout=15
            ) as resp:
                _LOGGER.debug(
                    "DHL search request (piececode=%s) -> status %s",
                    piececode,
                    resp.status,
                )
                if resp.status != 200:
                    raise DhlApiError(
                        f"DHL shipment search failed with status {resp.status}"
                    )
                raw_text = await resp.text()
                _LOGGER.debug("DHL search raw response body: %s", raw_text)
                payload = await resp.json(content_type=None)
        except ClientError as err:
            raise DhlApiError(f"Network error fetching DHL shipments: {err}") from err

        return (payload or {}).get("sendungen", []) or []
