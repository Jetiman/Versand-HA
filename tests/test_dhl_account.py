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


class _Plain:
    """Stand-in base class for HA entity classes (sensor.py subclasses them)."""

    def __init__(self, *args, **kwargs):
        pass

    async def async_remove(self):
        pass

    def __class_getitem__(cls, item):
        return cls


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
    uc.CoordinatorEntity = type("CoordinatorEntity", (_Plain,), {})
    importlib.import_module("homeassistant.components.sensor").SensorEntity = type(
        "SensorEntity", (_Plain,), {}
    )
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[PKG] = pkg
    return (
        importlib.import_module(f"{PKG}.dhl_account"),
        importlib.import_module(f"{PKG}.coordinator"),
        importlib.import_module(f"{PKG}.const"),
        importlib.import_module(f"{PKG}.sensor"),
    )


account, coordinator, const, sensor = _load()


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
        {
            "id": "00340434111111111111",
            "sendungsinfo": {"sendungsliste": "AKTIV", "sendungsname": "  Beispiel Shop GmbH "},
        },
        {"id": "00340434222222222222", "sendungsinfo": {}},
        {
            "id": "00340434333333333333",
            "sendungsinfo": {"sendungsliste": "ARCHIVIERT", "sendungsname": "Archiv Versand"},
        },
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


def test_account_names_come_from_the_list(state):
    """The app's shipment name (``sendungsname``) is only in the account list."""
    tokens = {"id_token": make_token(), "refresh_token": "r"}

    async def go():
        server, session, client = await _client(state)
        try:
            return await client.fetch_account(tokens)
        finally:
            await session.close()
            await server.close()

    ids, names = run(go())
    assert ids == ["00340434111111111111", "00340434222222222222"]
    # trimmed; archived shipments keep their name; no name -> no entry
    assert names == {
        "00340434111111111111": "Beispiel Shop GmbH",
        "00340434333333333333": "Archiv Versand",
    }


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
    def __init__(self, ids=None, error=None, fresh=None, names=None):
        self.ids, self.error, self.fresh = ids or [], error, fresh
        self.names = names or {}

    async def ensure_fresh(self, session):
        return self.fresh or session

    async def fetch_account(self, session):
        if self.error:
            raise self.error
        return self.ids, self.names


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
    coord.dhl_account_names = {}
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


def test_account_names_are_kept_for_the_poll():
    names = {"A": "Beispiel Shop GmbH"}
    coord = make_coordinator(FakeAccount(ids=["A", "B"], names=names), SESSION)
    run(coord._merge_dhl_account_numbers([]))
    assert coord.dhl_account_names == names


def test_dhl_hiccup_keeps_the_known_names():
    err = account.DhlConnectionError("timeout")
    coord = make_coordinator(
        FakeAccount(error=err), SESSION, data={"A": {"id": "A"}}
    )
    coord.dhl_account_names = {"A": "Beispiel Shop GmbH"}
    run(coord._merge_dhl_account_numbers([]))
    assert coord.dhl_account_names == {"A": "Beispiel Shop GmbH"}


def test_app_name_replaces_the_number_placeholder():
    coord = make_coordinator(FakeAccount(), SESSION)
    coord.dhl_account_names = {"A": "Beispiel Shop GmbH"}
    raw = {"id": "A", "sendungsinfo": {}, "sendungsdetails": {}}
    item = coordinator.normalize_dhl_shipment(raw)
    assert item["name"] == "DHL A"  # the anonymous lookup has no name
    item["carrier"] = const.CARRIER_DHL
    coord._apply_dhl_account_name("A", item)
    assert item["carrier_name"] == "Beispiel Shop GmbH"
    # an already archived item with the old placeholder is corrected, too
    old = {"id": "A", "carrier": const.CARRIER_DHL, "name": "DHL A", "carrier_name": "DHL A"}
    coord._apply_dhl_account_name("A", old)
    assert old["carrier_name"] == "Beispiel Shop GmbH"


def test_parcel_ready_in_packstation_counts_as_delivered():
    text = "Die Sendung liegt in der Packstation zur Abholung bereit."
    raw = {
        "id": "A",
        "sendungsinfo": {},
        "sendungsdetails": {
            "sendungsverlauf": {"fortschritt": 4, "kurzStatus": text, "events": []}
        },
    }
    item = coordinator.normalize_dhl_shipment(raw)
    assert item["delivered"] is True
    assert item["group"] == const.GROUP_DELIVERED


def test_app_name_only_for_dhl_shipments_the_account_knows():
    coord = make_coordinator(FakeAccount(), SESSION)
    coord.dhl_account_names = {"A": "Beispiel Shop GmbH"}
    other = {"id": "A", "carrier": const.CARRIER_DPD, "name": "DPD A"}
    coord._apply_dhl_account_name("A", other)
    assert "carrier_name" not in other  # another carrier's parcel is untouched
    unknown = {"id": "Z", "carrier": const.CARRIER_DHL, "name": "DHL Z"}
    coord._apply_dhl_account_name("Z", unknown)
    assert "carrier_name" not in unknown


# --- shipments DHL no longer lists lose their registry entry ----------------
class FakeRegistry:
    def __init__(self, entities):
        self.entities = entities  # list of SimpleNamespace(entity_id, unique_id, domain)
        self.removed = []

    def async_get_entity_id(self, domain, platform, unique_id):
        for e in self.entities:
            if e.unique_id == unique_id:
                return e.entity_id
        return None

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


def _entity(entry_id, item_id, domain="sensor"):
    return types.SimpleNamespace(
        entity_id=f"sensor.dhl_{item_id}",
        unique_id=f"{entry_id}_{item_id}",
        domain=domain,
    )


def _registry_for(monkeypatch, entities):
    registry = FakeRegistry(entities)
    fake_er = types.SimpleNamespace(
        async_get=lambda hass: registry,
        async_entries_for_config_entry=lambda reg, eid: list(reg.entities),
    )
    monkeypatch.setattr(sensor, "er", fake_er)
    return registry


def test_stale_dhl_entities_are_removed_at_setup(monkeypatch):
    entry = types.SimpleNamespace(entry_id="E1")
    registry = _registry_for(
        monkeypatch,
        [
            _entity("E1", "ALIVE"),
            _entity("E1", "GONE"),
            types.SimpleNamespace(
                entity_id="sensor.dhl_konto_erkennung",
                unique_id="E1_dhl_account_status",
                domain="sensor",
            ),
        ],
    )
    coord = make_coordinator(FakeAccount(), SESSION, data={"ALIVE": {"id": "ALIVE"}})
    coord.last_update_success = True
    sensor._drop_stale_shipment_entities(coord.hass, entry, coord)
    # the vanished shipment goes, the live one and the diagnostic sensor stay
    assert registry.removed == ["sensor.dhl_GONE"]


def test_stale_cleanup_waits_for_a_good_poll(monkeypatch):
    entry = types.SimpleNamespace(entry_id="E1")
    registry = _registry_for(monkeypatch, [_entity("E1", "GONE")])
    coord = make_coordinator(FakeAccount(), SESSION, data={})
    coord.last_update_success = False  # DHL hiccup: nothing is deleted
    sensor._drop_stale_shipment_entities(coord.hass, entry, coord)
    coord.last_update_success = True
    coord.data = None
    sensor._drop_stale_shipment_entities(coord.hass, entry, coord)
    assert registry.removed == []


def test_other_providers_keep_their_registry_entries(monkeypatch):
    entry = types.SimpleNamespace(entry_id="E1")
    registry = _registry_for(monkeypatch, [_entity("E1", "GONE")])
    other = types.SimpleNamespace(
        hass=None, last_update_success=True, data={}, entry=entry
    )
    sensor._drop_stale_shipment_entities(None, entry, other)
    assert registry.removed == []
    assert sensor._forget_stale_entity(other, entry, "GONE") is False


def test_live_removal_forgets_the_registry_entry(monkeypatch):
    entry = types.SimpleNamespace(entry_id="E1")
    registry = _registry_for(monkeypatch, [_entity("E1", "GONE")])
    coord = make_coordinator(FakeAccount(), SESSION, data={})
    coord.last_update_success = True
    assert sensor._forget_stale_entity(coord, entry, "GONE") is True
    assert registry.removed == ["sensor.dhl_GONE"]
    assert sensor._forget_stale_entity(coord, entry, "UNKNOWN") is False


# --- other providers: registry entries go after a grace period ---------------
from datetime import datetime, timedelta, timezone


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)

    def utcnow(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def _grace_setup(monkeypatch, registry_ids, data):
    """Run ``_setup_dynamic_entities`` for a non-DHL coordinator.

    Returns (coord, registry, clock, sync) - ``sync()`` is the coordinator
    update listener the platform registered.
    """
    entry = types.SimpleNamespace(
        entry_id="E1", async_on_unload=lambda cb: None
    )
    registry = _registry_for(monkeypatch, [_entity("E1", i) for i in registry_ids])
    clock = _Clock()
    monkeypatch.setattr(sensor, "dt_util", types.SimpleNamespace(utcnow=clock.utcnow))
    coord = coordinator.TrackingNumbersDataUpdateCoordinator.__new__(
        coordinator.TrackingNumbersDataUpdateCoordinator
    )
    coord.hass = types.SimpleNamespace(
        async_create_task=lambda coro: coro.close()
    )
    coord.data = data
    coord.last_update_success = True
    listeners = []
    coord.async_add_listener = lambda cb: listeners.append(cb)
    sensor._setup_dynamic_entities(entry, coord, lambda entities: None)
    return coord, registry, clock, listeners[0]


def test_leftovers_from_before_a_restart_go_after_the_grace_period(monkeypatch):
    coord, registry, clock, sync = _grace_setup(
        monkeypatch, ["ALIVE", "OLD"], {"ALIVE": {"id": "ALIVE"}}
    )
    clock.advance(hours=1)
    sync()
    assert registry.removed == []  # not yet: the source may just be hiccuping
    clock.advance(hours=2, minutes=1)
    sync()
    assert registry.removed == ["sensor.dhl_OLD"]
    sync()
    assert registry.removed == ["sensor.dhl_OLD"]  # only once


def test_shipment_that_vanishes_is_forgotten_after_the_grace_period(monkeypatch):
    coord, registry, clock, sync = _grace_setup(
        monkeypatch, ["A", "B"], {"A": {"id": "A"}, "B": {"id": "B"}}
    )
    coord.data = {"B": {"id": "B"}}
    sync()
    assert registry.removed == []  # the entity is gone, its registry entry stays
    clock.advance(hours=3)
    sync()
    assert registry.removed == ["sensor.dhl_A"]


def test_shipment_that_comes_back_is_kept(monkeypatch):
    coord, registry, clock, sync = _grace_setup(
        monkeypatch, ["A"], {"A": {"id": "A"}}
    )
    coord.data = {}
    sync()
    clock.advance(hours=2)
    coord.data = {"A": {"id": "A"}}  # incomplete answer was a blip
    sync()
    clock.advance(hours=5)
    sync()
    assert registry.removed == []


def test_nothing_is_forgotten_while_the_provider_fails(monkeypatch):
    coord, registry, clock, sync = _grace_setup(
        monkeypatch, ["OLD"], {"X": {"id": "X"}}
    )
    coord.last_update_success = False
    clock.advance(hours=10)
    sync()
    assert registry.removed == []
    coord.last_update_success = True
    sync()
    assert registry.removed == ["sensor.dhl_OLD"]


def test_summary_and_status_sensors_are_not_shipments(monkeypatch):
    entry = types.SimpleNamespace(entry_id="E1")
    for suffix in ("dhl_account_status", "out_for_delivery_today", "next_refresh"):
        e = types.SimpleNamespace(
            entity_id="sensor.x", unique_id=f"E1_{suffix}", domain="sensor"
        )
        assert sensor._shipment_item_id("E1", e) is None
    other = types.SimpleNamespace(entity_id="b", unique_id="E1_A", domain="binary_sensor")
    assert sensor._shipment_item_id("E1", other) is None
    foreign = types.SimpleNamespace(entity_id="s", unique_id="E2_A", domain="sensor")
    assert sensor._shipment_item_id("E1", foreign) is None


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


# --- keep_vanished: shipments the provider dropped stay in the archive -------
def _keeper(keep=True, names=None):
    coord = object.__new__(coordinator.DhlAccountDataUpdateCoordinator)
    coord.entry = types.SimpleNamespace(
        options={const.CONF_KEEP_VANISHED: keep, const.CONF_NAMES: names or {}}
    )
    coord._known = {}
    coord._known_store = None  # nothing is written to disk in these tests
    return coord


def test_vanished_shipment_is_kept_archived_until_deleted():
    coord = _keeper()
    first = {"A": {"id": "A", "name": "Shop", "status": "Unterwegs", "archived": False}}
    coord._keep_vanished(first)
    second: dict = {}  # the provider no longer lists A
    coord._keep_vanished(second)
    kept = second["A"]
    assert kept["archived"] is True and kept["vanished"] is True
    assert kept["status"] == "Unterwegs"  # last known state
    third: dict = {}
    coord._keep_vanished(third)  # still there on every later poll
    assert "A" in third


def test_vanished_shipment_that_returns_is_live_again():
    coord = _keeper()
    coord._keep_vanished({"A": {"id": "A", "status": "x"}})
    coord._keep_vanished({})
    back = {"A": {"id": "A", "status": "y", "archived": False}}
    coord._keep_vanished(back)
    assert "vanished" not in back["A"] and back["A"]["archived"] is False
    again: dict = {}
    coord._keep_vanished(again)
    assert again["A"]["status"] == "y"  # frozen at the newer state


def test_vanished_keeps_a_custom_name():
    coord = _keeper(names={"A": "Mein Paket"})
    coord._keep_vanished({"A": {"id": "A", "name": "Shop"}})
    gone: dict = {}
    coord._keep_vanished(gone)
    assert gone["A"]["name"] == "Mein Paket"


def test_option_off_keeps_nothing_and_forgets():
    coord = _keeper(keep=False)
    coord._known = {"A": {"id": "A"}}
    result: dict = {}
    coord._keep_vanished(result)
    assert result == {} and coord._known == {}


def test_typed_in_numbers_never_keep_vanished():
    coord = object.__new__(coordinator.TrackingNumbersDataUpdateCoordinator)
    assert coord._keeps_vanished is False


def test_forgetting_a_vanished_shipment():
    coord = _keeper()
    coord._known = {"A": {"id": "A", "vanished": True}}
    published = []
    coord.data = {"A": {"id": "A", "vanished": True}, "B": {"id": "B"}}
    coord.async_set_updated_data = published.append
    assert run(coord.async_forget_vanished("A")) is True
    assert published == [{"B": {"id": "B"}}] and "A" not in coord._known
    assert run(coord.async_forget_vanished("B")) is False  # a live shipment


def test_switching_the_option_acts_immediately():
    coord = _keeper()
    coord._keeps_vanished = True
    coord.data = {"A": {"id": "A", "status": "x"}}
    coord.apply_keep_vanished(True)
    assert coord._known["A"]["status"] == "x"  # remembered before it can vanish

    kept = {"A": {"id": "A", "vanished": True}, "B": {"id": "B"}}
    coord.data = kept
    published = []
    coord.async_set_updated_data = published.append
    coord.apply_keep_vanished(False)
    assert coord._known == {} and published == [{"B": {"id": "B"}}]
