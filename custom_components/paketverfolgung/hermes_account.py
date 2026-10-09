"""Optional myhermes.de account login + shipment auto-discovery.

Reverse-engineered from the myhermes.de web client (login bundle). The flow:

1. ``POST /services/login/token/json`` with ``{"username", "password"}`` and
   an ``X-HMAC`` header. The header is not a secret: the web client computes
   ``base64(HMAC-SHA256(key=username, msg=username + password))`` itself.
   The session lives in cookies afterwards (no token in the body).
2. ``GET /services/receivelist/shipments/`` returns the shipments that are on
   their way to the account owner ("Empfangsübersicht").
3. On HTTP 401 the web client renews the session with
   ``PUT /services/login/token``; when that fails we log in again.

The list only carries the number, sender and a status text; details (history,
delivery window) come from the public tracking client in
``hermes_tracking_api``. Everything is best-effort against an undocumented
API: failures raise ``HermesAuthError`` / ``HermesAccountError`` so the
coordinator can surface them without crashing.

The ``ClientSession`` handed in MUST have its own cookie jar - the shared
Home Assistant session would mix the Hermes login cookies with every other
integration's.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any

from aiohttp import ClientError, ClientSession

from .const import (
    GROUP_DELIVERED,
    GROUP_OUT_FOR_DELIVERY,
    GROUP_REGISTERED,
    GROUP_TRANSIT,
    HERMES_ACCOUNT_BASE,
)

_LOGGER = logging.getLogger(__name__)

_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "de-DE,de;q=0.9",
    "origin": "https://www.myhermes.de",
    "referer": "https://www.myhermes.de/meinkonto/empfangsuebersicht/",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"
    ),
}
_TIMEOUT = 20


class HermesAccountError(Exception):
    """Error talking to the myhermes.de account endpoints."""


class HermesAuthError(HermesAccountError):
    """Login failed, or the session expired and could not be renewed."""


def compute_hmac(username: str, password: str) -> str:
    """The ``X-HMAC`` value the myhermes.de login form sends."""
    digest = hmac.new(
        username.encode("utf-8"),
        (username + password).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def group_from_status(status: str | None) -> str:
    """Coarse lifecycle group from the list's German status text."""
    text = (status or "").lower()
    if "zugestellt" in text or "abgeholt" in text:
        return GROUP_DELIVERED
    if "in zustellung" in text or "zustellfahrzeug" in text or "paketshop" in text:
        return GROUP_OUT_FOR_DELIVERY
    if "angekündigt" in text or "erwartet" in text or "avisiert" in text:
        return GROUP_REGISTERED
    return GROUP_TRANSIT


def parse_shipments(payload: Any) -> list[dict]:
    """Normalise the ``receivelist/shipments`` response.

    Accepts a bare list or an object wrapping one; skips entries without a
    shipment id. Unknown fields are ignored.
    """
    if isinstance(payload, dict):
        for key in ("shipments", "items", "content", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = []
    if not isinstance(payload, list):
        return []

    result: list[dict] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        shipment_id = str(raw.get("shipmentId") or "").strip()
        if not shipment_id or shipment_id in seen:
            continue
        seen.add(shipment_id)
        status = str(raw.get("trackingStatus") or "").strip()
        result.append(
            {
                "id": shipment_id,
                "description": str(raw.get("userDescription") or "").strip(),
                "sender": str(raw.get("sender") or "").strip(),
                "status": status,
                "group": group_from_status(status),
                "last_modified": raw.get("lastModified"),
            }
        )
    return result


class HermesAccountClient:
    """Logs in to myhermes.de and lists the account's incoming shipments."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def login(self, username: str, password: str) -> None:
        """Sign in; raises ``HermesAuthError`` for rejected credentials."""
        headers = {
            **_HEADERS,
            "content-type": "application/json",
            "x-hmac": compute_hmac(username, password),
        }
        try:
            async with self._session.post(
                f"{HERMES_ACCOUNT_BASE}/services/login/token/json",
                json={"username": username, "password": password},
                headers=headers,
                timeout=_TIMEOUT,
            ) as resp:
                status = resp.status
            if status in (400, 401, 403):
                raise HermesAuthError(f"Hermes rejected the login (HTTP {status})")
            if status != 200:
                raise HermesAccountError(f"Hermes login returned HTTP {status}")

            # A rejected login may still answer 200 - confirm the session.
            async with self._session.get(
                f"{HERMES_ACCOUNT_BASE}/services/login/user/current",
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise HermesAuthError("Hermes login did not create a session")
                if resp.status != 200:
                    raise HermesAccountError(
                        f"Hermes user check returned HTTP {resp.status}"
                    )
        except ClientError as err:
            raise HermesAccountError(f"Network error during Hermes login: {err}") from err

    async def _refresh(self) -> bool:
        try:
            async with self._session.put(
                f"{HERMES_ACCOUNT_BASE}/services/login/token",
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                return resp.status == 200
        except ClientError:
            return False

    async def _get_list(self) -> tuple[int, Any]:
        async with self._session.get(
            f"{HERMES_ACCOUNT_BASE}/services/receivelist/shipments/",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                return resp.status, None
            return resp.status, await resp.json(content_type=None)

    async def fetch_shipments(self) -> list[dict]:
        """Return the account's shipments.

        Raises ``HermesAuthError`` when there is no valid session (the
        caller should log in again) and ``HermesAccountError`` otherwise.
        """
        try:
            status, payload = await self._get_list()
            if status in (401, 403) and await self._refresh():
                status, payload = await self._get_list()
        except ClientError as err:
            raise HermesAccountError(f"Network error fetching Hermes list: {err}") from err
        except ValueError as err:
            raise HermesAccountError(f"Hermes list returned no JSON: {err}") from err

        if status in (401, 403):
            raise HermesAuthError("Hermes session expired")
        if status != 200:
            raise HermesAccountError(f"Hermes list returned HTTP {status}")
        _LOGGER.debug("Hermes account list: %s", payload)
        return parse_shipments(payload)
