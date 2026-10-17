#!/usr/bin/env python3
"""Look at what Picnic returns for your account (no Home Assistant needed).

    python3 tools/picnic_probe.py you@example.org            # asks for the password
    python3 tools/picnic_probe.py you@example.org --country DE --raw out.json

It signs in with the same client the integration uses (SMS code is asked for
if your account has two-factor), lists your deliveries and prints how the
integration would show each one: status, group, window, name. ``--raw FILE``
additionally writes the raw API answers to FILE, so a changed API can be
compared against ``tests/test_picnic_account.py``. The file contains your
order data (no password, no token) - look at it before sharing it.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import ClientSession

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "paketverfolgung"


def load():
    pkg = types.ModuleType("pv_probe")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["pv_probe"] = pkg
    return importlib.import_module("pv_probe.picnic_account")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("username")
    ap.add_argument("--country", default="DE", choices=["DE", "NL", "FR"])
    ap.add_argument("--days", type=int, default=30, help="how many days of delivered orders to show")
    ap.add_argument("--raw", metavar="FILE", help="write the raw API answers to FILE")
    args = ap.parse_args()

    acc = load()
    password = getpass.getpass("Picnic-Passwort: ")
    async with ClientSession(**importlib.import_module("pv_probe.const").PICNIC_SESSION_KWARGS) as session:
        client = acc.PicnicAccountClient(session, country=args.country)
        try:
            await client.login(args.username, password)
        except acc.PicnicTwoFactorRequired:
            await client.generate_2fa_code()
            await client.verify_2fa_code(input("SMS-Code: ").strip())
        summaries = await client.fetch_deliveries()
        now = datetime.now(ZoneInfo("Europe/Berlin"))
        raw = {"summaries": summaries, "details": {}, "positions": {}}
        print(f"{len(summaries)} Lieferung(en) bei Picnic\n")
        for s in summaries:
            if not acc.in_history(s, now, args.days):
                continue
            did = str(s.get("delivery_id") or s.get("id") or "")
            detail = await client.fetch_delivery(did) if did else None
            position = await client.fetch_position(did) if acc.wants_position(s, now) else None
            raw["details"][did], raw["positions"][did] = detail, position
            item = acc.normalize_delivery(s, detail, position, now)
            if not item:
                continue
            window = ""
            if item["delivery_from"] and item["delivery_to"]:
                a = acc.parse_ts(item["delivery_from"]).astimezone(now.tzinfo)
                b = acc.parse_ts(item["delivery_to"]).astimezone(now.tzinfo)
                window = f"{a:%d.%m. %H:%M}–{b:%H:%M}"
            print(f"{did[:12]:12}  {item['group']:16} {item['status']:34} {window:18} {item['name']}")
            if position:
                print(f"{'':12}  Position/ETA: {json.dumps(position)[:160]}")
        if args.raw:
            Path(args.raw).write_text(json.dumps(raw, indent=2, ensure_ascii=False))
            print(f"\nRohdaten: {args.raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
