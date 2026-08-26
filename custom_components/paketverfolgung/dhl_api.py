"""Async DHL tracking and account client."""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiohttp import BasicAuth, ClientError, ClientSession

from .const import (
    APP_USER_AGENT,
    DHL_AUTH_BASE,
    DHL_CLIENT_ID,
    DHL_REDIRECT_URI,
    SEARCH_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class DhlApiError(Exception):
    """Error talking to DHL."""


class DhlAuthError(DhlApiError):
    """DHL authentication failed or expired."""


def extract_authorization_code(redirect_url: str) -> str:
    """Extract the OAuth code from a dhllogin:// redirect URL."""
    if not redirect_url or not redirect_url.startswith("dhllogin://"):
        raise DhlAuthError("The DHL redirect must start with dhllogin://")
    code = parse_qs(urlparse(redirect_url).query).get("code", [None])[0]
    if not code:
        raise DhlAuthError("No authorization code found in DHL redirect URL")
    return code


def _jwt_expiring(id_token: str | None, within_seconds: int = 600) -> bool:
    """Return True when an ID token is missing, invalid, or expires soon."""
    if not id_token:
        return True
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return float(claims.get("exp", 0)) <= time.time() + within_seconds
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return True


class DhlApiClient:
    """Talk to DHL's shipment tracking and account endpoints."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Exchange an authorization code for a persistent DHL OAuth session."""
        data = {
            "redirect_uri": DHL_REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
            "code": code,
        }
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://login.dhl.de",
            "user-agent": APP_USER_AGENT,
            "accept-language": "de-de",
        }
        return await self._token_request(data, headers)

    async def refresh_session(self, session: dict[str, Any]) -> dict[str, Any]:
        """Refresh the DHL session when the ID token is close to expiry."""
        if not _jwt_expiring(session.get("id_token")):
            return session
        refresh_token = session.get("refresh_token")
        if not refresh_token:
            raise DhlAuthError("DHL session has no refresh token")
        data = {
            "redirect_uri": DHL_REDIRECT_URI,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://login.dhl.de",
            "user-agent": APP_USER_AGENT,
            "accept-language": "de-de",
        }
        refreshed = await self._token_request(data, headers)
        if not refreshed.get("refresh_token"):
            refreshed["refresh_token"] = refresh_token
        return refreshed

    async def _token_request(
        self, data: dict[str, str], headers: dict[str, str]
    ) -> dict[str, Any]:
        try:
            async with self._session.post(
                f"{DHL_AUTH_BASE}/token",
                data=data,
                headers=headers,
                auth=BasicAuth(DHL_CLIENT_ID, ""),
                timeout=20,
            ) as resp:
                payload = await resp.json(content_type=None)
                if resp.status != 200 or not payload.get("id_token"):
                    response_keys = (
                        sorted(str(key) for key in payload)
                        if isinstance(payload, dict)
                        else []
                    )
                    _LOGGER.debug(
                        "DHL token request failed (status=%s, response_keys=%s)",
                        resp.status,
                        response_keys,
                    )
                    raise DhlAuthError(
                        f"DHL token request failed with status {resp.status}"
                    )
                return payload
        except ClientError as err:
            raise DhlAuthError(f"Network error during DHL login: {err}") from err

    async def fetch_account_tracking_numbers(
        self, session: dict[str, Any]
    ) -> list[str]:
        """Fetch non-archived shipment IDs linked to the authenticated DHL account."""
        id_token = session.get("id_token")
        if not id_token:
            raise DhlAuthError("DHL session has no ID token")
        headers = self._tracking_headers()
        headers["cookie"] = f"dhli={id_token}"
        params = {"noRedirect": "true", "language": "de", "cid": "app"}
        payload = await self._search(params, headers)
        result: list[str] = []
        for shipment in payload.get("sendungen", []) or []:
            info = shipment.get("sendungsinfo") or {}
            if info.get("sendungsliste") == "ARCHIVIERT":
                continue
            shipment_id = shipment.get("id")
            if shipment_id and shipment_id not in result:
                result.append(shipment_id)
        return result

    async def fetch_shipments(self, tracking_numbers: list[str]) -> list[dict]:
        """Fetch current details for the given tracking numbers."""
        if not tracking_numbers:
            return []
        params = {
            "piececode": ",".join(tracking_numbers),
            "noRedirect": "true",
            "language": "de",
            "cid": "app",
        }
        payload = await self._search(params, self._tracking_headers())
        return payload.get("sendungen", []) or []

    async def _search(
        self, params: dict[str, str], headers: dict[str, str]
    ) -> dict[str, Any]:
        try:
            async with self._session.get(
                SEARCH_URL, headers=headers, params=params, timeout=15
            ) as resp:
                payload = await resp.json(content_type=None)
                _LOGGER.debug("DHL search request -> status %s", resp.status)
                if resp.status == 401:
                    raise DhlAuthError("DHL account session is unauthorized")
                if resp.status != 200:
                    raise DhlApiError(
                        f"DHL shipment search failed with status {resp.status}"
                    )
                return payload or {}
        except ClientError as err:
            raise DhlApiError(f"Network error fetching DHL shipments: {err}") from err

    @staticmethod
    def _tracking_headers() -> dict[str, str]:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": USER_AGENT,
            "accept-language": "de-de",
        }
