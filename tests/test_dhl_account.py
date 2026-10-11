"""HA-free tests for the DHL account mode (simulated DHL login + search)."""
import asyncio
import base64
import importlib
import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "paketverfolgung"
PKG = "pv_dhl_under_test"


# --- Home Assistant stand-ins (just enough to import coordinator.py) --------
class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


class _Finder:
    def find_spec(self, name, path=None, target=None):
        if name == "homeassistant" or name.startswith("homeassistant."):
            import importlib.machinery

            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):
        module = _StubModule(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):
        pass


class _Coordinator:
    def __init__(self, hass=None, logger=None, name=None, update_interval=None, **kw):
        self.hass = hass
        self.update_interval = update_interval
        self.data = None

    def __class_getitem__(cls, item):
        return cls


class _AuthFailed(Exception):
    pass


class _UpdateFailed(Exception):
    pass


def _load():
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.append(_Finder())
    uc = importlib.import_module("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = _Coordinator
    uc.UpdateFailed = _UpdateFailed
    importlib.import_module("homeassistant.exceptions").ConfigEntryAuthFailed = _AuthFailed
    importlib.import_module("homeassistant.core").callback = lambda f: f
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[PKG] = pkg
    return (
        importlib.import_module(f"{PKG}.dhl_account"),
        importlib.import_module(f"{PKG}.coordinator"),
        importlib.import_module(f"{PKG}.const"),
    )


account, coordinator, const = _load()


def run(coro):
    return asyncio.run(coro)


def make_token(exp_offset=3600, email="max@example.org"):
    def b64(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    claims = {"exp": time.time() + exp_offset}
    if email:
        claims["email"] = email
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


# --- the pasted dhllogin:// address ---------------------------------------
@pytest.mark.parametrize(
    "pasted",
    [
        "dhllogin://de.deutschepost.dhl/login?code=abc123&state=x",
        "  dhllogin://de.deutschepost.dhl/login?state=x&code=abc123  ",
        # the whole line the browser console prints
        "Failed to launch 'dhllogin://de.deutschepost.dhl/login?code=abc123&state=x' "
        "because the URL scheme is not registered.",
        # copied from rendered HTML
        "dhllogin://de.deutschepost.dhl/login?code=abc123&amp;state=x",
        'Uncaught: "dhllogin://de.deutschepost.dhl/login?code=abc123".',
        "DHLLOGIN://de.deutschepost.dhl/login?code=abc123",
    ],
)
def test_extract_code_accepts_console_output(pasted):
    assert account.extract_code(pasted) == "abc123"


@pytest.mark.parametrize(
    "pasted", ["", "https://www.dhl.de/login?code=abc", "dhllogin://x/login?state=1"]
)
def test_extract_code_rejects_garbage(pasted):
    with pytest.raises(account.DhlAuthError):
        account.extract_code(pasted)


def test_account_label():
    assert account.account_label(make_token(email="max@example.org")) == "max@example.org"
    assert account.account_label(make_token(email=None)) is None
    assert account.account_label("garbage") is None
    assert account.account_label(None) is None


def test_connection_error_is_an_auth_error_for_old_callers():
    assert issubclass(account.DhlConnectionError, account.DhlAuthError)


# --- client against a simulated server -----------------------------------
SEARCH = {
    "sendungen": [
        {"id": "00340434111111111111", "sendungsinfo": {"sendungsliste": "AKTIV"}},
        {"id": "00340434222222222222", "sendungsinfo": {}},
        {"id": "00340434333333333333", "sendungsinfo": {"sendungsliste": "ARCHIVIERT"}},
        {"id": "00340434111111111111"},
        {
            "id": "00340434444444444444",
            "sendungsinfo": {"sendungsliste": "AKTUELL"},
            "sendungsdetails": {"ruecksendung": True},
        },
        {
            "id": "00340434555555555555",
            "sendungsinfo": {"sendungsliste": "AKTUELL"},
            "sendungsdetails": {"retoure": True},
        },
        {"nothing": True},
    ]
}


def make_app(state):
    async def token(request):
        form = await request.post()
        state["token_calls"].append(dict(form))
        status = state.get("token_status", 200)
        if status != 200:
            return web.json_response({"error": "invalid_grant"}, status=status)
        return web.json_response(
            {"id_token": make_token(), "refresh_token": "r2", "token_type": "Bearer"}
        )

    async def search(request):
        state["cookies"].append(request.headers.get("cookie"))
        status = state.get("search_status", 200)
        if status != 200:
            return web.Response(status=status)
        return web.json_response(SEARCH)

    app = web.Application()
    app.router.add_post("/auth/token", token)
    app.router.add_get("/search", search)
    return app


@pytest.fixture
def state():
    return {"token_calls": [], "cookies": []}


async def _client(state):
    server = TestServer(make_app(state))
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    account.DHL_AUTH_BASE = f"{base}/auth"
    account.SEARCH_URL = f"{base}/search"
    session = ClientSession()
    return server, session, account.DhlAccountClient(session)


def test_exchange_and_list(state):
    async def go():
        server, session, client = await _client(state)
        try:
            tokens = await client.exchange_code("abc123", "verifier")
            ids = await client.fetch_shipment_ids(tokens)
        finally:
            await session.close()
            await server.close()
        return tokens, ids

    tokens, ids = run(go())
    assert state["token_calls"][0]["grant_type"] == "authorization_code"
    assert state["token_calls"][0]["code"] == "abc123"
    assert state["token_calls"][0]["code_verifier"] == "verifier"
    # archived shipments, duplicates and entries without id are skipped
    assert ids == ["00340434111111111111", "00340434222222222222"]
    assert state["cookies"][0] == f"dhli={tokens['id_token']}"


@pytest.mark.parametrize("status", [400, 401, 403])
def test_rejected_code_is_auth_error(state, status):
    state["token_status"] = status

    async def go():
        server, session, client = await _client(state)
        try:
            await client.exchange_code("bad", "verifier")
        finally:
            await session.close()
            await server.close()

    with pytest.raises(account.DhlAuthError) as err:
        run(go())
    assert not isinstance(err.value, account.DhlConnectionError)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_server_trouble_is_connection_error(state, status):
    state["token_status"] = status

    async def go():
        server, session, client = await _client(state)
        try:
            await client.exchange_code("abc", "verifier")
        finally:
            await session.close()
            await server.close()

    with pytest.raises(account.DhlConnectionError):
        run(go())


def test_search_errors_are_classified(state):
    tokens = {"id_token": make_token(), "refresh_token": "r"}

    async def go(status):
        state["search_status"] = status
        server, session, client = await _client(state)
        try:
            await client.fetch_shipment_ids(tokens)
        finally:
            await session.close()
            await server.close()

    for status in (401, 403):
        with pytest.raises(account.DhlAuthError) as err:
            run(go(status))
        assert not isinstance(err.value, account.DhlConnectionError)
    with pytest.raises(account.DhlConnectionError):
        run(go(500))


def test_unreachable_server_is_connection_error():
    async def go():
        account.DHL_AUTH_BASE = "http://127.0.0.1:9/auth"  # nothing listens here
        session = ClientSession()
        try:
            await account.DhlAccountClient(session).exchange_code("abc", "v")
        finally:
            await session.close()

    with pytest.raises(account.DhlConnectionError):
        run(go())


def test_expiring_token_is_refreshed(state):
    old = {"id_token": make_token(exp_offset=30), "refresh_token": "r1"}
    fresh = {"id_token": make_token(exp_offset=3600), "refresh_token": "r1"}

    async def go():
        server, session, client = await _client(state)
        try:
            return await client.ensure_fresh(old), await client.ensure_fresh(fresh)
        finally:
            await session.close()
            await server.close()

    refreshed, untouched = run(go())
    assert [c["grant_type"] for c in state["token_calls"]] == ["refresh_token"]
    assert refreshed["id_token"] != old["id_token"]
    assert untouched is fresh


# --- the dedicated coordinator ---------------------------------------------
class FakeAccount:
    def __init__(self, ids=None, error=None, fresh=None):
        self.ids, self.error, self.fresh = ids or [], error, fresh

    async def ensure_fresh(self, session):
        return self.fresh or session

    async def fetch_shipment_ids(self, session):
        if self.error:
            raise self.error
        return self.ids


def make_coordinator(client, session=None, data=None):
    coord = coordinator.DhlAccountDataUpdateCoordinator.__new__(
        coordinator.DhlAccountDataUpdateCoordinator
    )
    coord.entry = types.SimpleNamespace(
        data={"dhl_session": session} if session is not None else {},
        options={},
    )
    updates = []
    coord.hass = types.SimpleNamespace(
        config_entries=types.SimpleNamespace(
            async_update_entry=lambda entry, data=None, **kw: updates.append(data)
        )
    )
    coord.dhl_account = client
    coord.carriers = {}
    coord.data = data
    coord.dhl_account_status = None
    coord.updates = updates
    return coord


SESSION = {"id_token": "t", "refresh_token": "r"}


def test_config_is_account_driven():
    coord = make_coordinator(FakeAccount(), SESSION)
    coord.entry.data["tracking_numbers"] = ["should-be-ignored"]
    assert coord._config(const.CONF_TRACKING_NUMBERS, ["x"]) == []
    assert coord._config(const.CONF_DHL_AUTO_DISCOVERY) is True
    assert coord._config(const.CONF_CARRIER_OVERRIDES, {"a": "dpd"}) == {}
    coord.entry.options["names"] = {"a": "b"}
    assert coord._config(const.CONF_NAMES) == {"a": "b"}


def test_account_ids_become_dhl_shipments():
    coord = make_coordinator(FakeAccount(ids=["A", "B"]), SESSION)
    assert run(coord._merge_dhl_account_numbers([])) == ["A", "B"]
    assert coord.carriers == {"A": const.CARRIER_DHL, "B": const.CARRIER_DHL}
    assert coord.dhl_account_status == "2 Sendung(en) erkannt"
    assert coord.updates == []  # nothing refreshed - nothing persisted


def test_refreshed_session_is_persisted():
    fresh = {"id_token": "new", "refresh_token": "r"}
    coord = make_coordinator(FakeAccount(ids=["A"], fresh=fresh), SESSION)
    run(coord._merge_dhl_account_numbers([]))
    assert coord.updates == [{"dhl_session": fresh}]


def test_rejected_session_asks_for_reauth():
    coord = make_coordinator(
        FakeAccount(error=account.DhlAuthError("abgelaufen")), SESSION
    )
    with pytest.raises(_AuthFailed):
        run(coord._merge_dhl_account_numbers([]))
    assert coord.dhl_account_status == "abgelaufen"


def test_missing_session_asks_for_reauth():
    coord = make_coordinator(FakeAccount(), None)
    with pytest.raises(_AuthFailed):
        run(coord._merge_dhl_account_numbers([]))


def test_dhl_hiccup_keeps_known_shipments():
    err = account.DhlConnectionError("timeout")
    known = {"A": {"id": "A"}, "B": {"id": "B"}}
    coord = make_coordinator(FakeAccount(error=err), SESSION, data=known)
    assert run(coord._merge_dhl_account_numbers([])) == ["A", "B"]
    assert coord.dhl_account_status == "timeout"


def test_dhl_hiccup_on_first_poll_is_retried_later():
    coord = make_coordinator(
        FakeAccount(error=account.DhlConnectionError("timeout")), SESSION, data=None
    )
    with pytest.raises(_UpdateFailed):
        run(coord._merge_dhl_account_numbers([]))


# --- returns ("Retouren") are ignored ---------------------------------------
tracking_util = importlib.import_module(f"{PKG}.tracking_util")


@pytest.mark.parametrize(
    "item, expected",
    [
        ({"direction": "return", "status": "Unterwegs"}, True),  # DPD account
        ({"direction": "RETURN", "status": ""}, True),
        ({"direction": "receive", "status": "Rücksendung an den Absender"}, True),
        ({"status": "Retoure wurde abgeholt"}, True),
        ({"status": "Ruecksendung eingeleitet"}, True),
        ({"direction": "receive", "status": "In Zustellung"}, False),
        ({"direction": "send", "status": "Zugestellt"}, False),
        ({"status": None, "direction": None}, False),
        (None, False),
    ],
)
def test_is_return(item, expected):
    assert tracking_util.is_return(item) is expected


def test_returns_do_not_trigger_notifications():
    coord = make_coordinator(FakeAccount(), SESSION)
    coord.entry.options = {"notify_enabled": True}
    coord._notify_primed = True
    coord._notify_targets = lambda: ["mobile_app_x"]
    pushed = []
    coord._push_notification = lambda targets, action, item, prev: pushed.append(item["id"])
    coord.data = {}
    coord._notify_changes(
        {
            "R1": {"id": "R1", "status": "Rücksendung unterwegs", "group": "transit"},
            "R2": {"id": "R2", "status": "Unterwegs", "group": "transit", "direction": "return"},
            "P1": {"id": "P1", "status": "Unterwegs", "group": "transit", "direction": "receive"},
        }
    )
    assert pushed == ["P1"]
