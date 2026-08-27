"""Sensor platform for Paketverfolgung.

One sensor entity per tracked shipment/parcel (from either coordinator -
they share the same normalized shape), plus a single provider-wide
"Heute in Zustellung" summary sensor. Entities appear and disappear as
shipments enter and leave the coordinators' data.
"""
from __future__ import annotations

from typing import Callable

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    COMBINED_SENSOR_ADDED_KEY,
    DEFAULT_ICON,
    DEFAULT_STATUS,
    DOMAIN,
    GROUP_ICONS,
    GROUP_OUT_FOR_DELIVERY,
)
from .coordinator import (
    DpdAccountDataUpdateCoordinator,
    TrackingNumbersDataUpdateCoordinator,
)

_SHIPMENT_COORDINATORS = (
    TrackingNumbersDataUpdateCoordinator,
    DpdAccountDataUpdateCoordinator,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    _setup_dynamic_entities(entry, coordinator, async_add_entities)

    # One combined summary entity for all providers together, added under
    # whichever entry sets up first - guarded (by that owning entry_id, not
    # just a bool) so a second provider entry doesn't add a duplicate, and
    # so a *different* entry reloading doesn't wrongly re-trigger it while
    # the owner's copy is still alive.
    if hass.data[DOMAIN].get(COMBINED_SENSOR_ADDED_KEY) is None:
        hass.data[DOMAIN][COMBINED_SENSOR_ADDED_KEY] = entry.entry_id
        async_add_entities([CombinedOutForDeliveryTodaySensor(hass)])


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
            "carrier": s.get("carrier"),
            "group": s.get("group"),
            "removable": isinstance(
                self.coordinator, TrackingNumbersDataUpdateCoordinator
            ),
            "direction": s.get("direction"),
            "delivered": s.get("delivered"),
            "protected": s.get("protected"),
            "delivery_window_from": s.get("delivery_from"),
            "delivery_window_to": s.get("delivery_to"),
            "tracking_url": s.get("tracking_url"),
            "events": s.get("events", []),
        }


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


class CombinedOutForDeliveryTodaySensor(SensorEntity):
    """Counts shipments currently out for delivery, across *all* providers."""

    _attr_has_entity_name = True
    _attr_name = "Heute in Zustellung"
    _attr_icon = "mdi:truck-delivery"
    _attr_native_unit_of_measurement = "Sendungen"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._attr_unique_id = f"{DOMAIN}_out_for_delivery_today"
        self._unsub_listeners: list[Callable[[], None]] = []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for coordinator in self.hass.data[DOMAIN].values():
            if not isinstance(coordinator, _SHIPMENT_COORDINATORS):
                continue
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

    def _shipments(self) -> list[dict]:
        items: list[dict] = []
        for coordinator in self.hass.data[DOMAIN].values():
            if isinstance(coordinator, _SHIPMENT_COORDINATORS):
                items.extend(_out_for_delivery(coordinator))
        return items

    @property
    def native_value(self) -> int:
        return len(self._shipments())

    @property
    def extra_state_attributes(self) -> dict:
        return {"shipments": self._shipments()}
