"""HA-free tests for the Picnic account mode (simulated Picnic API server)."""
import asyncio
import hashlib
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "paketverfolgung"
PKG = "pv_picnic_under_test"


def _load():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[PKG] = pkg
    return importlib.import_module(f"{PKG}.picnic_account")


account = _load()
const = importlib.import_module(f"{PKG}.const")

BERLIN = ZoneInfo("Europe/Berlin")
USER, PASSWORD = "max@example.org", "geheim"
SECRET = hashlib.md5(PASSWORD.encode()).hexdigest()


def at(day, hh, mm=0):
    """Berlin wall-clock time on 2026-10-<day>."""
    return datetime(2026, 10, day, hh, mm, tzinfo=BERLIN)


def iso(dt):
    return dt.isoformat()


NOW = at(17, 9, 0)  # Saturday 17.10.2026 09:00

PLANNED = {  # slot in three days
    "delivery_id": "d-planned", "status": "CURRENT",
    "creation_time": iso(at(15, 20)),
    "slot": {"window_start": iso(at(20, 18)), "window_end": iso(at(20, 19)),
             "hub_id": "X", "slot_id": "s"},
    "orders": [{"creation_time": iso(at(15, 20)), "total_price": 4000,
                "checkout_total_price": 4199}],
}
TODAY = {
    "delivery_id": "d-today", "status": "CURRENT",
    "creation_time": iso(at(16, 20)),
    "slot": {"window_start": iso(at(17, 10)), "window_end": iso(at(17, 11)),
             "hub_id": "X", "slot_id": "s"},
    "orders": [],
}
DONE = {
    "delivery_id": "d-done", "status": "COMPLETED",
    "creation_time": iso(at(10, 20)),
    "slot": {"window_start": iso(at(12, 18)), "window_end": iso(at(12, 19))},
    "delivery_time": {"start": iso(at(12, 18, 21)), "end": iso(at(12, 18, 36))},
    "orders": [{"total_price": 3000, "checkout_total_price": 3050}],
}
CANCELLED = {"delivery_id": "d-x", "status": "CANCELLED", "slot": {}}
DETAIL = {
    "delivery_id": "d-planned",
    "orders": [{"items": [
        {"type": "ORDER_LINE", "items": [
            {"id": "s1", "decorators": [{"type": "QUANTITY", "quantity": 3}]}]},
        {"type": "ORDER_LINE", "items": [{"id": "s2", "decorators": []}]},
        {"type": "ORDER_LINE", "items": []},
    ]}],
}


def make_app(state):
    def authed(request):
        return request.headers.get("x-picnic-auth") == state["token"]

    async def login(request):
        body = await request.json()
        state["login_calls"] += 1
        if body.get("key") != USER or body.get("secret") != SECRET:
            return web.json_response(
                {"error": {"code": "AUTH_INVALID_CRED", "message": "no"}}, status=403
            )
        if body.get("client_id") != const.PICNIC_CLIENT_ID:
            return web.Response(status=400)
        if state.get("twofa") and not state.get("verified"):
            return web.json_response(
                {"second_factor_authentication_required": True},
                headers={"x-picnic-auth": "pre-2fa"},
            )
        return web.json_response({"user_id": "u1"}, headers={"x-picnic-auth": state["token"]})

    async def gen(request):
        state["gen_calls"] += 1
        assert (await request.json()) == {"channel": "SMS"}
        return web.Response(status=200, text="")

    async def verify(request):
        body = await request.json()
        if body.get("otp") != "123456":
            return web.json_response({"error": {"code": "TWO_FACTOR_FAIL"}}, status=400)
        state["verified"] = True
        return web.Response(status=200, text="", headers={"x-picnic-auth": state["token"]})

    async def summary(request):
        if not authed(request):
            return web.json_response({"error": {"code": "AUTH_ERROR"}}, status=401)
        state["summary_calls"] += 1
        assert (await request.json()) == []
        headers = {"x-picnic-auth": state["rotate"]} if state.get("rotate") else {}
        return web.json_response(state.get("summaries", []), headers=headers)

    async def detail(request):
        if not authed(request):
            return web.Response(status=401)
        did = request.match_info["id"]
        if did == "d-planned":
            headers = {}
            if state.get("bigtoken"):
                # Picnic's real token is a JWT of well over 8190 bytes.
                headers["x-picnic-auth"] = "x" * 20000
            return web.json_response(DETAIL, headers=headers)
        return web.Response(status=404)

    async def position(request):
        if not authed(request):
            return web.Response(status=401)
        return web.json_response(state.get("position", {}))

    app = web.Application()
    app.router.add_post("/user/login", login)
    app.router.add_post("/user/2fa/generate", gen)
    app.router.add_post("/user/2fa/verify", verify)
    app.router.add_post("/deliveries/summary", summary)
    app.router.add_get("/deliveries/{id}", detail)
    app.router.add_get("/deliveries/{id}/position", position)
    return app


def run(coro_factory, **state_over):
    state = {"token": "tok-1", "login_calls": 0, "gen_calls": 0, "summary_calls": 0}
    state.update(state_over)

    async def main():
        server = TestServer(make_app(state))
        await server.start_server()
        try:
            async with ClientSession(**const.PICNIC_SESSION_KWARGS) as session:
                client = account.PicnicAccountClient(
                    session, base_url=str(server.make_url("")).rstrip("/")
                )
                return await coro_factory(client, state)
        finally:
            await server.close()

    return asyncio.run(main())


# -- client --------------------------------------------------------------------


def test_md5_secret():
    assert account.md5_secret(PASSWORD) == SECRET


def test_login_stores_token_and_lists():
    async def go(client, state):
        await client.login(USER, PASSWORD)
        assert client.token == "tok-1"
        return await client.fetch_deliveries()

    out = run(go, summaries=[PLANNED, "junk", DONE])
    assert [d["delivery_id"] for d in out] == ["d-planned", "d-done"]


def test_wrong_password_is_auth_error():
    async def go(client, state):
        with pytest.raises(account.PicnicAuthError):
            await client.login(USER, "falsch")

    run(go)


def test_two_factor_flow():
    async def go(client, state):
        with pytest.raises(account.PicnicTwoFactorRequired):
            await client.login(USER, PASSWORD)
        await client.generate_2fa_code()
        assert state["gen_calls"] == 1
        with pytest.raises(account.PicnicTwoFactorError):
            await client.verify_2fa_code("000000")
        await client.verify_2fa_code("123456")
        assert client.token == "tok-1"
        return await client.fetch_deliveries()

    assert run(go, twofa=True, summaries=[DONE])[0]["delivery_id"] == "d-done"


def test_token_rotation_is_picked_up():
    async def go(client, state):
        await client.login(USER, PASSWORD)
        await client.fetch_deliveries()
        return client.token

    assert run(go, rotate="tok-2", summaries=[]) == "tok-2"


def test_expired_session_is_auth_error():
    async def go(client, state):
        state["token"] = "other"
        with pytest.raises(account.PicnicAuthError):
            await client.fetch_deliveries()

    run(go)


def test_detail_and_position_are_best_effort():
    async def go(client, state):
        await client.login(USER, PASSWORD)
        assert (await client.fetch_delivery("d-planned"))["delivery_id"] == "d-planned"
        assert await client.fetch_delivery("missing") is None
        return await client.fetch_position("d-planned")

    assert run(go, position={"eta": 1}) == {"eta": 1}


def test_oversized_token_header_is_readable():
    async def go(client, state):
        await client.login(USER, PASSWORD)
        detail = await client.fetch_delivery("d-planned")
        return detail, len(client.token)

    detail, size = run(go, bigtoken=True)
    assert detail["delivery_id"] == "d-planned"
    assert size == 20000


def test_detail_failure_does_not_break_the_poll():
    async def main():
        async with ClientSession() as session:  # default 8190-byte limit
            server = TestServer(make_app({"token": "tok-1", "login_calls": 0,
                                          "gen_calls": 0, "summary_calls": 0,
                                          "bigtoken": True}))
            await server.start_server()
            try:
                client = account.PicnicAccountClient(
                    session, base_url=str(server.make_url("")).rstrip("/"),
                    token="tok-1")
                assert await client.fetch_delivery("d-planned") is None
                assert await client.fetch_position("d-planned") == {}
            finally:
                await server.close()

    asyncio.run(main())


def test_unreachable_server_is_account_error():
    async def main():
        async with ClientSession() as session:
            client = account.PicnicAccountClient(session, base_url="http://127.0.0.1:1")
            with pytest.raises(account.PicnicAccountError):
                await client.fetch_deliveries()

    asyncio.run(main())


# -- parsing -------------------------------------------------------------------


def test_parse_ts_variants():
    base = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
    assert account.parse_ts("2026-10-17T09:00:00Z") == base
    assert account.parse_ts("2026-10-17T11:00:00+02:00") == base
    assert account.parse_ts("2026-10-17T09:00:00") == base  # naive = UTC
    ms = int(base.timestamp() * 1000)
    assert account.parse_ts(ms) == base
    assert account.parse_ts(str(ms)) == base
    assert account.parse_ts(int(base.timestamp())) == base
    for bad in (None, "", "nope", True):
        assert account.parse_ts(bad) is None


def test_article_count():
    assert account.article_count(DETAIL) == 5  # 3 + 1 + empty line counts 1
    assert account.article_count(None) is None
    assert account.article_count({"orders": []}) is None


def test_total_cents_prefers_checkout_price():
    assert account.total_cents(PLANNED) == 4199
    assert account.total_cents(TODAY) is None


# -- normalisation -------------------------------------------------------------


def norm(summary, detail=None, position=None, now=NOW):
    return account.normalize_delivery(summary, detail, position, now)


def test_planned_delivery():
    item = norm(PLANNED, DETAIL)
    assert item["carrier"] == "picnic"
    assert item["id"] == "d-planned"
    assert item["group"] == const.GROUP_REGISTERED
    assert item["status"] == "Lieferung geplant am Di. 20.10."
    assert item["name"] == "Einkauf · 5 Artikel"
    assert item["delivery_from"] == iso(at(20, 18).astimezone(timezone.utc))
    assert item["delivery_to"] == iso(at(20, 19).astimezone(timezone.utc))
    assert item["direction"] == "receive"
    assert item["delivered"] is False
    assert item["events"][-1]["status"] == "Bestellung aufgegeben (41,99 €)"
    assert item["events"][0]["status"].startswith("Lieferfenster gebucht: Di. 20.10. 18:00–19:00")


def test_status_texts_dont_trigger_dashboard_regexes():
    # The user's calendar/notification templates read these phrases as
    # "in Zustellung"; only the real out-for-delivery state may use them.
    import re

    bad = re.compile(
        r"zustellfahrzeug|in zustellung|wird zugestellt|heute zugestellt|zustellung heute",
        re.I,
    )
    for summary in (PLANNED, TODAY, DONE):
        item = norm(summary)
        assert not bad.search(item["status"]), item["status"]
    live = norm(TODAY, position={"eta": int(at(17, 10, 30).timestamp() * 1000)})
    assert bad.search(live["status"])


def test_delivery_today_not_yet_live():
    item = norm(TODAY)
    assert item["group"] == const.GROUP_TRANSIT
    assert item["status"] == "Lieferung heute geplant"
    assert item["name"] == "Einkauf"
    assert item["live"] is False


def test_eta2_window_replaces_slot_and_is_rounded():
    summary = dict(TODAY, eta2={"start": iso(at(17, 10, 12)), "end": iso(at(17, 10, 38))})
    item = norm(summary)
    assert item["delivery_from"] == iso(at(17, 10, 10).astimezone(timezone.utc))
    assert item["delivery_to"] == iso(at(17, 10, 40).astimezone(timezone.utc))
    assert item["group"] == const.GROUP_TRANSIT
    # the booked slot stays available for polling decisions
    assert item["slot_from"] == iso(at(17, 10).astimezone(timezone.utc))


def test_live_van_with_eta_window():
    pos = {"eta_window": {"start": int(at(17, 10, 21).timestamp() * 1000),
                          "end": int(at(17, 10, 41).timestamp() * 1000)}}
    item = norm(TODAY, position=pos)
    assert item["group"] == const.GROUP_OUT_FOR_DELIVERY
    assert item["status"] == "In Zustellung"
    assert item["live"] is True
    assert item["delivery_from"] == iso(at(17, 10, 20).astimezone(timezone.utc))
    assert item["delivery_to"] == iso(at(17, 10, 40).astimezone(timezone.utc))
    assert "ca. 10:20–10:40 Uhr" in item["events"][0]["status"]


def test_live_van_with_single_eta_gets_padded():
    item = norm(TODAY, position={"eta": int(at(17, 10, 30).timestamp() * 1000)})
    assert item["delivery_from"] == iso(at(17, 10, 25).astimezone(timezone.utc))
    assert item["delivery_to"] == iso(at(17, 10, 35).astimezone(timezone.utc))


def test_eta_wobble_keeps_window_and_status_stable():
    def at_min(m, s):
        return {"eta": int(at(17, 10, m).timestamp() * 1000) + s * 1000}

    a = norm(TODAY, position=at_min(31, 0))
    b = norm(TODAY, position=at_min(32, 10))
    assert (a["delivery_from"], a["delivery_to"], a["status"]) == (
        b["delivery_from"], b["delivery_to"], b["status"])


def _live(start_h, start_m, end_h, end_m, previous=None):
    pos = {"eta_window": {"start": int(at(17, start_h, start_m).timestamp() * 1000),
                          "end": int(at(17, end_h, end_m).timestamp() * 1000)}}
    return account.normalize_delivery(TODAY, None, pos, NOW, previous)


def test_eta_flipping_between_neighbouring_steps_keeps_the_shown_window():
    # Real morning of 22.10.2026: the ETA jumped 11:40 -> 11:45 -> 11:40 -> 11:35
    # -> 11:40 ... and every jump became a "window changed" push.
    shown = _live(10, 40, 11, 0)
    windows = set()
    for start_h, start_m, end_h, end_m in [
        (10, 45, 11, 5), (10, 40, 11, 0), (10, 35, 10, 55), (10, 40, 11, 0), (10, 45, 11, 5),
    ]:
        shown = _live(start_h, start_m, end_h, end_m, previous=shown)
        windows.add((shown["delivery_from"], shown["delivery_to"]))
    assert windows == {(iso(at(17, 10, 40).astimezone(timezone.utc)),
                        iso(at(17, 11, 0).astimezone(timezone.utc)))}
    # the timeline text names the window that is shown
    assert "ca. 10:40–11:00 Uhr" in shown["events"][0]["status"]


def test_eta_that_really_moves_changes_the_window():
    shown = _live(10, 40, 11, 0)
    later = _live(10, 50, 11, 10, previous=shown)  # +10 min: noticeable
    assert later["delivery_from"] == iso(at(17, 10, 50).astimezone(timezone.utc))
    assert later["delivery_to"] == iso(at(17, 11, 10).astimezone(timezone.utc))
    # a window that only changes at one end by 10+ minutes counts, too
    wider = _live(10, 40, 11, 15, previous=shown)
    assert wider["delivery_to"] == iso(at(17, 11, 15).astimezone(timezone.utc))


def test_slow_drift_is_measured_from_the_shown_window():
    # Four 5-minute steps in a row must not be swallowed one by one.
    shown = _live(10, 40, 11, 0)
    for m in (45, 50):
        shown = _live(10, m, 11, m - 40, previous=shown)
    assert shown["delivery_from"] == iso(at(17, 10, 50).astimezone(timezone.utc))


def test_hold_ignores_missing_or_delivered_previous():
    shown = _live(10, 40, 11, 0)
    assert _live(10, 45, 11, 5, previous=None)["delivery_from"] == iso(
        at(17, 10, 45).astimezone(timezone.utc))
    done_before = dict(shown, delivered=True)
    assert _live(10, 45, 11, 5, previous=done_before)["delivery_from"] == iso(
        at(17, 10, 45).astimezone(timezone.utc))
    # a delivered parcel shows the booked slot, whatever was shown before
    item = account.normalize_delivery(DONE, None, None, at(17, 9), shown)
    assert item["delivery_from"] != shown["delivery_from"]


def test_hold_window_helper():
    a, b = at(17, 10, 40), at(17, 11, 0)
    assert account.hold_window(a + timedelta(minutes=9), b, iso(a), iso(b)) == (a, b)
    assert account.hold_window(a + timedelta(minutes=10), b, iso(a), iso(b)) == (
        a + timedelta(minutes=10), b)
    assert account.hold_window(a, b, None, None) == (a, b)
    assert account.hold_window(a, b, "kaputt", iso(b)) == (a, b)


def test_delivered_delivery():
    item = norm(DONE, now=at(17, 9))
    assert item["group"] == const.GROUP_DELIVERED
    assert item["status"] == "Zugestellt"
    assert item["delivered"] is True
    assert item["live"] is False
    assert item["events"][0] == {
        "datum": iso(at(12, 18, 21).astimezone(timezone.utc)),
        "status": "Zugestellt",
    }
    # the booked slot, not a live ETA, is what stays on a delivered item
    assert item["delivery_from"] == iso(at(12, 18).astimezone(timezone.utc))


def test_delivery_time_alone_counts_as_delivered():
    summary = dict(TODAY, delivery_time={"start": iso(at(17, 10, 5)), "end": iso(at(17, 10, 20))})
    assert norm(summary)["delivered"] is True
    assert norm(summary, position={"eta": 1})["live"] is False


def test_overdue_open_delivery():
    item = norm(PLANNED, now=at(21, 9))
    assert item["status"] == "Lieferung ausstehend"
    assert item["group"] == const.GROUP_TRANSIT


def test_cancelled_and_idless_are_skipped():
    assert norm(CANCELLED) is None
    assert norm({"status": "CURRENT"}) is None


def test_slot_day_uses_local_time_not_utc():
    # 00:30 Berlin on the 18th is still the 17th in UTC.
    summary = dict(TODAY, slot={"window_start": iso(at(18, 0, 30)),
                                "window_end": iso(at(18, 1, 30))})
    assert norm(summary, now=at(17, 23, 50))["status"] == "Lieferung geplant am So. 18.10."


# -- history / polling ---------------------------------------------------------


def test_in_history():
    now = at(17, 9)
    assert account.in_history(PLANNED, now, 0)
    assert not account.in_history(CANCELLED, now, 90)
    assert not account.in_history(DONE, now, 0)  # delivered 5 days ago
    assert not account.in_history(DONE, now, 2)
    assert account.in_history(DONE, now, 6)


def test_choose_interval():
    base = timedelta(minutes=15)
    now = at(17, 9).astimezone(timezone.utc)

    def item(slot_start, slot_end, live=False, delivered=False):
        return {"slot_from": iso(slot_start), "slot_to": iso(slot_end),
                "live": live, "delivered": delivered}

    far = item(at(20, 18), at(20, 19))
    soon = item(at(17, 9, 40), at(17, 10, 40))
    live = item(at(17, 8), at(17, 9), live=True)
    done = item(at(17, 8), at(17, 9), delivered=True)
    assert account.choose_interval([far], now, base) == base
    assert account.choose_interval([done], now, base) == base
    assert account.choose_interval([far, soon], now, base) == const.PICNIC_SLOT_INTERVAL
    assert account.choose_interval([soon, live], now, base) == const.PICNIC_LIVE_INTERVAL
    assert account.choose_interval([live], now, timedelta(minutes=1)) == timedelta(minutes=1)
    # a base interval below the fast ones is never slowed down
    assert account.choose_interval([soon], now, timedelta(minutes=2)) == timedelta(minutes=2)


def test_wants_position_only_on_delivery_day():
    assert not account.wants_position(PLANNED, NOW)
    assert account.wants_position(TODAY, NOW)
    assert not account.wants_position(DONE, NOW)
    assert account.wants_position(dict(PLANNED, eta2={"start": iso(at(20, 18))}), NOW)


# Shape of a real answer (trimmed from a live account, 2026-10-16).
REAL_SUMMARY = {
    "delivery_id": "3sa42flzbq",
    "creation_time": "2026-10-14T23:56:40.601+02:00",
    "slot": {"slot_id": "x", "hub_id": "MGL", "fc_id": "FC3",
             "window_start": "2026-10-16T14:30:00.000+02:00",
             "window_end": "2026-10-16T16:30:00.000+02:00",
             "cut_off_time": "2026-10-15T23:00:00.000+02:00",
             "is_available": False, "selected": False, "reserved": False,
             "unavailability_reason": "CLOSED", "slot_characteristics": []},
    "eta2": {"start": "2026-10-16T15:27:00.000+02:00", "end": "2026-10-16T15:47:00.000+02:00"},
    "status": "COMPLETED",
    "delivery_time": {"start": "2026-10-16T15:27:09.798+02:00",
                      "end": "2026-10-16T15:31:20.882+02:00"},
    "orders": [{"type": "ORDER", "id": "602-002-1183", "creation_time":
                "2026-10-14T23:57:20.503+02:00", "total_price": 4793,
                "status": "COMPLETED", "cancellation_time": None}],
}
REAL_DETAIL = dict(REAL_SUMMARY, orders=[{
    "type": "ORDER", "id": "602-002-1183", "total_price": 4793,
    "checkout_total_price": 4882,
    "items": [{"type": "ORDER_LINE", "id": "47_59", "items": [
        {"type": "ORDER_ARTICLE", "id": "s1",
         "decorators": [{"type": "IMMUTABLE"}, {"type": "QUANTITY", "quantity": 2}]}]},
        {"type": "ORDER_LINE", "id": "49", "items": [
            {"type": "ORDER_ARTICLE", "id": "s2",
             "decorators": [{"type": "QUANTITY", "quantity": 1}]}]}],
}])


def test_real_shaped_delivered_delivery():
    item = norm(REAL_SUMMARY, REAL_DETAIL, now=at(17, 9))
    assert item["group"] == const.GROUP_DELIVERED and item["delivered"] is True
    assert item["name"] == "Einkauf · 3 Artikel"
    assert item["delivery_from"] == iso(at(16, 14, 30).astimezone(timezone.utc))
    assert item["delivery_to"] == iso(at(16, 16, 30).astimezone(timezone.utc))
    assert item["events"][0] == {
        "datum": account.parse_ts("2026-10-16T15:27:09.798+02:00").isoformat(),
        "status": "Zugestellt"}
    # charged amount (with deposit) comes from the detail, not the summary
    assert item["events"][-1]["status"] == "Bestellung aufgegeben (48,82 €)"
    assert norm(REAL_SUMMARY, None, now=at(17, 9))["events"][-1]["status"] == (
        "Bestellung aufgegeben (47,93 €)")
