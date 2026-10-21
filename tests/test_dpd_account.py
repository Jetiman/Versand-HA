"""HA-free tests for the myDPD web-account client (dpd_api.py).

The pages are synthetic: they carry only the element ids the client reads,
which are the ones DPD's ASP.NET pages use.
"""
import asyncio
import importlib.util
import sys
import types
from datetime import date
from pathlib import Path

import pytest
from aiohttp import ClientConnectionError

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "paketverfolgung"
PKG = "pv_dpd_under_test"


def _load():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[PKG] = pkg
    for name in ("const", "dpd_api"):
        spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", PKG_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{PKG}.dpd_api"], sys.modules[f"{PKG}.const"]


dpd, const = _load()
P = "ContentPlaceHolder1_"


# --- synthetic pages -------------------------------------------------------
def _state() -> str:
    return (
        '<input type="hidden" name="__VIEWSTATE" value="vs" />'
        '<input type="hidden" name="__EVENTVALIDATION" value="ev" />'
    )


PORTAL = f'<html><body>{_state()}<a id="x" href="javascript:__doPostBack(\'btnHomeMyParcel\')">Pakete</a></body></html>'


def inbox(rows=(), incoming=None, sending=0, returning=0, panel="") -> str:
    count = len(rows) if incoming is None else incoming
    out = [f'<html><body>{_state()}<a id="modBurgerNavi_btnLogout">Abmelden</a>']
    out.append(f'<span id="{P}counterGetIncomingParcel">{count}</span>')
    out.append(f'<span id="{P}counterGetSendingParcel">{sending}</span>')
    out.append(f'<span id="{P}counterGetReturningParcel">{returning}</span>')
    for i, (barcode, label, sentence, icon) in enumerate(rows):
        out.append(f'<span id="{P}repParcelList_labParcelNo_{i}">{barcode}</span>')
        out.append(f'<span id="{P}repParcelList_labParcelName_{i}">{label}</span>')
        out.append(f'<span id="{P}repParcelList_labDeliveryStatus_{i}">{sentence}</span>')
        out.append(f'<img id="{P}repParcelList_imgDeliverStatus_{i}" src="images/status_{icon}.svg" />')
    out.append(panel)
    out.append("</body></html>")
    return "".join(out)


def panel(barcode: str, slots: dict[str, tuple[str, str]], icon: int = 2) -> str:
    html = [f'<input type="hidden" id="{P}hidParcelNo" value="{barcode}" />']
    html.append(f'<img id="{P}imgParcelStatus" src="images/status_{icon}.svg" />')
    for slot in dpd.SLOTS:
        text, day = slots.get(slot, ("", ""))
        html.append(f'<span id="{P}labStatus{slot}">{text}</span>')
        html.append(f'<span id="{P}labStatus{slot}Date">{day}</span>')
    return "".join(html)


LOGIN_FORM = '<form class="german-sso-login-form"><input name="dpg_username"></form>'
CAPTCHA_FORM = '<form class="german-sso-login-form dgs-form-has-captcha"><input name="dpg_username"></form>'
REJECTED_FORM = (
    '<form class="german-sso-login-form"><input name="dpg_username">'
    '<div class="dgs-inline-message error">Falsche Zugangsdaten</div></form>'
)


# --- fake aiohttp session ---------------------------------------------------
class _Resp:
    def __init__(self, status=200, body="", location=None, ctype="text/html"):
        self.status = status
        self._body = body
        self.headers = {"Location": location} if location else {}
        self.content_type = ctype

    async def text(self, errors="strict"):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeDpd:
    """Plays DPD: login on dpd.com, portal, parcel list with postbacks."""

    def __init__(self, *, login_page=None, pages=None, details=None, signed_in=False):
        self.login_page = login_page  # None -> login succeeds
        self.pages = pages or {}  # tab target suffix -> html
        self.details = details or {}  # row target -> html
        self.signed_in = signed_in
        self.calls: list[tuple[str, str]] = []
        self.fail = False
        self.first_inbox = inbox()

    def request(self, method, url, data=None, headers=None, allow_redirects=True, timeout=None):
        self.calls.append((method, url))
        if self.fail:
            raise ClientConnectionError("boom")
        if url.startswith(const.DPD_LOGIN_URL.split("?")[0]):
            if self.login_page is not None:
                return _Resp(200, self.login_page)
            self.signed_in = True
            return _Resp(302, location="https://portal.dpd.de/home_pn.aspx?dpd_token=abc")
        if url.startswith("https://portal.dpd.de/home_pn.aspx"):
            if method == "POST":
                return _Resp(200, self.first_inbox)
            return _Resp(200, PORTAL if self.signed_in else LOGIN_FORM)
        if url.startswith(const.DPD_INBOX_URL):
            if method == "GET":
                return _Resp(200, self.first_inbox if self.signed_in else "<html>empty inbox</html>")
            target = data["__EVENTTARGET"]
            for suffix, html in self.pages.items():
                if target.endswith(suffix):
                    return _Resp(200, html)
            for suffix, html in self.details.items():
                if target.endswith(suffix):
                    return _Resp(200, html)
        return _Resp(404, "")

    def count(self, method, needle):
        return sum(1 for m, u in self.calls if m == method and needle in u)


@pytest.fixture(autouse=True)
def _reset_backoff():
    dpd._CAPTCHA_UNTIL.clear()
    dpd._UNOPENED_UNTIL.clear()


def _client(fake, user="me@example.com"):
    return dpd.DpdApiClient(fake, user, "pw")


def _run(coro):
    return asyncio.run(coro)


# --- tests -------------------------------------------------------------------
def test_empty_account_returns_no_parcels():
    fake = FakeDpd()
    assert _run(_client(fake).fetch_parcels()) == []
    assert fake.count("POST", "dpd.com") == 1  # one login


def test_parcel_with_milestones_becomes_events_newest_first():
    row = ("01234567890123", "Schuhe", "Ihr Paket wird heute zugestellt", 4)
    slots = {
        "Start": ("Paketinfo an DPD übergeben", "19.10."),
        "OnTheRoad": ("Paket unterwegs", "20.10."),
        "CarLoad": ("Paket in Zustellung", "21.10."),
    }
    fake = FakeDpd(
        details={"btnSelectParcel": inbox([row], panel=panel(row[0], slots))},
    )
    fake.first_inbox = inbox([row])
    parcels = _run(_client(fake).fetch_parcels())
    assert len(parcels) == 1
    p = parcels[0]
    assert p["id"] == "01234567890123" and p["name"] == "Schuhe"
    assert p["status"] == "Ihr Paket wird heute zugestellt"
    assert p["status_id"] == "OUT_FOR_DELIVERY" and p["delivered"] is False
    assert p["direction"] == "receive"
    assert [e["status"] for e in p["events"]] == [
        "Paket in Zustellung",
        "Paket unterwegs",
        "Paketinfo an DPD übergeben",
    ]
    assert all(len(e["datum"]) == 10 for e in p["events"])  # ISO day


def test_delivered_milestone_marks_parcel_delivered():
    row = ("01234567890123", "", "Geliefert", 6)
    slots = {"Start": ("Paketinfo", "01.10."), "Delivered": ("Paket zugestellt", "03.10.")}
    fake = FakeDpd(details={"btnSelectParcel": inbox([row], panel=panel(row[0], slots, icon=6))})
    fake.first_inbox = inbox([row])
    p = _run(_client(fake).fetch_parcels())[0]
    assert p["delivered"] is True and p["status_id"] == "DELIVERED"
    assert p["name"] == row[0]  # no label -> barcode


def test_row_without_panel_falls_back_to_icon_and_sentence():
    delivered = ("11111111111111", "", "Zugestellt", 6)
    shop = ("22222222222222", "", "Liegt im Paketshop zur Abholung bereit", 3)
    fake = FakeDpd()  # no details served -> postback 404 -> no panel
    fake.first_inbox = inbox([delivered, shop])
    a, b = _run(_client(fake).fetch_parcels())
    assert a["status_id"] == "DELIVERED" and a["events"] == []
    assert b["status_id"] == "AT_PARCELSHOP"


def test_other_tabs_are_read_with_their_direction():
    sent = ("33333333333333", "Retoure an Shop", "Unterwegs", 2)
    fake = FakeDpd(pages={"btnGetSendingParcel": inbox([sent], incoming=0, sending=1)})
    fake.first_inbox = inbox([], sending=1)
    parcels = _run(_client(fake).fetch_parcels())
    assert [(p["id"], p["direction"]) for p in parcels] == [(sent[0], "send")]


def test_session_is_reused_between_polls():
    fake = FakeDpd()
    client = _client(fake)
    _run(client.fetch_parcels())
    _run(client.fetch_parcels())
    assert fake.count("POST", "dpd.com") == 1  # logged in only once


def test_wrong_password_is_an_auth_error():
    fake = FakeDpd(login_page=REJECTED_FORM)
    with pytest.raises(dpd.DpdAuthError):
        _run(_client(fake).fetch_parcels())


def test_captcha_is_reported_and_not_retried():
    fake = FakeDpd(login_page=CAPTCHA_FORM)
    client = _client(fake)
    with pytest.raises(dpd.DpdCaptchaError):
        _run(client.fetch_parcels())
    assert not isinstance(dpd.DpdCaptchaError("x"), dpd.DpdAuthError)  # never a reauth
    before = fake.count("POST", "dpd.com")
    with pytest.raises(dpd.DpdCaptchaError):
        _run(client.fetch_parcels())
    assert fake.count("POST", "dpd.com") == before  # back-off: no second login attempt


def test_config_flow_login_ignores_backoff():
    fake = FakeDpd(login_page=CAPTCHA_FORM)
    with pytest.raises(dpd.DpdCaptchaError):
        _run(_client(fake).fetch_parcels())
    fake.login_page = None  # DPD stopped asking
    _run(_client(fake).login())  # the user's explicit attempt goes through
    assert fake.count("POST", "dpd.com") == 2


def test_network_error_is_an_api_error_not_auth():
    fake = FakeDpd()
    fake.fail = True
    with pytest.raises(dpd.DpdApiError) as err:
        _run(_client(fake).fetch_parcels())
    assert not isinstance(err.value, dpd.DpdAuthError)


def test_login_that_opens_no_list_is_held_off():
    fake = FakeDpd()
    fake.first_inbox = "<html>no logout marker</html>"
    client = _client(fake)
    with pytest.raises(dpd.DpdApiError):
        _run(client.fetch_parcels())
    before = fake.count("POST", "dpd.com")
    with pytest.raises(dpd.DpdApiError):
        _run(client.fetch_parcels())
    assert fake.count("POST", "dpd.com") == before


@pytest.mark.parametrize(
    "days,today,expected",
    [
        (["03.10.", "05.10."], date(2026, 10, 21), [date(2026, 10, 3), date(2026, 10, 5)]),
        # a day far in the future can only be last year's
        (["28.12."], date(2026, 1, 2), [date(2025, 12, 28)]),
        # a later milestone never falls before an earlier one
        (["30.12.", "02.01."], date(2026, 1, 3), [date(2025, 12, 30), date(2026, 1, 2)]),
        (["", "-", "x"], date(2026, 10, 21), [None, None, None]),
    ],
)
def test_infer_dates(days, today, expected):
    slots = [{"slot": "s", "text": "t", "date": d} for d in days]
    assert dpd.infer_dates(slots, today) == expected


def test_const_urls_are_https_dpd_hosts():
    for url in (const.DPD_LOGIN_URL, const.DPD_PORTAL_HOME_URL, const.DPD_INBOX_URL):
        assert url.startswith("https://") and dpd._allowed_host(url)
