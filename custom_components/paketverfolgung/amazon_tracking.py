"""Amazon tracking timeline extraction for Paketverfolgung.

Amazon's order page exposes the current shipment status quite reliably, while
its full tracking history is spread across different HTML/state layouts. This
module keeps timeline parsing isolated from login/order discovery and enriches
Amazon shipments with the same ``events`` shape used by the other providers.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import ClientError
from bs4 import BeautifulSoup
from yarl import URL

from .amazon_api import AMAZON_BASE, AMAZON_USER_AGENT, AmazonApiClient

_LOGGER = logging.getLogger(__name__)
_BERLIN = ZoneInfo("Europe/Berlin")

_MONTHS = {
    "januar": 1,
    "jan": 1,
    "january": 1,
    "februar": 2,
    "feb": 2,
    "february": 2,
    "märz": 3,
    "maerz": 3,
    "mrz": 3,
    "mär": 3,
    "march": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "may": 5,
    "juni": 6,
    "jun": 6,
    "june": 6,
    "juli": 7,
    "jul": 7,
    "july": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "oktober": 10,
    "okt": 10,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "dezember": 12,
    "dez": 12,
    "december": 12,
    "dec": 12,
}

_DATE_KEYS = (
    "eventDate",
    "eventTime",
    "dateTime",
    "timestamp",
    "trackingDate",
    "displayDate",
    "date",
    "time",
)
_STATUS_KEYS = (
    "eventMessage",
    "primaryMessage",
    "statusMessage",
    "description",
    "message",
    "status",
    "shortStatus",
    "detail",
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_hour(hour: int, ampm: str | None) -> int:
    if not ampm:
        return hour
    marker = ampm.upper()
    if marker == "AM":
        return 0 if hour == 12 else hour
    if marker == "PM":
        return hour if hour == 12 else hour + 12
    return hour


def _build_datetime(
    day: int,
    month: int,
    year_raw: str | None,
    hour_raw: str | None,
    minute_raw: str | None,
    ampm: str | None = None,
) -> datetime | None:
    now = datetime.now(_BERLIN)
    year = int(year_raw) if year_raw else now.year
    if year < 100:
        year += 2000
    hour = _normalize_hour(int(hour_raw or 0), ampm)
    minute = int(minute_raw or 0)
    try:
        result = datetime(year, month, day, hour, minute, tzinfo=_BERLIN)
    except ValueError:
        return None

    # Amazon omits the year for recent tracking events. Around New Year, a
    # December event shown in January therefore belongs to the previous year.
    if not year_raw and result > now.replace(hour=23, minute=59) and (
        result - now
    ).days > 7:
        result = result.replace(year=year - 1)
    return result


def _event_datetime(value: Any) -> datetime | None:
    """Parse common German/English Amazon tracking date representations."""
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, tz=_BERLIN)
        except (OverflowError, OSError, ValueError):
            return None

    text = _clean(value)
    if not text:
        return None

    if re.fullmatch(r"\d{10,13}", text):
        stamp = float(text)
        if stamp > 10_000_000_000:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, tz=_BERLIN)
        except (OverflowError, OSError, ValueError):
            return None

    iso_text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_BERLIN)
        return parsed.astimezone(_BERLIN)
    except ValueError:
        pass

    # 29.08.2026, 10:07 / 29.08., 10:07 / 29.08.2026
    match = re.search(
        r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})\."
        r"(?:(?P<year>\d{2,4})\.?)?"
        r"(?:\s*(?:,|um|at)?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})"
        r"(?:\s*(?P<ampm>AM|PM))?)?",
        text,
        flags=re.I,
    )
    if match:
        return _build_datetime(
            int(match.group("day")),
            int(match.group("month")),
            match.group("year"),
            match.group("hour"),
            match.group("minute"),
            match.group("ampm"),
        )

    # 29. August 2026, 10:07 / Samstag, 29. August, 10:07
    match = re.search(
        r"(?P<day>\d{1,2})\.?(?:\s+)(?P<month>[A-Za-zÄÖÜäöüß]+)\.?"
        r"(?:\s+(?P<year>\d{4}))?"
        r"(?:\s*(?:,|um|at)?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})"
        r"(?:\s*(?P<ampm>AM|PM))?)?",
        text,
        flags=re.I,
    )
    if match:
        month = _MONTHS.get(match.group("month").lower())
        if month:
            return _build_datetime(
                int(match.group("day")),
                month,
                match.group("year"),
                match.group("hour"),
                match.group("minute"),
                match.group("ampm"),
            )

    # Tuesday, June 4, 3:13 PM / June 4, 2026 15:13
    match = re.search(
        r"(?P<month>[A-Za-z]+)\.?(?:\s+)(?P<day>\d{1,2})"
        r"(?:,?\s+(?P<year>\d{4}))?"
        r"(?:\s*(?:,|at)?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})"
        r"(?:\s*(?P<ampm>AM|PM))?)?",
        text,
        flags=re.I,
    )
    if match:
        month = _MONTHS.get(match.group("month").lower())
        if month:
            return _build_datetime(
                int(match.group("day")),
                month,
                match.group("year"),
                match.group("hour"),
                match.group("minute"),
                match.group("ampm"),
            )

    return None


def _pick(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, "", [], {}):
            return value
    return None


_NO_LOCATION = {"-", "—", "unbekannt", "unknown", "n/a"}


def _append_event(
    events: list[dict[str, str]],
    date_value: Any,
    status_value: Any,
    location_value: Any = None,
) -> None:
    status = _clean(status_value)
    moment = _event_datetime(date_value)
    if not status or moment is None:
        return
    if status.lower() in {"details", "weitere details", "alle aktualisierungen"}:
        return
    event = {"datum": moment.isoformat(), "status": status}
    location = _clean(location_value)
    if location and len(location) <= 160 and location.casefold() not in _NO_LOCATION:
        event["location"] = location
    events.append(event)


def _events_from_tracking_modal(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Parse Amazon's dedicated "all tracking updates" modal."""
    container = soup.select_one("#tracking-events-container")
    if container is None:
        container = soup.select_one("#a-popover-tracking-events-modal")
    if container is None:
        return []

    events: list[dict[str, str]] = []
    for header in container.select(".tracking-event-date-header"):
        date_node = header.select_one(".tracking-event-date")
        date_text = _clean(
            " ".join((date_node or header).stripped_strings)
        )
        group = header.parent
        if group is None:
            continue

        for message_node in group.select(".tracking-event-message"):
            status = _clean(" ".join(message_node.stripped_strings))
            if not status:
                continue

            event_row = message_node.parent
            while event_row is not None and event_row is not group:
                if event_row.select_one(".tracking-event-time") is not None:
                    break
                event_row = event_row.parent

            time_node = (
                event_row.select_one(".tracking-event-time")
                if event_row is not None and event_row is not group
                else None
            )
            time_text = _clean(" ".join(time_node.stripped_strings)) if time_node else ""
            date_time = f"{date_text} {time_text}".strip()
            # The place ("Erftstadt, DE") sits in the same row as the time;
            # only taken when the row holds this one message.
            location_node = (
                event_row.select_one(".tracking-event-location")
                if event_row is not None
                and event_row is not group
                and len(event_row.select(".tracking-event-message")) == 1
                else None
            )
            location = (
                " ".join(location_node.stripped_strings) if location_node else None
            )
            _append_event(events, date_time, status, location)

    return events


def _events_from_state(state: Any) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            date_value = _pick(value, _DATE_KEYS)
            status_value = _pick(value, _STATUS_KEYS)
            if date_value is not None and status_value is not None:
                if isinstance(status_value, dict):
                    status_value = _pick(status_value, _STATUS_KEYS)
                _append_event(events, date_value, status_value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(state)
    return events


def _events_from_generic_dom(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Fallback for newer Amazon layouts without the classic modal."""
    events: list[dict[str, str]] = []
    selectors = (
        ".pt-tracking-event",
        ".pt-event-container",
        ".tracking-event",
        "[class*='shipment-event']",
        "[class*='event-container']",
        "[class*='trackingEvent']",
    )

    seen_nodes: set[int] = set()
    for selector in selectors:
        for node in soup.select(selector):
            node_id = id(node)
            if node_id in seen_nodes:
                continue
            seen_nodes.add(node_id)

            text = _clean(" ".join(node.stripped_strings))
            if not text:
                continue

            date_node = node.select_one("[class*='date']")
            time_node = node.select_one("[class*='time']")
            date_text = _clean(" ".join(date_node.stripped_strings)) if date_node else ""
            time_text = _clean(" ".join(time_node.stripped_strings)) if time_node else ""
            moment = _event_datetime(f"{date_text} {time_text}".strip())
            if moment is None:
                moment = _event_datetime(text)
            if moment is None:
                continue

            status_node = (
                node.select_one("[class*='status']")
                or node.select_one("[class*='message']")
                or node.select_one("[class*='description']")
            )
            status = (
                _clean(" ".join(status_node.stripped_strings)) if status_node else text
            )
            if date_text and status == text:
                status = _clean(status.replace(date_text, "", 1))
            if time_text and status == text:
                status = _clean(status.replace(time_text, "", 1))
            if status:
                events.append({"datum": moment.isoformat(), "status": status})

    return events


def parse_amazon_events(html: str) -> list[dict[str, str]]:
    """Return Amazon tracking events in newest-first shared event format."""
    soup = BeautifulSoup(html, "html.parser")
    events = _events_from_tracking_modal(soup)

    for script in soup.select('script[data-a-state*="page-state"]'):
        if not script.string:
            continue
        try:
            state = json.loads(script.string)
        except (TypeError, ValueError):
            continue
        events.extend(_events_from_state(state))

    if not events:
        events.extend(_events_from_generic_dom(soup))

    unique: dict[tuple[str, str], dict[str, str]] = {}
    for event in events:
        key = (event["datum"], event["status"].casefold())
        known = unique.get(key)
        if known and known.get("location") and not event.get("location"):
            event = {**event, "location": known["location"]}
        unique[key] = event

    return sorted(unique.values(), key=lambda event: event["datum"], reverse=True)


# Amazon's own map on the tracking page asks this address for the delivery
# van's position. Only Amazon's own vans report one, and only on the last
# stretch, so it is asked only with a live stop countdown.
MAP_TRACKING_URL = (
    "https://www.amazon.de/progress-tracker/package/actions/map-tracking-deans-proxy"
)
VEHICLE_MAX_STOPS = 10
_VEHICLE_STATES = {"PICKED_UP", "RENDERED", "SKIPPED_STOP", "YOU_ARE_NEXT"}
_AMAZON_VANS = {"amazon", "amzl", "amazon logistics", "amazon shipping"}
_CSRF_RE = re.compile(r"csrfToken\s*[=:]\s*[\"']([^\"']{8,4096})[\"']")
_SESSION_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")


def vehicle_wanted(shipment: dict[str, Any]) -> bool:
    """A live countdown on an Amazon van: worth asking for its position."""
    stops = shipment.get("stops_away")
    tracking_id = str(shipment.get("tracking_id") or "")
    carrier = str(shipment.get("carrier") or "").strip().casefold()
    if type(stops) is not int or not 0 <= stops <= VEHICLE_MAX_STOPS:
        return False
    if not re.fullmatch(r"[A-Z0-9-]{6,64}", tracking_id, re.I):
        return False
    return carrier in _AMAZON_VANS or (
        not carrier and tracking_id.upper().startswith("DE")
    )


def map_request_form(html: str, tracking_id: str) -> dict[str, str] | None:
    """Form for the map request, from the tracking page already loaded."""
    soup = BeautifulSoup(html, "html.parser")
    state: dict[str, Any] | None = None
    for script in soup.select('script[data-a-state*="page-state"]'):
        try:
            value = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("mapTracking"), dict):
            state = value
            break
    if state is None:
        return None
    code = state.get("trackingId") or tracking_id
    if not isinstance(code, str) or code.upper() != tracking_id.upper():
        return None
    token = state.get("csrfToken")
    if not isinstance(token, str) or len(token) < 8:
        tag = soup.select_one('input[name="csrfToken"]')
        token = tag.get("value") if tag else None
    if not isinstance(token, str) or len(token) < 8:
        match = _CSRF_RE.search(html)
        token = match.group(1) if match else None
    if not isinstance(token, str) or not 8 <= len(token) <= 4096:
        return None
    return {"trackingId": code, "csrfToken": token}


def parse_vehicle_position(
    data: Any, tracking_id: str, now: datetime
) -> dict[str, Any] | None:
    """The van's current position from the map answer, or None.

    Positions older than ten minutes (or from the future) are dropped, so a
    stale point is never shown as live.
    """
    if not isinstance(data, dict) or data.get("success") is not True:
        return None
    details = data.get("packageLocationDetails")
    if not isinstance(details, dict):
        return None
    if details.get("trackingObjectState") not in _VEHICLE_STATES:
        return None
    actual = details.get("trackingObjectId")
    if actual is not None and (
        not isinstance(actual, str) or actual.upper() != tracking_id.upper()
    ):
        return None
    transporter = details.get("transporterDetails")
    fix = transporter.get("geoLocation") if isinstance(transporter, dict) else None
    if not isinstance(fix, dict):
        return None
    lat, lon = fix.get("latitude"), fix.get("longitude")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (lat, lon)):
        return None
    if not (-85.06 <= lat <= 85.06 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    position_at = None
    stamp = fix.get("locationTime")
    if stamp is not None:
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            return None
        if stamp > 1e11:  # milliseconds
            stamp /= 1000
        try:
            moment = datetime.fromtimestamp(stamp, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
        if not -120 <= (now - moment).total_seconds() <= 600:
            return None
        position_at = moment.isoformat()
    accuracy = fix.get("accuracy")
    if type(accuracy) not in (int, float) or not math.isfinite(accuracy) or accuracy < 0:
        accuracy = None
    return {
        "latitude": lat,
        "longitude": lon,
        "position_at": position_at,
        "accuracy": accuracy,
        "fetched_at": now.isoformat(),
    }


class AmazonTrackingApiClient(AmazonApiClient):
    """Amazon client that enriches base shipment parsing with full history.

    The tracking page is fetched once and reused for both the status parse
    and the timeline parse (no second request per shipment).
    """

    async def _fetch_tracking_page(self, url: str, desc: str) -> dict[str, Any] | None:
        html, final_url = await self._load_tracking_html(url)
        shipment = self._parse_tracking_page(html, final_url, desc)
        if not shipment:
            return None
        shipment["events"] = parse_amazon_events(html)
        if vehicle_wanted(shipment):
            shipment["vehicle"] = await self._fetch_vehicle(
                html, final_url, shipment["tracking_id"]
            )
        _LOGGER.debug(
            "Amazon: parsed %d tracking event(s) for %s",
            len(shipment["events"]),
            shipment.get("tracking_id"),
        )
        return shipment

    async def _fetch_vehicle(
        self, html: str, referer: str, tracking_id: str
    ) -> dict[str, Any] | None:
        """One extra request per poll for the van's position.

        Never fails the poll: without a position the parcel is shown as before.
        """
        form = map_request_form(html, tracking_id)
        if form is None:
            _LOGGER.debug("Amazon: no map on the tracking page of %s", tracking_id)
            return None
        cookie = self._jar.filter_cookies(URL(MAP_TRACKING_URL)).get("session-id")
        session_id = getattr(cookie, "value", None)
        if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
            return None
        headers = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "de-DE,de;q=0.9,en;q=0.7",
            "content-type": "application/x-www-form-urlencoded",
            "x-requested-with": "XMLHttpRequest",
            "origin": AMAZON_BASE,
            "referer": referer,
            "user-agent": AMAZON_USER_AGENT,
            "x-amzn-sessionid": session_id,
        }
        try:
            async with self._session.post(
                MAP_TRACKING_URL,
                data=form,
                headers=headers,
                allow_redirects=False,
                timeout=15,
            ) as response:
                if response.status != 200:
                    _LOGGER.debug(
                        "Amazon: map request for %s answered %s",
                        tracking_id,
                        response.status,
                    )
                    return None
                data = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug(
                "Amazon: map request for %s failed (%s)", tracking_id, type(err).__name__
            )
            return None
        return parse_vehicle_position(data, tracking_id, datetime.now(timezone.utc))
