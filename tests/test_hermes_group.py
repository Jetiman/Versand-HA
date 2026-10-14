"""HA-free tests for the Hermes status -> lifecycle group mapping."""
import importlib
import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "paketverfolgung"
PKG = "pv_group_under_test"


def _load(name):
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG] = pkg
    return importlib.import_module(f"{PKG}.{name}")


tracking = _load("hermes_tracking_api")
account = _load("hermes_account")
const = _load("const")


def _payload(*texts):
    """parcelProgress is newest-first; codes are deliberately unknown."""
    return [
        {
            "parcelAttributes": {"delivered": False},
            "parcelProgress": [
                {"timestamp": f"2026-10-14T15:0{i}:00.000Z", "headlineText": t}
                for i, t in enumerate(texts)
            ],
        }
    ]


def test_sender_dropoff_at_paketshop_is_transit_not_out_for_delivery():
    item = tracking._parse(
        "53287110403958",
        _payload(
            "Sendung wurde vom Absender im PaketShop abgegeben.",
            "Sendung wurde Hermes elektronisch angekündigt.",
        ),
    )
    assert item["group"] == const.GROUP_TRANSIT


def test_ready_for_pickup_is_still_out_for_delivery():
    for status in (
        "Ihre Sendung liegt zur Abholung im PaketShop bereit.",
        "Sendung wurde im PaketShop bereitgestellt.",
    ):
        assert tracking._parse("X", _payload(status))["group"] == const.GROUP_OUT_FOR_DELIVERY


def test_delivered_wording_still_wins():
    item = tracking._parse("X", _payload("Sendung wurde im PaketShop zugestellt."))
    assert item["group"] == const.GROUP_DELIVERED
    assert tracking._parse("X", _payload("Sendung wurde abgeholt."))["group"] == const.GROUP_DELIVERED


def test_account_list_text_mapping():
    g = account.group_from_status
    assert g("Sendung wurde vom Absender im PaketShop abgegeben.") == const.GROUP_TRANSIT
    assert g("Sendung in Zustellung") == const.GROUP_OUT_FOR_DELIVERY
    assert g("Liegt zur Abholung im PaketShop bereit") == const.GROUP_OUT_FOR_DELIVERY
    assert g("Zugestellt") == const.GROUP_DELIVERED
