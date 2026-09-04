"""DataUpdateCoordinators for the Paketverfolgung integration.

Two coordinators, both producing the same normalized ``dict[id, shipment]``
shape so the sensor/panel code is carrier-agnostic:

* ``TrackingNumbersDataUpdateCoordinator`` - a manually-managed list of
  tracking numbers. Each number's carrier (DHL, DPD or Hermes) is
  auto-detected and remembered, then only that carrier is queried on
  later refreshes.
* ``DpdAccountDataUpdateCoordinator`` - every parcel on a myDPD account,
  enriched with the full scan history from DPD's public tracking API.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ARCHIVE_AFTER_HOURS,
    CARRIER_DHL,
    CARRIER_DPD,
    CARRIER_HERMES,
    CARRIER_UNKNOWN,
    CARRIERS,
    CONF_CARRIER_OVERRIDES,
    CONF_DEFAULT_POSTCODE,
    CONF_DHL_AUTO_DISCOVERY,
    CONF_DHL_SESSION,
    CONF_DIRECTION_OVERRIDES,
    CONF_NAMES,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_OUT_FOR_DELIVERY_ONLY,
    CONF_NOTIFY_SHORT_NAME,
    CONF_NOTIFY_TARGETS,
    CONF_DPD_PASSWORD,
    CONF_DPD_USERNAME,
    CONF_TRACKING_NUMBERS,
    DEFAULT_STATUS,
    DOMAIN,
    DIRECTIONS,
    DPD_STATUS_GROUP,
    DPD_TRACKING_PAGE_URL,
    EVENT_NOTIFICATION,
    GROUP_DELIVERED,
    GROUP_OUT_FOR_DELIVERY,
    GROUP_REGISTERED,
    GROUP_TRANSIT,
    GROUP_UNKNOWN,
    NO_DATA_STATUS,
    PANEL_URL_PATH,
    PROGRESS_GROUP,
    PROGRESS_STATUS,
    SIGNAL_COORDINATOR_UPDATED,
    TRACKING_PAGE_URL,
)
from .dhl_account import DhlAccountClient, DhlAuthError
from .dhl_api import DhlApiClient, DhlApiError
from .dpd_api import DpdApiClient, DpdApiError, DpdAuthError, DpdSession
from .dpd_tracking_api import DpdTrackingApiClient, DpdTrackingApiError
from .hermes_tracking_api import HermesTrackingApiClient, HermesTrackingApiError

_LOGGER = logging.getLogger(__name__)

# Cap on DPD public-tracking calls per account poll (history enrichment),
# so a large myDPD account doesn't fan out into dozens of HTTP requests.
_HISTORY_FETCHES_PER_POLL = 12

# A parcel's status can carry Amazon's live "N Stopps entfernt" van countdown.
# That number changes every few minutes and must not, on its own, count as a
# status change worth a push notification.
_VOLATILE_STATUS_RE = re.compile(
    r"[.,\s]*\b\d+\s*(?:Stopps?|Lieferstopps?|stops?)\b[^.]*", re.I
)


def _stable_status(status: str | None) -> str:
    return _VOLATILE_STATUS_RE.sub("", status or "").strip(" .,")


def _short_name(name: str, limit: int = 42) -> str:
    """Trim a long shipment name (Amazon product titles) for a notification.

    Word-boundary cut - no comma split, German uses "," as a decimal point
    ("Kabel 0,25M").
    """
    name = (name or "").strip()
    if len(name) <= limit:
        return name
    cut = name[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-–—") or name[:limit]
    return cut + " …"


def format_notification(
    item: dict, action: str, short_name: bool
) -> tuple[str, str]:
    """(title, message) for a shipment notification. Title = the status line
    (what changed), message = the shipment name."""
    name = item.get("name") or item.get("id") or "Sendung"
    if short_name:
        name = _short_name(name)
    carrier = (item.get("carrier") or "").upper()
    status = (item.get("status") or "").strip()
    if action == "detected":
        headline = "Neue Sendung" + (f" · {carrier}" if carrier else "")
    elif not status or status == NO_DATA_STATUS:
        headline = "Zugestellt" if item.get("delivered") else "Sendungs-Update"
    else:
        headline = status
    return f"\U0001f4e6 {headline}", name


# DHL's sendungsrichtung -> the panel's canonical direction values. Note the
# anonymous tracking endpoint returns "ANKOMMEND" for every parcel, sender's
# own shipments included - hence the per-shipment override.
_DHL_DIRECTION = {"ANKOMMEND": "receive", "EINGEHEND": "receive", "AUSGEHEND": "send"}


def normalize_dhl_shipment(raw: dict) -> dict:
    """Turn a raw DHL search result into the shared shipment shape."""
    info = raw.get("sendungsinfo", {})
    details = raw.get("sendungsdetails", {})
    verlauf = details.get("sendungsverlauf", {})
    zustellung = details.get("zustellung", {})
    fortschritt = verlauf.get("fortschritt")

    status = (
        verlauf.get("kurzStatus")
        or verlauf.get("status")
        or PROGRESS_STATUS.get(fortschritt, DEFAULT_STATUS)
    )
    # DHL returns the full history oldest-first; the panel wants newest first.
    events = [
        {"datum": event.get("datum"), "status": event.get("status")}
        for event in verlauf.get("events", [])
        if event.get("status")
    ]
    events.reverse()

    group = PROGRESS_GROUP.get(fortschritt, GROUP_UNKNOWN)
    # "Elektronisch angekündigt/angemeldet" = only the label data reached DHL,
    # the parcel is not in the network yet. DHL still reports fortschritt 1
    # for this, which PROGRESS_GROUP maps to "transit" - override it.
    if group == GROUP_TRANSIT and "elektronisch ang" in status.lower():
        group = GROUP_REGISTERED

    shipment_id = raw["id"]
    return {
        "id": shipment_id,
        "carrier": CARRIER_DHL,
        "name": info.get("sendungsname") or f"DHL {shipment_id}",
        "status": status,
        "group": group,
        "direction": _DHL_DIRECTION.get(
            str(info.get("sendungsrichtung") or "").upper(),
            info.get("sendungsrichtung"),
        ),
        "delivery_from": zustellung.get("zustellzeitfensterVon"),
        "delivery_to": zustellung.get("zustellzeitfensterBis"),
        "tracking_url": TRACKING_PAGE_URL.format(id=shipment_id),
        "events": events,
        "delivered": fortschritt == 5,
        "protected": False,
    }


def normalize_dpd_parcel(raw: dict, events: list[dict] | None) -> dict:
    """Turn a raw myDPD SOAP parcel into the shared shipment shape."""
    group = DPD_STATUS_GROUP.get(raw.get("status_id"), GROUP_UNKNOWN)
    delivered = bool(raw.get("delivered")) or group == GROUP_DELIVERED
    parcel_id = raw["id"]
    return {
        "id": parcel_id,
        "carrier": CARRIER_DPD,
        "name": raw.get("name") or f"DPD {parcel_id}",
        "status": raw.get("status") or DEFAULT_STATUS,
        "group": GROUP_DELIVERED if delivered else group,
        "direction": raw.get("direction"),
        "delivery_from": None,
        "delivery_to": None,
        "tracking_url": DPD_TRACKING_PAGE_URL.format(id=parcel_id),
        "events": events or [],
        "delivered": delivered,
        "protected": False,
    }


def _has_data(item: dict | None) -> bool:
    """True if a lookup returned real tracking substance (so the number can
    be *locked* to that carrier). A soft result - a DPD "postcode needed"
    placeholder, an empty shell - is shown but must not lock, so the other
    carriers still get probed next cycle.
    """
    if not item or item.get("protected"):
        return False
    return bool(
        item.get("events")
        or item.get("delivered")
        or item.get("group") not in (None, GROUP_UNKNOWN)
    )


def _dhl_has_data(raw: dict) -> bool:
    """True if DHL actually knows this shipment (not just an echo shell).

    DHL's search echoes unknown piececodes back with an otherwise-empty
    sendungsverlauf, which would be mistaken for a freshly-registered DHL
    shipment and wrongly lock the number to DHL (e.g. a Hermes number).
    """
    details = raw.get("sendungsdetails") or {}
    if raw.get("sendungNichtGefunden") or details.get("sendungNichtGefunden"):
        return False
    verlauf = details.get("sendungsverlauf") or {}
    return bool(
        verlauf.get("kurzStatus")
        or verlauf.get("status")
        or verlauf.get("events")
        or details.get("zustellung")
        or details.get("istZugestellt")
    )


def _placeholder(number: str, carrier: str) -> dict:
    """Shipment shown for a number neither carrier has data for (yet)."""
    return {
        "id": number,
        "carrier": carrier,
        "name": number,
        "status": NO_DATA_STATUS,
        "group": GROUP_UNKNOWN if carrier == CARRIER_UNKNOWN else GROUP_REGISTERED,
        "direction": None,
        "delivery_from": None,
        "delivery_to": None,
        "tracking_url": None,
        "events": [],
        "delivered": False,
        "protected": False,
    }


ARCHIVE_AFTER = timedelta(hours=ARCHIVE_AFTER_HOURS)


def _delivery_moment(item: dict) -> datetime | None:
    """When the parcel was delivered, from its newest (parseable) event."""
    for event in item.get("events") or []:
        parsed = dt_util.parse_datetime(str(event.get("datum") or ""))
        if parsed:
            return dt_util.as_utc(parsed)
    return None


class _BaseCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Adds "when is the next poll" bookkeeping for the panel countdown."""

    last_poll: datetime | None = None
    entry: ConfigEntry

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # id -> ISO timestamp we consider the delivery moment. Only used as
        # a fallback when the carrier gives no dated delivery event (e.g.
        # DPD account parcels on a host where tracking.dpd.de is blocked).
        # Persisted so the "24h after delivery" archive clock survives a
        # restart instead of resetting on every reload.
        self._delivered_since: dict[str, str] = {}
        self._archive_store: Store | None = None
        self._archive_dirty = False
        self._notify_primed = False

    @property
    def next_poll(self) -> datetime | None:
        if self.last_poll is None or self.update_interval is None:
            return None
        return self.last_poll + self.update_interval

    @callback
    def async_update_listeners(self) -> None:
        # Also nudge the cross-provider summary sensors, which can't rely on
        # having subscribed to this particular coordinator (see
        # SIGNAL_COORDINATOR_UPDATED).
        super().async_update_listeners()
        async_dispatcher_send(self.hass, SIGNAL_COORDINATOR_UPDATED)

    def _apply_direction_overrides(self, result: dict[str, dict]) -> None:
        """Pin "Richtung" per shipment where auto-detection can't know it
        (a plain tracking number gives no sender/recipient context)."""
        raw = self.entry.options.get(CONF_DIRECTION_OVERRIDES, {}) or {}
        for sid, item in result.items():
            forced = raw.get(str(sid))
            forced = forced if forced in DIRECTIONS else None
            if forced:
                item["direction"] = forced
            item["forced_direction"] = forced

    def _mark_polled(self) -> None:
        self.last_poll = dt_util.utcnow()

    def _notify_targets(self) -> list[str]:
        opts = self.entry.options
        if not opts.get(CONF_NOTIFY_ENABLED):
            return []
        raw = opts.get(CONF_NOTIFY_TARGETS) or []
        if isinstance(raw, str):
            raw = [raw]
        out: list[str] = []
        for value in raw:
            value = str(value or "").strip()
            if value.startswith("notify."):
                value = value[len("notify."):]
            if value and value not in out:
                out.append(value)
        return out

    def _notify_changes(self, new: dict[str, dict]) -> None:
        """Send a notification on a new shipment or a status change.

        The first run after enabling only records the baseline, so you
        don't get a burst for shipments that already existed.
        """
        primed = self._notify_primed
        self._notify_primed = True
        opts = self.entry.options
        if not opts.get(CONF_NOTIFY_ENABLED):
            return
        # "Only on out-for-delivery" mode: skip the new-shipment and every
        # intermediate-scan message; notify only when a shipment *enters*
        # the out-for-delivery (or delivered) phase.
        ofd_only = bool(opts.get(CONF_NOTIFY_OUT_FOR_DELIVERY_ONLY))
        ofd_groups = (GROUP_OUT_FOR_DELIVERY, GROUP_DELIVERED)
        targets = self._notify_targets()
        old = self.data or {}
        for sid, item in new.items():
            if item.get("archived"):
                continue
            prev = old.get(sid)
            status = item.get("status") or ""
            group = item.get("group")
            if prev is None:
                if not (primed and status != NO_DATA_STATUS):
                    continue
                if ofd_only and group not in ofd_groups:
                    continue
                self._push_notification(targets, "detected", item, None)
                continue
            changed = _stable_status(status) != _stable_status(
                prev.get("status")
            ) or group != prev.get("group")
            if not changed:
                continue
            if ofd_only and (
                group == prev.get("group") or group not in ofd_groups
            ):
                continue
            self._push_notification(
                targets, "changed", item, prev.get("status")
            )

    def _push_notification(
        self,
        targets: list[str],
        action: str,
        item: dict,
        previous_status: str | None,
    ) -> None:
        name = item.get("name") or item.get("id")
        status = (item.get("status") or "").strip()
        title, message = format_notification(
            item,
            action,
            bool(self.entry.options.get(CONF_NOTIFY_SHORT_NAME)),
        )

        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_{item.get('id')}"
        )
        click_path = f"/{PANEL_URL_PATH}/{entity_id}" if entity_id else f"/{PANEL_URL_PATH}"

        self.hass.bus.async_fire(
            EVENT_NOTIFICATION,
            {
                "action": action,
                "tracking_id": item.get("id"),
                "name": name,
                "carrier": item.get("carrier"),
                "delivery_carrier": item.get("delivery_carrier"),
                "status": status,
                "previous_status": previous_status,
                "group": item.get("group"),
                "delivered": bool(item.get("delivered")),
                "tracking_url": item.get("tracking_url"),
            },
        )
        for service in targets:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "notify",
                    service,
                    {
                        "title": title,
                        "message": message,
                        "data": {"clickAction": click_path},
                    },
                    blocking=False,
                )
            )

    async def _load_archive(self) -> None:
        if self._archive_store is None:
            self._archive_store = Store(
                self.hass, 1, f"{DOMAIN}.archive.{self.entry.entry_id}"
            )
            stored = await self._archive_store.async_load()
            self._delivered_since = dict(stored) if stored else {}

    async def _save_archive(self, live_ids: set[str]) -> None:
        for stale in [i for i in self._delivered_since if i not in live_ids]:
            del self._delivered_since[stale]
            self._archive_dirty = True
        if self._archive_dirty and self._archive_store is not None:
            await self._archive_store.async_save(self._delivered_since)
            self._archive_dirty = False

    async def _apply_archive(self, item: dict) -> None:
        """Set ``item['archived']`` and ``item['delivered_at']`` (ISO)."""
        sid = str(item.get("id") or "")
        if not item.get("delivered"):
            if sid in self._delivered_since:
                del self._delivered_since[sid]
                self._archive_dirty = True
            item["delivered_at"] = None
            item["archived"] = False
            return

        moment = _delivery_moment(item)  # a dated carrier event is best
        if moment is None:
            iso = self._delivered_since.get(sid)
            if iso is None:
                moment = await self._estimate_delivery_moment(sid, item)
                self._delivered_since[sid] = moment.isoformat()
                self._archive_dirty = True
            else:
                moment = dt_util.parse_datetime(iso) or dt_util.utcnow()

        item["delivered_at"] = moment.isoformat()
        item["archived"] = dt_util.utcnow() - moment >= ARCHIVE_AFTER

    async def _estimate_delivery_moment(self, sid: str, item: dict) -> datetime:
        """Best guess at when an already-delivered parcel (no dated event)
        was delivered: the first time the recorder saw its sensor reach the
        current status. Falls back to now() (recorder off / out of range /
        parcel new)."""
        status = item.get("status")
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, f"{self.entry.entry_id}_{sid}"
        )
        if not entity_id or not status:
            return dt_util.utcnow()
        try:
            from homeassistant.components.recorder import get_instance, history

            def _first_seen() -> datetime | None:
                start = dt_util.utcnow() - timedelta(days=10)
                states = history.state_changes_during_period(
                    self.hass, start, dt_util.utcnow(), entity_id,
                    include_start_time_state=False, no_attributes=True,
                ).get(entity_id, [])
                for state in states:
                    if state.state == status:
                        return dt_util.as_utc(state.last_changed)
                return None

            found = await get_instance(self.hass).async_add_executor_job(_first_seen)
            return found or dt_util.utcnow()
        except Exception:  # noqa: BLE001 - recorder optional, never break a poll
            return dt_util.utcnow()


class TrackingNumbersDataUpdateCoordinator(_BaseCoordinator):
    """Tracks a manually-managed list of DHL / DPD / Hermes tracking numbers."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)
        self.entry = entry
        session = async_get_clientsession(hass)
        self.dhl = DhlApiClient(session)
        self.dpd = DpdTrackingApiClient(session)
        self.hermes = HermesTrackingApiClient(session)
        self.dhl_account = DhlAccountClient(session)
        # number -> detected carrier; kept in memory (re-detected after a
        # restart, which just means one extra probe per number).
        self.carriers: dict[str, str] = {}
        # last DHL-account-discovery outcome, for diagnostics
        self.dhl_account_status: str | None = None

    def _config(self, key, default=None):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    async def _async_update_data(self) -> dict[str, dict]:
        self._mark_polled()
        await self._load_archive()
        numbers = [
            str(n).strip()
            for n in self._config(CONF_TRACKING_NUMBERS, [])
            if str(n).strip()
        ]
        numbers = await self._merge_dhl_account_numbers(numbers)
        postcode = (self._config(CONF_DEFAULT_POSTCODE) or "").strip() or None
        overrides = {
            str(k).strip(): v
            for k, v in (self._config(CONF_CARRIER_OVERRIDES, {}) or {}).items()
            if v in CARRIERS
        }
        names = {
            str(k).strip(): str(v).strip()
            for k, v in (self._config(CONF_NAMES, {}) or {}).items()
            if str(v).strip()
        }
        if not numbers:
            self.carriers.clear()
            return {}

        def _frozen(n: str) -> dict | None:
            """A previously-archived item, unless its carrier override just
            changed - archived shipments are no longer re-queried."""
            prev = (self.data or {}).get(n)
            if prev and prev.get("archived") and overrides.get(n) in (
                None,
                prev.get("carrier"),
            ):
                return prev
            return None

        # Only batch-query DHL for numbers that could still be DHL: not
        # locked to another carrier, not pinned to a non-DHL one, and not
        # already archived.
        dhl_by_id = await self._dhl_lookup(
            [
                n
                for n in numbers
                if _frozen(n) is None
                and overrides.get(n, CARRIER_DHL) == CARRIER_DHL
                and self.carriers.get(n, CARRIER_UNKNOWN)
                not in (CARRIER_DPD, CARRIER_HERMES)
            ]
        )

        result: dict[str, dict] = {}
        for number in numbers:
            forced = overrides.get(number)
            known = self.carriers.get(number, CARRIER_UNKNOWN)
            carrier = forced or known
            item: dict | None = None

            frozen = _frozen(number)
            if frozen is not None:
                item = dict(frozen)
                item["forced"] = forced
                item.setdefault("carrier_name", item.get("name"))
                item["custom_name"] = names.get(number)
                item["name"] = names.get(number) or item["carrier_name"]
                item["archived"] = True
                self.carriers[number] = item["carrier"]
                result[number] = item
                continue

            raw_dhl = dhl_by_id.get(number)
            dhl_confirmed = raw_dhl is not None and _dhl_has_data(raw_dhl)

            if forced == CARRIER_DHL:
                item = normalize_dhl_shipment(raw_dhl) if raw_dhl is not None else None
            elif forced == CARRIER_DPD:
                item = await self._dpd_lookup(number, postcode)
            elif forced == CARRIER_HERMES:
                item = await self._hermes_lookup(number)
            elif dhl_confirmed:
                item, carrier = normalize_dhl_shipment(raw_dhl), CARRIER_DHL
            else:
                item, carrier = await self._detect(number, postcode, known)

            if item is None:
                # Nothing this cycle - keep the last real data (transient
                # carrier outage, still-propagating number) over blanking.
                previous = (self.data or {}).get(number)
                if previous and previous.get("status") != NO_DATA_STATUS:
                    item = previous
                    if not forced and carrier == CARRIER_UNKNOWN:
                        carrier = previous.get("carrier", carrier)
                else:
                    item = _placeholder(number, carrier)

            item["carrier"] = carrier
            item["forced"] = forced
            # Keep the carrier's own name so a cleared custom name reverts.
            item.setdefault("carrier_name", item.get("name"))
            item["custom_name"] = names.get(number)
            item["name"] = names.get(number) or item["carrier_name"]
            await self._apply_archive(item)
            self.carriers[number] = carrier
            result[number] = item

        for stale in [n for n in self.carriers if n not in numbers]:
            self.carriers.pop(stale, None)
        self._apply_direction_overrides(result)
        await self._save_archive(set(result))
        self._notify_changes(result)
        return result

    async def _merge_dhl_account_numbers(self, numbers: list[str]) -> list[str]:
        """If DHL account discovery is on, add the account's shipment ids."""
        if not self._config(CONF_DHL_AUTO_DISCOVERY):
            self.dhl_account_status = None
            return numbers
        dhl_session = self.entry.data.get(CONF_DHL_SESSION)
        if not dhl_session:
            self.dhl_account_status = "Kein DHL-Login hinterlegt"
            _LOGGER.warning("Paketverfolgung: DHL-Erkennung an, aber kein Login")
            return numbers
        try:
            fresh = await self.dhl_account.ensure_fresh(dict(dhl_session))
            if fresh != dhl_session:
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={**self.entry.data, CONF_DHL_SESSION: fresh},
                )
            account_ids = await self.dhl_account.fetch_shipment_ids(fresh)
        except DhlAuthError as err:
            self.dhl_account_status = str(err)
            _LOGGER.warning("Paketverfolgung: DHL-Kontoabfrage fehlgeschlagen: %s", err)
            return numbers

        merged = list(numbers)
        for shipment_id in account_ids:
            if shipment_id not in merged:
                merged.append(shipment_id)
                self.carriers.setdefault(shipment_id, CARRIER_DHL)
        self.dhl_account_status = f"{len(account_ids)} Sendung(en) erkannt"
        _LOGGER.debug("Paketverfolgung: DHL-Konto -> %s", account_ids)
        return merged

    async def _detect(
        self, number: str, postcode: str | None, known: str
    ) -> tuple[dict | None, str]:
        """Probe DPD then Hermes and return (item, carrier).

        A carrier is *locked* only on a substantive result. A soft result
        (DPD "postcode needed", an empty shell) is shown but leaves the
        number open for re-detection. An already-known carrier is probed
        first (cheap path) and its lock is kept through a transient
        failure as long as we still hold substantive history for it.
        """
        order = [CARRIER_HERMES, CARRIER_DPD] if known == CARRIER_HERMES else [
            CARRIER_DPD,
            CARRIER_HERMES,
        ]
        soft: dict | None = None
        for candidate in order:
            if candidate == CARRIER_DPD:
                res = await self._dpd_lookup(number, postcode)
            else:
                res = await self._hermes_lookup(number)
            if _has_data(res):
                return res, candidate
            if res is not None and soft is None:
                soft = res

        previous = (self.data or {}).get(number)
        if known in (CARRIER_DPD, CARRIER_HERMES) and _has_data(previous):
            return previous, known  # transient blip - keep the lock
        if soft is not None:
            return soft, CARRIER_UNKNOWN  # show it, stay open for re-detection
        return None, CARRIER_UNKNOWN

    async def _dhl_lookup(self, candidates: list[str]) -> dict[str, dict]:
        if not candidates:
            return {}
        try:
            shipments = await self.dhl.fetch_shipments(candidates)
        except DhlApiError as err:
            # A carrier being briefly unreachable shouldn't blank every
            # sensor - keep the last data by not raising UpdateFailed.
            _LOGGER.warning("DHL lookup failed: %s", err)
            return {}
        return {s["id"]: s for s in shipments if s.get("id")}

    async def _dpd_lookup(self, number: str, postcode: str | None) -> dict | None:
        """Fresh DPD result, or None on any failure (the caller decides
        whether to fall back to the last-known data)."""
        try:
            return await self.dpd.fetch(number, postcode)
        except DpdTrackingApiError as err:
            _LOGGER.warning("DPD lookup for %s failed: %s", number, err)
            return None

    async def _hermes_lookup(self, number: str) -> dict | None:
        try:
            return await self.hermes.fetch(number)
        except HermesTrackingApiError as err:
            _LOGGER.warning("Hermes lookup for %s failed: %s", number, err)
            return None


class DpdAccountDataUpdateCoordinator(_BaseCoordinator):
    """Fetches every parcel on a myDPD account, with full scan history."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_dpd", update_interval=update_interval
        )
        self.entry = entry
        session = async_get_clientsession(hass)
        self.client = DpdApiClient(session)
        self.tracking = DpdTrackingApiClient(session)
        # Kept in memory only (see the note in the old coordinator): DPD
        # rotates the token on most calls and persisting it would reload
        # the entry on every poll.
        self._session: DpdSession | None = None
        self._events: dict[str, list[dict]] = {}

    async def _async_update_data(self) -> dict[str, dict]:
        self._mark_polled()
        await self._load_archive()
        if self._session is None:
            await self._login()

        try:
            parcels, self._session = await self.client.fetch_parcels(self._session)
        except DpdAuthError:
            _LOGGER.info("DPD session expired, logging in again")
            await self._login()
            try:
                parcels, self._session = await self.client.fetch_parcels(self._session)
            except DpdApiError as err:
                raise UpdateFailed(f"Error fetching DPD parcels: {err}") from err
        except DpdApiError as err:
            raise UpdateFailed(f"Error fetching DPD parcels: {err}") from err

        postcode = (
            self.entry.options.get(CONF_DEFAULT_POSTCODE)
            or self.entry.data.get(CONF_DEFAULT_POSTCODE)
            or ""
        ).strip() or None
        names = {
            str(k).strip(): str(v).strip()
            for k, v in (self.entry.options.get(CONF_NAMES, {}) or {}).items()
            if str(v).strip()
        }

        # In-flight parcels first, then delivered ones - so a large account
        # (mostly old delivered parcels) backfills its history a few per
        # poll instead of firing dozens of HTTP calls at once.
        ordered = sorted(
            (p for p in parcels if p.get("id")),
            key=lambda p: bool(p.get("delivered")),
        )
        budget = _HISTORY_FETCHES_PER_POLL

        result: dict[str, dict] = {}
        for parcel in ordered:
            parcel_id = parcel["id"]
            events = self._events.get(parcel_id, [])
            # Refresh history for in-flight parcels; for delivered ones only
            # backfill once (when we have nothing cached).
            wants_history = not parcel.get("delivered") or not events
            if wants_history and budget > 0:
                budget -= 1
                fetched = await self._history(parcel_id, postcode)
                if fetched:
                    events = fetched
                    self._events[parcel_id] = fetched
            item = normalize_dpd_parcel(parcel, events)
            item["forced"] = None
            item["carrier_name"] = item["name"]
            item["custom_name"] = names.get(parcel_id)
            item["name"] = names.get(parcel_id) or item["carrier_name"]
            await self._apply_archive(item)
            result[parcel_id] = item

        for stale in [p for p in self._events if p not in result]:
            self._events.pop(stale, None)
        self._apply_direction_overrides(result)
        await self._save_archive(set(result))
        self._notify_changes(result)
        _LOGGER.debug("Paketverfolgung (DPD): %d parcel(s) fetched", len(result))
        return result

    async def _history(self, parcel_id: str, postcode: str | None) -> list[dict]:
        try:
            detail = await self.tracking.fetch(parcel_id, postcode)
        except DpdTrackingApiError as err:
            _LOGGER.debug("DPD history for %s failed: %s", parcel_id, err)
            return []
        return detail.get("events", []) if detail else []

    async def _login(self) -> None:
        username = self.entry.data[CONF_DPD_USERNAME]
        password = self.entry.data[CONF_DPD_PASSWORD]
        try:
            self._session = await self.client.login(username, password)
        except DpdAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err


# Backwards-compatible aliases (old names used elsewhere / in the wild).
DhlDataUpdateCoordinator = TrackingNumbersDataUpdateCoordinator
DpdDataUpdateCoordinator = DpdAccountDataUpdateCoordinator
