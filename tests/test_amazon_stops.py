"""HA-free test for the Amazon "N Stopps entfernt" countdown parser.

The pure function is lifted out of amazon_api.py, so the test needs neither
aiohttp nor BeautifulSoup.
"""
import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "paketverfolgung" / "amazon_api.py"


def _load():
    wanted = {"_STOPS_AWAY_RE", "_NEXT_STOP_RE", "_stops_away"}
    nodes = [
        n
        for n in ast.parse(SRC.read_text()).body
        if (isinstance(n, ast.FunctionDef) and n.name in wanted)
        or (
            isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) in wanted for t in n.targets)
        )
    ]
    ns = {"re": re}
    exec(compile(ast.Module(nodes, []), "amazon_api", "exec"), ns)
    return ns["_stops_away"]


stops_away = _load()


@pytest.mark.parametrize(
    ("callout", "expected"),
    [
        ("5 Stopps entfernt", 5),
        ("Noch 1 Stopp entfernt", 1),
        ("3 stops away", 3),
        ("Sie sind der nächste Stopp", 0),
        ("Du bist der nächste Halt", 0),
        ("You are the next stop", 0),
        ("Lieferung heute", None),
        ("", None),
        (None, None),
    ],
)
def test_stops_away(callout, expected):
    assert stops_away(callout) == expected
