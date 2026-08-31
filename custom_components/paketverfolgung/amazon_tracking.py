"""Amazon tracking timeline extraction for Paketverfolgung.

Amazon's order page exposes the current shipment status quite reliably, while
its full tracking history is spread across different HTML/state layouts. This
module keeps timeline parsing isolated from login/order discovery and enriches
Amazon shipments with the same ``events`` shape used by the other providers.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .amazon_api import AmazonApiClient

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


def _append_event(events: list[dict[str, str]], date_value: Any, status_value: Any) -> None:
    status = _clean(status_value)
    moment = _event_datetime(date_value)
    if not status or moment is None:
        return
    if status.lower() in {"details", "weitere details", "alle aktualisierungen"}:
        return
    events.append({"datum": moment.isoformat(), "status": status})


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
            _append_event(events, date_time, status)

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
        unique[key] = event

    return sorted(unique.values(), key=lambda event: event["datum"], reverse=True)


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
        _LOGGER.debug(
            "Amazon: parsed %d tracking event(s) for %s",
            len(shipment["events"]),
            shipment.get("tracking_id"),
        )
        return shipment
