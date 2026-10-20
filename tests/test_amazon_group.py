"""HA-free test for the Amazon status -> lifecycle group mapping.

amazon_coordinator imports Home Assistant, so the pure function is lifted out
of the source instead of importing the module.
"""
import ast
import importlib
import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "paketverfolgung"
PKG = "pv_amazon_group_under_test"

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(PKG_DIR)]
sys.modules[PKG] = pkg
const = importlib.import_module(f"{PKG}.const")


def _load():
    src = (PKG_DIR / "amazon_coordinator.py").read_text()
    wanted = {"_FINISHED_MARKERS", "_group_from_status"}
    nodes = [
        n
        for n in ast.parse(src).body
        if (isinstance(n, ast.FunctionDef) and n.name in wanted)
        or (
            isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) in wanted for t in n.targets)
        )
    ]
    ns = {k: getattr(const, k) for k in dir(const) if k.startswith("GROUP_")}
    exec(compile(ast.Module(nodes, []), "amazon_coordinator", "exec"), ns)
    return ns["_group_from_status"]


group = _load()


def test_refund_and_cancellation_count_as_finished():
    for text in (
        "Erstattung veranlasst",
        "Ihre Bestellung wurde storniert.",
        "Rückerstattet",
        "Order cancelled",
    ):
        assert group(text) == const.GROUP_DELIVERED, text


def test_ordinary_statuses_are_unchanged():
    assert group("Zugestellt: 13. Oktober.") == const.GROUP_DELIVERED
    assert group("Versendet. Zustellung: Samstag") == const.GROUP_TRANSIT
    assert group("Bestellt. Zustellung: Donnerstag") == const.GROUP_REGISTERED
