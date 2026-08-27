"""Minimal async client for DPD's Paketnavigator SOAP API.

Reverse-engineered from the DPD Android app v4.1.2 (package de.dpd.mobile,
"Paketnavigator3") via the open-source ioBroker.parcel adapter
(https://github.com/TA2k/ioBroker.parcel/blob/master/lib/dpdLogin.js).
Needs a real myDPD account login - DPD's public web tracking page requires
a ZIP code plus a CAPTCHA-guarded ASP.NET form, which wasn't worth
automating, so this authenticates the same way the app itself does.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from aiohttp import ClientError, ClientSession

from .const import (
    DPD_API_VERSION,
    DPD_LANGUAGE,
    DPD_NS,
    DPD_PARTNER_NAME,
    DPD_PARTNER_PASSWORD,
    DPD_PARTNER_TOKEN,
    DPD_SERVICE_URL,
)

_LOGGER = logging.getLogger(__name__)


class DpdApiError(Exception):
    """Generic error talking to the DPD SOAP API."""


class DpdAuthError(DpdApiError):
    """Raised when login fails or a session token is no longer valid."""


@dataclass
class DpdSession:
    """A logged-in DPD SOAP session."""

    session_token: str
    cloud_user_id: int


def _compute_key_phase(cloud_user_id: int, endpoint_name: str) -> str:
    """Time-boxed (~1 minute) request signature the API expects.

    Matches the algorithm in the app's APIHelper.smali: MD5 of
    time_seed + partner name + cloud user id + endpoint name + partner
    password, base64-encoded and truncated, prefixed with the time seed.
    """
    now = datetime.now(timezone.utc)
    time_seed = str((now.hour * 60 + now.minute + 1000) * 3)
    payload = (
        f"{time_seed}{DPD_PARTNER_NAME}{cloud_user_id or 0}"
        f"{endpoint_name}{DPD_PARTNER_PASSWORD}"
    )
    md5_b64 = base64.b64encode(hashlib.md5(payload.encode("utf-8")).digest()).decode()
    return time_seed + md5_b64[:16]


def _xml_escape(value: str | None) -> str:
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _pick_xml(xml: str | None, tag: str) -> str | None:
    """Extract the text content of the first `<tag>` in an XML string.

    Deliberately simple regex-based extraction (matching the proven
    ioBroker.parcel implementation) instead of full XML parsing, since the
    SOAP response is stable UTF-8 without CDATA/comments and this sidesteps
    namespace-handling headaches with a full XML parser.
    """
    if not xml:
        return None
    escaped = re.escape(tag)
    tag_pattern = r"(?:[a-zA-Z_][\w.-]*:)?" + escaped
    open_match = re.search(r"<" + tag_pattern + r"(?:\s[^>]*?)?(/?)>", xml)
    if not open_match:
        return None
    if open_match.group(1) == "/":
        return ""
    after_open = xml[open_match.end() :]
    close_match = re.search(r"</" + tag_pattern + r">", after_open)
    if not close_match:
        return None
    raw = after_open[: close_match.start()]
    return (
        raw.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )


def _partner_credentials(key_phase: str) -> str:
    return (
        f'<PartnerCredentials xmlns="{DPD_NS}"><Name>{DPD_PARTNER_NAME}</Name>'
        f"<Token>{DPD_PARTNER_TOKEN}</Token><KeyPhase>{_xml_escape(key_phase)}</KeyPhase>"
        "</PartnerCredentials>"
    )


def _device_data() -> str:
    return (
        "<DeviceData><Version>1</Version><HardwareID>home-assistant</HardwareID>"
        "<BootSystemID>Android_Phone</BootSystemID><Name>Home Assistant</Name>"
        "<AppVersion>4.1.2</AppVersion><PushToken></PushToken>"
        "<AllowPushNotifications>false</AllowPushNotifications></DeviceData>"
    )


_TRACKING_LISTS = [
    ("SendTrackingDataList", "SendTrackingData", "send"),
    ("ReceiveTrackingDataList", "ReceiveTrackingData", "receive"),
    ("ReturnTrackingDataList", "ReturnTrackingData", "return"),
]


def _parse_tracking_lists(xml: str) -> list[dict]:
    """Extract parcels from the three lists in a getSessionFullState response.

    The server returns empty lists as self-closing tags (e.g.
    `<SendTrackingDataList />`), which the regexes below tolerate.
    """
    parcels: list[dict] = []
    ns_prefix = r"(?:[a-zA-Z_][\w.-]*:)?"
    for list_tag, item_tag, direction in _TRACKING_LISTS:
        list_open = re.search(r"<" + ns_prefix + list_tag + r"(?:\s[^>]*?)?(/?)>", xml)
        if not list_open or list_open.group(1) == "/":
            continue
        body_match = re.search(
            r"<"
            + ns_prefix
            + list_tag
            + r"(?:\s[^>]*)?>([\s\S]*?)</"
            + ns_prefix
            + list_tag
            + r">",
            xml,
        )
        if not body_match:
            continue
        inner = body_match.group(1)
        item_re = re.compile(
            r"<" + ns_prefix + item_tag + r"(?:\s[^>]*)?>([\s\S]*?)</" + ns_prefix + item_tag + r">"
        )
        for item_match in item_re.finditer(inner):
            item = item_match.group(1)
            parcel_no = _pick_xml(item, "ParcelNo")
            if not parcel_no:
                continue
            last_status_info = _pick_xml(item, "LastStatusInfo") or ""
            parcels.append(
                {
                    "id": parcel_no,
                    "name": _pick_xml(item, "ParcelNicName") or parcel_no,
                    "status": (
                        _pick_xml(last_status_info, "StatusText_Mobile")
                        or _pick_xml(item, "StatusText_Mobile")
                        or _pick_xml(item, "DataViewStatus")
                        or ""
                    ),
                    "status_id": (
                        _pick_xml(last_status_info, "StatusID")
                        or _pick_xml(item, "StatusID")
                        or ""
                    ),
                    "delivered": _pick_xml(item, "Delivered") == "true",
                    "direction": direction,
                }
            )
    return parcels


class DpdApiClient:
    """Talks to the DPD Paketnavigator SOAP endpoint."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def _call(self, operation: str, body: str) -> str:
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soap:Body><{operation} xmlns="{DPD_NS}">{body}</{operation}></soap:Body>'
            "</soap:Envelope>"
        )
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": DPD_NS + operation,
            "User-Agent": "ksoap2-android/2.6.0+",
            "Accept": "text/xml",
        }
        try:
            async with self._session.post(
                DPD_SERVICE_URL, data=envelope, headers=headers, timeout=30
            ) as resp:
                _LOGGER.debug("DPD %s -> status %s", operation, resp.status)
                if resp.status != 200:
                    raise DpdApiError(f"DPD API returned status {resp.status}")
                return await resp.text()
        except ClientError as err:
            raise DpdApiError(f"Network error contacting DPD: {err}") from err

    async def login(self, username: str, password: str) -> DpdSession:
        """Two-step login: get an anonymous session, then log the user in with it."""
        anon_body = (
            f'<getSessionFullStateRequest xmlns="{DPD_NS}">'
            f"<Version>{DPD_API_VERSION}</Version><Language>{DPD_LANGUAGE}</Language>"
            f'{_partner_credentials(_compute_key_phase(0, "getSessionFullState"))}'
            f"<SessionToken></SessionToken>{_device_data()}</getSessionFullStateRequest>"
        )
        anon_text = await self._call("getSessionFullState", anon_body)
        anon_token = _pick_xml(anon_text, "SessionToken")
        if not anon_token:
            raise DpdAuthError("DPD did not return an anonymous session token")

        login_body = (
            f'<getUserLoginRequest xmlns="{DPD_NS}">'
            f"<Version>{DPD_API_VERSION}</Version><Language>{DPD_LANGUAGE}</Language>"
            f'{_partner_credentials(_compute_key_phase(0, "getUserLogin"))}'
            f"<SessionToken>{_xml_escape(anon_token)}</SessionToken>"
            f"<UserName>{_xml_escape(username)}</UserName>"
            f"<UserPassword>{_xml_escape(password)}</UserPassword>"
            "</getUserLoginRequest>"
        )
        login_text = await self._call("getUserLogin", login_body)
        if _pick_xml(login_text, "Ack") != "true":
            error_msg = _pick_xml(login_text, "ErrorMsg") or "Login abgelehnt"
            raise DpdAuthError(error_msg)

        session_token = _pick_xml(login_text, "SessionToken")
        cloud_user_id_raw = _pick_xml(login_text, "cloudUserID")
        if not session_token or not cloud_user_id_raw:
            raise DpdAuthError("DPD login response missing SessionToken/cloudUserID")
        return DpdSession(session_token=session_token, cloud_user_id=int(cloud_user_id_raw))

    async def fetch_parcels(self, session: DpdSession) -> tuple[list[dict], DpdSession]:
        """Fetch all parcels on the account (send/receive/return).

        Returns `(parcels, session)`, where `session` is the same object
        unless DPD rotated the token, in which case it's a fresh one the
        caller should persist. Raises DpdAuthError if the session is no
        longer valid - the caller should log in again and retry.
        """
        body = (
            f'<getSessionFullStateRequest xmlns="{DPD_NS}">'
            f"<Version>{DPD_API_VERSION}</Version><Language>{DPD_LANGUAGE}</Language>"
            f'{_partner_credentials(_compute_key_phase(session.cloud_user_id, "getSessionFullState"))}'
            f"<SessionToken>{_xml_escape(session.session_token)}</SessionToken>"
            f"{_device_data()}</getSessionFullStateRequest>"
        )
        text = await self._call("getSessionFullState", body)
        if _pick_xml(text, "Ack") != "true":
            error_code = _pick_xml(text, "ErrorCode") or "unknown"
            raise DpdAuthError(f"DPD session invalid ({error_code})")

        new_token = _pick_xml(text, "SessionToken")
        refreshed = session
        if new_token and new_token != session.session_token:
            refreshed = DpdSession(session_token=new_token, cloud_user_id=session.cloud_user_id)

        return _parse_tracking_lists(text), refreshed
