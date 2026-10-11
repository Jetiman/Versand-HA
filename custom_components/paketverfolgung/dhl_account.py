"""Optional DHL account login + shipment auto-discovery.

Reverse-engineered from the iOS DHL app (de.deutschepost.dhl). The flow:

1. Build an OAuth/PKCE ``authorize`` URL, the user opens it and signs in.
2. DHL redirects the browser to ``dhllogin://.../login?code=...``; the user
   pastes that whole URL back into Home Assistant.
3. The ``code`` is exchanged for an ``id_token`` / ``refresh_token`` pair.
4. The public ``int-verfolgen/data/search`` endpoint, called with
   ``Cookie: dhli=<id_token>`` and no piececode, returns the account's
   (non-archived) shipments.

Everything here is best-effort against an undocumented API; failures raise
``DhlAuthError`` (login rejected / session expired - the user has to sign in
again) or its subclass ``DhlConnectionError`` (network or server trouble -
worth retrying later) so the coordinator can surface them without crashing.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from aiohttp import BasicAuth, ClientError, ClientSession

from .const import (
    APP_USER_AGENT,
    DHL_AUTH_BASE,
    DHL_CLIENT_ID,
    DHL_LOGIN_CLAIMS,
    DHL_LOGIN_STATE,
    DHL_REDIRECT_URI,
    SEARCH_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class DhlAuthError(Exception):
    """DHL login failed, expired, or was rejected."""


class DhlConnectionError(DhlAuthError):
    """DHL could not be reached or answered with a server error.

    A subclass of ``DhlAuthError`` so older callers that only catch the base
    class keep working; the dedicated DHL-account entry tells the two apart
    (retry later vs. ask the user to sign in again).
    """


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_login(nonce: str | None = None) -> tuple[str, str]:
    """Return ``(code_verifier, authorize_url)`` for a fresh login attempt."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "redirect_uri": DHL_REDIRECT_URI,
        "state": DHL_LOGIN_STATE,
        "client_id": DHL_CLIENT_ID,
        "response_type": "code",
        "scope": "openid offline_access",
        "claims": DHL_LOGIN_CLAIMS,
        "nonce": nonce or _b64url(secrets.token_bytes(16)),
        "login_hint": "",
        "prompt": "login",
        "ui_locales": "de-DE",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return verifier, f"{DHL_AUTH_BASE}/authorize?{urlencode(params)}"


_REDIRECT_RE = re.compile(r"dhllogin://[^\s'\"<>`]+", re.IGNORECASE)


def extract_code(redirect_url: str) -> str:
    """Pull the ``code`` out of the pasted ``dhllogin://...`` redirect.

    After the DHL sign-in the browser cannot open the ``dhllogin://`` address,
    so it is not shown in the address bar - it only turns up in the browser's
    developer console (F12 -> Console). People therefore paste whatever they
    copied there: the bare address, or the whole console line around it
    (``Failed to launch 'dhllogin://...?code=...' because ...``), sometimes
    HTML-escaped. All of that is accepted; the address is cut out of the text.
    """
    text = html.unescape((redirect_url or "").strip())
    match = _REDIRECT_RE.search(text)
    if not match:
        raise DhlAuthError("Die DHL-Weiterleitung muss mit dhllogin:// beginnen.")
    address = match.group(0).rstrip(".,;:)")
    code = parse_qs(urlparse(address).query).get("code", [None])[0]
    if not code:
        raise DhlAuthError("In der DHL-Weiterleitung wurde kein Code gefunden.")
    return code


def account_label(id_token: str | None) -> str | None:
    """The account's e-mail address from the ID token (for the entry title)."""
    if not id_token:
        return None
    try:
        part = id_token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part.encode()))
    except (ValueError, IndexError, TypeError):
        return None
    email = claims.get("email") if isinstance(claims, dict) else None
    return str(email).strip() or None if email else None


def _id_token_expiring(id_token: str | None, within: int = 600) -> bool:
    if not id_token:
        return True
    try:
        part = id_token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part.encode()))
        return float(claims.get("exp", 0)) <= time.time() + within
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return True


class DhlAccountClient:
    """Talks to DHL's OAuth token endpoint and the account shipment list."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        return await self._token(
            {
                "redirect_uri": DHL_REDIRECT_URI,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "code": code,
            }
        )

    async def ensure_fresh(self, dhl_session: dict[str, Any]) -> dict[str, Any]:
        """Refresh the token pair if the ID token is close to expiry."""
        if not _id_token_expiring(dhl_session.get("id_token")):
            return dhl_session
        refresh_token = dhl_session.get("refresh_token")
        if not refresh_token:
            raise DhlAuthError("DHL-Sitzung ohne Refresh-Token - bitte neu anmelden.")
        refreshed = await self._token(
            {
                "redirect_uri": DHL_REDIRECT_URI,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        refreshed.setdefault("refresh_token", refresh_token)
        return refreshed

    async def _token(self, data: dict[str, str]) -> dict[str, Any]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://login.dhl.de",
            "user-agent": APP_USER_AGENT,
            "accept-language": "de-de",
        }
        try:
            async with self._session.post(
                f"{DHL_AUTH_BASE}/token",
                data=data,
                headers=headers,
                auth=BasicAuth(DHL_CLIENT_ID, ""),
                timeout=20,
            ) as resp:
                status = resp.status
                try:
                    payload = await resp.json(content_type=None)
                except ValueError:
                    payload = None
                if status == 200 and isinstance(payload, dict) and payload.get(
                    "id_token"
                ):
                    return payload
                keys = sorted(map(str, payload)) if isinstance(payload, dict) else []
                _LOGGER.debug("DHL token request failed: status=%s keys=%s", status, keys)
                message = f"DHL-Anmeldung fehlgeschlagen (Status {status})."
                if status >= 500 or status == 429:
                    raise DhlConnectionError(message)
                raise DhlAuthError(message)
        except (ClientError, TimeoutError) as err:
            raise DhlConnectionError(
                f"Netzwerkfehler bei der DHL-Anmeldung: {err}"
            ) from err

    async def fetch_shipment_ids(self, dhl_session: dict[str, Any]) -> list[str]:
        """Non-archived, non-return shipment IDs (piececodes) of the account."""
        id_token = dhl_session.get("id_token")
        if not id_token:
            raise DhlAuthError("DHL-Sitzung ohne ID-Token - bitte neu anmelden.")
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": USER_AGENT,
            "accept-language": "de-de",
            "cookie": f"dhli={id_token}",
        }
        params = {"noRedirect": "true", "language": "de", "cid": "app"}
        try:
            async with self._session.get(
                SEARCH_URL, headers=headers, params=params, timeout=15
            ) as resp:
                if resp.status in (401, 403):
                    raise DhlAuthError("DHL-Konto-Sitzung ist abgelaufen.")
                if resp.status != 200:
                    raise DhlConnectionError(
                        f"DHL-Kontoabfrage fehlgeschlagen (Status {resp.status})."
                    )
                payload = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            raise DhlConnectionError(
                f"Netzwerkfehler bei der DHL-Kontoabfrage: {err}"
            ) from err

        _LOGGER.debug("DHL account discovery response: %s", payload)
        ids: list[str] = []
        for shipment in (payload or {}).get("sendungen", []) or []:
            info = shipment.get("sendungsinfo") or {}
            if info.get("sendungsliste") == "ARCHIVIERT":
                continue
            details = shipment.get("sendungsdetails") or {}
            if details.get("ruecksendung") is True or details.get("retoure") is True:
                # Returns ("Rücksendung an den Absender") never arrive at your
                # door, so they are not tracked at all.
                continue
            shipment_id = shipment.get("id")
            if shipment_id and shipment_id not in ids:
                ids.append(shipment_id)
        return ids
