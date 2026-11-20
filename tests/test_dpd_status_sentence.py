"""HA-free tests: DPD status from the list sentence and its cross-check."""
import importlib.util
import sys
import types
from datetime import date
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "paketverfolgung"
PKG = "pv_dpd_sentence_under_test"


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
TODAY = date(2026, 11, 20)


def _row(sentence, icon=None):
    return {
        "index": 0,
        "barcode": "01016814572472",
        "label": "",
        "status_sentence": sentence,
        "status_icon": icon,
    }


def _detail(**dates):
    slots = [
        {"slot": s, "text": s, "date": dates.get(s, "")} for s in dpd.SLOTS
    ]
    return {"identity": "01016814572472", "slots": slots, "status_icon": None}


@pytest.mark.parametrize(
    "sentence,expected",
    [
        ("Auftragsdaten übermittelt", "DATA_TRANSMITTED"),
        ("Paket angekündigt", "DATA_TRANSMITTED"),
        ("Unterwegs", "ON_THE_ROAD"),
        ("Ihr Paket ist unterwegs", "ON_THE_ROAD"),
        ("Im Paketzentrum angekommen", "ON_THE_ROAD"),
        ("Im Zustelldepot", "AT_DELIVERY_DEPOT"),
        ("Ihr Paket wird heute zugestellt", "OUT_FOR_DELIVERY"),
        ("Paket in Zustellung", "OUT_FOR_DELIVERY"),
        ("Liegt im Paketshop zur Abholung bereit", "AT_PARCELSHOP"),
        ("Zugestellt", "DELIVERED"),
        ("Nicht zugestellt", None),
        ("", None),
    ],
)
def test_status_id_from_sentence(sentence, expected):
    assert dpd._status_id_from_sentence(sentence) == expected


def test_announced_sentence_is_registered_not_unknown():
    parcel = dpd.build_parcel(_row("Auftragsdaten übermittelt"), None, "receive", TODAY)
    group = const.DPD_STATUS_GROUP[parcel["status_id"]]
    assert group == const.GROUP_REGISTERED


def test_in_transit_sentence_without_panel_is_transit():
    parcel = dpd.build_parcel(_row("Unterwegs"), None, "receive", TODAY)
    assert const.DPD_STATUS_GROUP[parcel["status_id"]] == const.GROUP_TRANSIT


def test_sentence_ahead_of_milestones_wins():
    # DPD already says "Unterwegs", the panel only has the Start milestone yet.
    parcel = dpd.build_parcel(
        _row("Unterwegs"), _detail(Start="20.11."), "receive", TODAY
    )
    assert parcel["status_id"] == "ON_THE_ROAD"
    assert len(parcel["events"]) == 1


def test_milestones_ahead_of_sentence_win():
    parcel = dpd.build_parcel(
        _row("Auftragsdaten übermittelt"),
        _detail(Start="19.11.", OnTheRoad="20.11."),
        "receive",
        TODAY,
    )
    assert parcel["status_id"] == "ON_THE_ROAD"


def test_unrecognised_sentence_keeps_milestone_status():
    parcel = dpd.build_parcel(
        _row("Irgendein neuer Satz"), _detail(Start="20.11."), "receive", TODAY
    )
    assert parcel["status_id"] == "START"
