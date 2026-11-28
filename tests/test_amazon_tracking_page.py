"""HA-free tests for what is read from an Amazon tracking page: the places in
the history and the delivery van's position (map request and answer)."""
import importlib
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "paketverfolgung"
PKG = "pv_amazon_tracking_under_test"

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(PKG_DIR)]
sys.modules[PKG] = pkg
tracking = importlib.import_module(f"{PKG}.amazon_tracking")

CODE = "DE6123456789"


def _page(rows, state=None):
    body = "".join(
        f"""<div class="row"><span class="tracking-event-time">{time}</span>
        <span class="tracking-event-message">{msg}</span>
        {f'<span class="tracking-event-location">{loc}</span>' if loc else ""}</div>"""
        for time, msg, loc in rows
    )
    script = (
        f"<script type='a-state' data-a-state='{{\"key\":\"page-state\"}}'>{json.dumps(state)}</script>"
        if state is not None
        else ""
    )
    return f"""<html><body>{script}
    <div id="tracking-events-container"><div class="group">
      <div class="tracking-event-date-header"><span class="tracking-event-date">24.11.2026</span></div>
      {body}
    </div></div></body></html>"""


def test_history_events_carry_their_place():
    html = _page(
        [
            ("16:39", "Sendung ist beim Verteilzentrum eingegangen.", "Erftstadt, DE"),
            ("12:00", "Paket hat die Einrichtung verlassen", ""),
            ("11:00", "Unterwegs", "—"),
        ]
    )
    events = tracking.parse_amazon_events(html)
    by_status = {e["status"]: e for e in events}
    assert by_status["Sendung ist beim Verteilzentrum eingegangen."]["location"] == "Erftstadt, DE"
    assert "location" not in by_status["Paket hat die Einrichtung verlassen"]
    assert "location" not in by_status["Unterwegs"]  # placeholder, no place


def test_place_survives_a_duplicate_without_one():
    state = {
        "mapTracking": {},
        "events": [
            {"eventDate": "2026-11-24T16:39:00+01:00", "eventMessage": "Zugestellt"}
        ],
    }
    html = _page([("16:39", "Zugestellt", "Ahlhorn, DE")], state)
    events = tracking.parse_amazon_events(html)
    assert [e.get("location") for e in events if e["status"] == "Zugestellt"] == [
        "Ahlhorn, DE"
    ]


@pytest.mark.parametrize(
    ("shipment", "wanted"),
    [
        ({"stops_away": 3, "tracking_id": CODE, "carrier": "Amazon"}, True),
        ({"stops_away": 0, "tracking_id": CODE, "carrier": None}, True),
        ({"stops_away": 11, "tracking_id": CODE, "carrier": "Amazon"}, False),
        ({"stops_away": None, "tracking_id": CODE, "carrier": "Amazon"}, False),
        ({"stops_away": 2, "tracking_id": "01425118215590", "carrier": "DPD"}, False),
        ({"stops_away": 2, "tracking_id": "x y", "carrier": "Amazon"}, False),
    ],
)
def test_van_position_only_for_amazon_vans_on_the_last_stretch(shipment, wanted):
    assert tracking.vehicle_wanted(shipment) is wanted


def test_map_form_from_the_page_state():
    html = _page([], {"mapTracking": {"x": 1}, "trackingId": CODE, "csrfToken": "token-123456"})
    assert tracking.map_request_form(html, CODE) == {
        "trackingId": CODE,
        "csrfToken": "token-123456",
    }
    # another parcel's page, or a page without the map: no request
    assert tracking.map_request_form(html, "DE0000000000") is None
    assert tracking.map_request_form(_page([], {"trackingId": CODE}), CODE) is None


NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)


def _answer(state="YOU_ARE_NEXT", seconds_ago=30, code=CODE, **geo):
    fix = {"latitude": 52.9, "longitude": 8.2, "accuracy": 12,
           "locationTime": NOW.timestamp() - seconds_ago}
    fix.update(geo)
    return {
        "success": True,
        "packageLocationDetails": {
            "trackingObjectState": state,
            "trackingObjectId": code,
            "transporterDetails": {"geoLocation": fix},
        },
    }


def test_van_position_is_read():
    pos = tracking.parse_vehicle_position(_answer(), CODE, NOW)
    assert pos["latitude"] == 52.9 and pos["longitude"] == 8.2
    assert pos["accuracy"] == 12
    assert pos["position_at"] == "2026-11-28T11:59:30+00:00"
    assert pos["fetched_at"] == NOW.isoformat()


def test_van_position_in_milliseconds():
    answer = _answer(locationTime=(NOW.timestamp() - 60) * 1000)
    pos = tracking.parse_vehicle_position(answer, CODE, NOW)
    assert pos["position_at"] == "2026-11-28T11:59:00+00:00"


@pytest.mark.parametrize(
    "answer",
    [
        _answer(seconds_ago=900),  # stale
        _answer(state="DELIVERED"),
        _answer(code="DE0000000000"),  # another parcel
        _answer(latitude=0, longitude=0),
        _answer(latitude="52.9"),
        {"success": False},
        None,
    ],
)
def test_no_van_position(answer):
    assert tracking.parse_vehicle_position(answer, CODE, NOW) is None
