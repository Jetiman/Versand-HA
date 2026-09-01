"""Sensor platform for Paketverfolgung.

One sensor entity per tracked shipment/parcel (from either coordinator -
they share the same normalized shape), plus a single provider-wide
"Heute in Zustellung" summary sensor. Entities appear and disappear as
shipments enter and leave the coordinators' data.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .amazon_coordinator import AmazonAccountDataUpdateCoordinator
from .const import (
    CONF_PROVIDER,
    DEFAULT_ICON,
    DEFAULT_STATUS,
    DOMAIN,
    GROUP_ICONS,
    GROUP_OUT_FOR_DELIVERY,
    PROVIDER_NUMBERS,
)
from .coordinator import (
    DpdAccountDataUpdateCoordinator,
    TrackingNumbersDataUpdateCoordinator,
)

_SHIPMENT_COORDINATORS = (
    TrackingNumbersDataUpdateCoordinator,
    DpdAccountDataUpdateCoordinator,
    AmazonAccountDataUpdateCoordinator,
)

_DELIVERY_TODAY_UID = f"{DOMAIN}_out_for_delivery_today"
_NEXT_REFRESH_UID = f"{DOMAIN}_next_refresh"
_CANONICAL_UIDS = {_DELIVERY_TODAY_UID, _NEXT_REFRESH_UID}


def _singleton_owner_id(hass: HomeAssistant) -> str | None:
    """The one config entry that owns the cross-provider summary sensors.

    Deterministic (tracking-number entry first, else lowest entry_id) so
    the singletons don't hop between entries on restart and pile up
    "_2"/"_3" registry duplicates.
    """
    entries = sorted(
        hass.config_entries.async_entries(DOMAIN), key=lambda e: e.entry_id
    )
    for entry in entries:
        if entry.data.get(CONF_PROVIDER, PROVIDER_NUMBERS) == PROVIDER_NUMBERS:
            return entry.entry_id
    return entries[0].entry_id if entries else None


def _cleanup_singletons(hass: HomeAssistant, owner_id: str) -> None:
    """Drop stale summary-sensor registry entries (old per-entry unique
    ids, or a canonical one stuck on the wrong entry) so the freshly
    added ones reclaim the plain `sensor.heute_in_zustellung` /
    `sensor.naechste_aktualisierung` ids."""
    registry = er.async_get(hass)
    legacy = {
        f"{entry.entry_id}_out_for_delivery_today"
        for entry in hass.config_entries.async_entries(DOMAIN)
    }
    for entity in list(registry.entities.values()):
        if entity.platform != DOMAIN or entity.domain != "sensor":
            continue
        uid = entity.unique_id
        if uid in legacy or (
            uid in _CANONICAL_UIDS and entity.config_entry_id != owner_id
        ):
            registry.async_remove(entity.entity_id)

    # Reclaim the plain entity_id if a "_2"/"_3" suffixed leftover still
    # holds a canonical unique id.
    for uid in _CANONICAL_UIDS:
        eid = registry.async_get_entity_id("sensor", DOMAIN, uid)
        if not eid:
            continue
        base = re.sub(r"_\d+$", "", eid)
        if base != eid and registry.async_get(base) is None:
            registry.async_update_entity(eid, new_entity_id=base)


def _amazon_entity_id(item_id: str) -> str:
    """Canonical entity id for an Amazon shipment: sensor.amazon_<order-number>."""
    return f"sensor.amazon_{slugify(item_id)}"


def _migrate_amazon_entity_ids(hass: HomeAssistant, entry_id: str, coordinator) -> None:
    """Give live Amazon entities the stable ``sensor.amazon_<order-number>``
    id, and drop leftover registry entries from builds that keyed Amazon
    shipments by the carrier tracking number (order numbers contain dashes,
    tracking numbers don't)."""
    registry = er.async_get(hass)
    live = set(coordinator.data or {})
    prefix = f"{entry_id}_"
    for entity in list(registry.entities.values()):
        if entity.platform != DOMAIN or entity.domain != "sensor":
            continue
        if not entity.unique_id.startswith(prefix):
            continue
        item_id = entity.unique_id[len(prefix):]
        if item_id in live:
            desired = _amazon_entity_id(item_id)
            if entity.entity_id != desired and registry.async_get(desired) is None:
                registry.async_update_entity(entity.entity_id, new_entity_id=desired)
        elif entity.entity_id.startswith("sensor.amazon_") and "-" not in item_id:
            registry.async_remove(entity.entity_id)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if isinstance(coordinator, AmazonAccountDataUpdateCoordinator):
        _migrate_amazon_entity_ids(hass, entry.entry_id, coordinator)
    _setup_dynamic_entities(entry, coordinator, async_add_entities)

    # Diagnostic read-out of the optional DHL-account auto-discovery.
    if isinstance(coordinator, TrackingNumbersDataUpdateCoordinator):
        async_add_entities([DhlAccountStatusSensor(coordinator, entry.entry_id)])

    # The combined summary sensors span every provider; exactly one entry
    # owns them.
    if entry.entry_id == _singleton_owner_id(hass):
        _cleanup_singletons(hass, entry.entry_id)
        async_add_entities(
            [CombinedOutForDeliveryTodaySensor(hass), NextRefreshSensor(hass)]
        )


def _setup_dynamic_entities(
    entry: ConfigEntry, coordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Add one entity per item key in coordinator.data, kept in sync."""
    known_ids: set[str] = set()
    entities: dict[str, ShipmentSensor] = {}

    @callback
    def _sync_entities() -> None:
        current_ids = set(coordinator.data or {})

        new_ids = current_ids - known_ids
        if new_ids:
            new_entities = [
                ShipmentSensor(coordinator, entry.entry_id, item_id)
                for item_id in new_ids
            ]
            for entity in new_entities:
                entities[entity.item_id] = entity
            known_ids.update(new_ids)
            async_add_entities(new_entities)

        removed_ids = known_ids - current_ids
        for item_id in removed_ids:
            entity = entities.pop(item_id, None)
            known_ids.discard(item_id)
            if entity is not None:
                coordinator.hass.async_create_task(entity.async_remove())

    entry.async_on_unload(coordinator.async_add_listener(_sync_entities))
    _sync_entities()


class ShipmentSensor(CoordinatorEntity, SensorEntity):
    """A single tracked shipment/parcel (carrier-agnostic)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry_id: str, item_id: str) -> None:
        super().__init__(coordinator)
        self.item_id = item_id
        self._attr_unique_id = f"{entry_id}_{item_id}"
        # Amazon's shipment "name" is the product title; suggest a stable
        # entity_id up front. The registry stays authoritative and keeps an
        # existing id for the same unique_id.
        if isinstance(coordinator, AmazonAccountDataUpdateCoordinator):
            self.entity_id = _amazon_entity_id(item_id)

    @property
    def _shipment(self) -> dict:
        return (self.coordinator.data or {}).get(self.item_id, {})

    @property
    def available(self) -> bool:
        return super().available and self.item_id in (self.coordinator.data or {})

    @property
    def name(self) -> str:
        return self._shipment.get("name") or self.item_id

    @property
    def native_value(self) -> str:
        return self._shipment.get("status") or DEFAULT_STATUS

    @property
    def icon(self) -> str:
        return GROUP_ICONS.get(self._shipment.get("group"), DEFAULT_ICON)

    @property
    def extra_state_attributes(self) -> dict:
        s = self._shipment
        return {
            "tracking_id": self.item_id,
            "carrier_tracking_id": s.get("tracking_id"),
            "carrier": s.get("carrier"),
            "delivery_carrier": s.get("delivery_carrier"),
            "forced_carrier": s.get("forced"),
            "custom_name": s.get("custom_name"),
            "group": s.get("group"),
            "removable": isinstance(
                self.coordinator, TrackingNumbersDataUpdateCoordinator
            ),
            "direction": s.get("direction"),
            "delivered": s.get("delivered"),
            "archived": bool(s.get("archived")),
            "delivered_at": s.get("delivered_at"),
            "protected": s.get("protected"),
            "delivery_window_from": s.get("delivery_from"),
            "delivery_window_to": s.get("delivery_to"),
            "tracking_url": s.get("tracking_url"),
            "order_id": s.get("order_id"),
            "short_status": s.get("short_status"),
            "events": s.get("events", []),
        }


class DhlAccountStatusSensor(CoordinatorEntity, SensorEntity):
    """Last outcome of the optional DHL-account shipment auto-discovery."""

    _attr_has_entity_name = True
    _attr_name = "DHL-Konto Erkennung"
    _attr_icon = "mdi:account-search-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_dhl_account_status"

    @property
    def native_value(self) -> str:
        return getattr(self.coordinator, "dhl_account_status", None) or "Aus"


def _out_for_delivery(coordinator) -> list[dict]:
    out = []
    for shipment in (coordinator.data or {}).values():
        if shipment.get("group") != GROUP_OUT_FOR_DELIVERY or shipment.get("delivered"):
            continue
        out.append(
            {
                "tracking_id": shipment.get("id"),
                "status": shipment.get("status"),
                "tracking_url": shipment.get("tracking_url"),
                "provider": (shipment.get("carrier") or "").upper(),
            }
        )
    return out


class _AllCoordinatorsSensor(SensorEntity):
    """Base for the summary sensors that span every configured provider.

    Not a CoordinatorEntity - it manually subscribes to every shipment
    coordinator present when it's added and recomputes on any of them.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._unsub_listeners: list[Callable[[], None]] = []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for coordinator in self.hass.data[DOMAIN].values():
            if isinstance(coordinator, _SHIPMENT_COORDINATORS):
                self._unsub_listeners.append(
                    coordinator.async_add_listener(self._handle_coordinator_update)
                )

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def _coordinators(self):
        return [
            c
            for c in self.hass.data[DOMAIN].values()
            if isinstance(c, _SHIPMENT_COORDINATORS)
        ]


class CombinedOutForDeliveryTodaySensor(_AllCoordinatorsSensor):
    """Counts shipments currently out for delivery, across *all* providers."""

    _attr_name = "Heute in Zustellung"
    _attr_icon = "mdi:truck-delivery"
    _attr_native_unit_of_measurement = "Sendungen"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self._attr_unique_id = f"{DOMAIN}_out_for_delivery_today"

    def _shipments(self) -> list[dict]:
        items: list[dict] = []
        for coordinator in self._coordinators():
            items.extend(_out_for_delivery(coordinator))
        return items

    @property
    def native_value(self) -> int:
        return len(self._shipments())

    @property
    def extra_state_attributes(self) -> dict:
        shipments = self._shipments()
        # Distinct carriers among today's deliveries, as a plain
        # comma-joined string (e.g. "DHL" / "DPD" / "DHL, DPD") so it can be
        # shown directly as a dashboard tile's secondary line via
        # state_content, without a template needing to unpack `shipments`.
        carriers = sorted({s["provider"] for s in shipments if s.get("provider")})
        next_polls = [c.next_poll for c in self._coordinators() if c.next_poll]
        return {
            "shipments": shipments,
            "carriers": ", ".join(carriers),
            "next_update": min(next_polls).isoformat() if next_polls else None,
        }


class NextRefreshSensor(_AllCoordinatorsSensor):
    """When the shipment data is next refreshed (soonest across providers)."""

    _attr_name = "Nächste Aktualisierung"
    _attr_icon = "mdi:timer-sync-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self._attr_unique_id = f"{DOMAIN}_next_refresh"

    @property
    def native_value(self) -> datetime | None:
        next_polls = [c.next_poll for c in self._coordinators() if c.next_poll]
        return min(next_polls) if next_polls else None
