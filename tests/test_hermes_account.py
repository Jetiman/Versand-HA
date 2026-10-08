"""HA-free tests for the Hermes account mode (simulated myhermes.de server)."""
import asyncio
import base64
import hashlib
import hmac
import importlib
import sys
import types
from pathlib import Path

import pytest
from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "paketverfolgung"
PKG = "pv_under_test"


def _load():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[PKG] = pkg
    return importlib.import_module(f"{PKG}.hermes_account")


account = _load()
const = importlib.import_module(f"{PKG}.const")

USER, PASSWORD = "max@example.org", "geheim"
LIST = [
    {"shipmentId": "H100", "userDescription": "Meine Sendung", "sender": "OTTO",
     "trackingStatus": "Zugestellt", "lastModified": "2026-10-06T11:33:00.828",
     "addingType": "MANUAL", "externalId": "x"},
    {"shipmentId": "H200", "userDescription": "Schuhe", "sender": "Zalando",
     "trackingStatus": "Sendung in Zustellung", "lastModified": None},
    {"shipmentId": "H100", "trackingStatus": "dup"},
    {"nothing": 1},
]


def expected_hmac():
    d = hmac.new(USER.encode(), (USER + PASSWORD).encode(), hashlib.sha256).digest()
    return base64.b64encode(d).decode()


def make_app(state):
    async def login(request):
        body = await request.json()
        state["login_calls"] += 1
        ok = (
            body == {"username": USER, "password": PASSWORD}
            and request.headers.get("X-HMAC") == expected_hmac()
        )
        if not ok:
            return web.Response(status=state.get("bad_status", 401))
        resp = web.Response(status=200, text="")
        resp.set_cookie("SESSION", "s1")
        return resp

    async def current(request):
        if request.cookies.get("SESSION") != "s1":
            return web.Response(status=state.get("anon_status", 401))
        return web.json_response({"ok": True})

    async def shipments(request):
        state["list_calls"] += 1
        if request.cookies.get("SESSION") != "s1" or state.get("expire"):
            return web.Response(status=401)
        return web.json_response(state.get("payload", LIST))

    async def refresh(request):
        state["refresh_calls"] += 1
        if state.get("refresh_ok"):
            state["expire"] = False
            return web.Response(status=200)
        return web.Response(status=401)

    app = web.Application()
    app.router.add_post("/services/login/token/json", login)
    app.router.add_get("/services/login/user/current", current)
    app.router.add_get("/services/receivelist/shipments/", shipments)
    app.router.add_put("/services/login/token", refresh)
    return app


@pytest.fixture
def state():
    return {"login_calls": 0, "list_calls": 0, "refresh_calls": 0}


async def _client(state):
    server = TestServer(make_app(state))
    await server.start_server()
    account.HERMES_ACCOUNT_BASE = str(server.make_url("")).rstrip("/")
    session = ClientSession(cookie_jar=CookieJar(unsafe=True))  # test host is an IP
    return server, session, account.HermesAccountClient(session)


def run(coro):
    return asyncio.run(coro)


def test_hmac_matches_web_client_formula():
    assert account.compute_hmac(USER, PASSWORD) == expected_hmac()


def test_login_and_list(state):
    async def go():
        server, session, client = await _client(state)
        try:
            await client.login(USER, PASSWORD)
            items = await client.fetch_shipments()
        finally:
            await session.close(); await server.close()
        return items

    items = run(go())
    assert [i["id"] for i in items] == ["H100", "H200"]
    assert items[0]["group"] == const.GROUP_DELIVERED
    assert items[1]["group"] == const.GROUP_OUT_FOR_DELIVERY
    assert items[1]["description"] == "Schuhe" and items[1]["sender"] == "Zalando"


@pytest.mark.parametrize("bad", [400, 401, 403])
def test_wrong_password_is_auth_error(state, bad):
    state["bad_status"] = bad

    async def go():
        server, session, client = await _client(state)
        try:
            await client.login(USER, "falsch")
        finally:
            await session.close(); await server.close()

    with pytest.raises(account.HermesAuthError):
        run(go())


def test_login_200_without_session_is_auth_error(state):
    state["bad_status"] = 200  # server answers 200 but sets no cookie

    async def go():
        server, session, client = await _client(state)
        try:
            await client.login(USER, "falsch")
        finally:
            await session.close(); await server.close()

    with pytest.raises(account.HermesAuthError):
        run(go())


def test_expired_session_is_refreshed(state):
    state.update(refresh_ok=True)

    async def go():
        server, session, client = await _client(state)
        try:
            await client.login(USER, PASSWORD)
            state["expire"] = True
            return await client.fetch_shipments()
        finally:
            await session.close(); await server.close()

    assert len(run(go())) == 2
    assert state["refresh_calls"] == 1 and state["list_calls"] == 2


def test_unrenewable_session_raises_auth_error(state):
    async def go():
        server, session, client = await _client(state)
        try:
            await client.login(USER, PASSWORD)
            state["expire"] = True
            await client.fetch_shipments()
        finally:
            await session.close(); await server.close()

    with pytest.raises(account.HermesAuthError):
        run(go())


@pytest.mark.parametrize(
    "payload,count",
    [([], 0), ({}, 0), ("x", 0), ({"shipments": [{"shipmentId": "A"}]}, 1), (None, 0)],
)
def test_parse_is_defensive(payload, count):
    assert len(account.parse_shipments(payload)) == count


def test_status_groups():
    g = account.group_from_status
    assert g("Zugestellt") == const.GROUP_DELIVERED
    assert g("In Zustellung") == const.GROUP_OUT_FOR_DELIVERY
    assert g("Sendung angekündigt") == const.GROUP_REGISTERED
    assert g("Unterwegs") == const.GROUP_TRANSIT
    assert g(None) == const.GROUP_TRANSIT


def _coordinator_module():
    """Load hermes_coordinator with Home Assistant stubbed out."""
    def mod(name, **attrs):
        m = types.ModuleType(name); m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class Dummy:
        def __init__(self, *a, **k): pass

    for n in ("homeassistant", "homeassistant.config_entries", "homeassistant.core",
              "homeassistant.exceptions", "homeassistant.helpers",
              "homeassistant.helpers.aiohttp_client",
              "homeassistant.helpers.update_coordinator"):
        mod(n)
    sys.modules["homeassistant.config_entries"].ConfigEntry = Dummy
    sys.modules["homeassistant.core"].HomeAssistant = Dummy
    sys.modules["homeassistant.exceptions"].ConfigEntryAuthFailed = Exception
    h = sys.modules["homeassistant.helpers.aiohttp_client"]
    h.async_create_clientsession = h.async_get_clientsession = lambda *a, **k: None
    sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed = Exception
    mod(f"{PKG}.coordinator", _BaseCoordinator=Dummy)
    importlib.import_module(f"{PKG}.hermes_tracking_api")
    return importlib.import_module(f"{PKG}.hermes_coordinator")


def test_merge_shipment():
    hc = _coordinator_module()
    entry = account.parse_shipments(LIST)[1]
    # without public detail: built from the list entry
    item = hc.merge_shipment(entry, None)
    assert item["name"] == "Schuhe" and item["direction"] == "receive"
    assert item["group"] == const.GROUP_OUT_FOR_DELIVERY and not item["delivered"]
    # with detail: events/window are kept, a user label wins, direction forced
    detail = {"id": "H200", "carrier": "hermes", "name": "H200", "status": "x",
              "group": const.GROUP_TRANSIT, "direction": "send",
              "delivery_from": "2026-10-06T08:15:00Z", "delivery_to": "2026-10-06T10:15:00Z",
              "events": [{"datum": "d", "status": "s"}], "delivered": False}
    merged = hc.merge_shipment(entry, detail)
    assert merged["delivery_from"] == "2026-10-06T08:15:00Z" and merged["events"]
    assert merged["name"] == "Schuhe" and merged["direction"] == "receive"
    assert detail["direction"] == "send"  # input not mutated
    # default Hermes label falls back to the sender
    e0 = account.parse_shipments(LIST)[0]
    assert hc.merge_shipment(e0, None)["name"] == "OTTO"
    assert hc.merge_shipment(e0, None)["delivered"] is True


def test_placeholder_sender_is_not_used_as_name():
    hc = _coordinator_module()
    entry = account.parse_shipments(
        [{"shipmentId": "H300", "userDescription": "Meine Sendung",
          "sender": "Versender", "trackingStatus": "Zugestellt"}]
    )[0]
    assert hc.merge_shipment(entry, None)["name"] == "H300"
    detail = {"id": "H300", "carrier": "hermes", "name": "H300", "status": "x",
              "group": const.GROUP_DELIVERED, "events": [], "delivered": True}
    assert hc.merge_shipment(entry, detail)["name"] == "H300"
