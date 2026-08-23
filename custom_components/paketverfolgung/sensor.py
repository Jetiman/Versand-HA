"""Sensor platform for Paketverfolgung (DHL).

Creates one sensor entity per tracked DHL tracking number. Entities are
added when a tracking number is configured and removed again if DHL no
longer returns data for it (e.g. it was removed from the config, or is
too old for DHL to still have data on it).
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEFAULT_ICON,
    DEFAULT_STATUS,
    DOMAIN,
    PROGRESS_ICONS,
    PROGRESS_OUT_FOR_DELIVERY,
    PROGRESS_STATUS,
    TRACKING_PAGE_URL,
)
from .coordinator import DhlDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DhlDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_ids: set[str] = set()
    entities: dict[str, "DhlShipmentSensor"] = {}

    @callback
    def _sync_entities() -> None:
        current_ids = set(coordinator.data or {})

        new_ids = current_ids - known_ids
        if new_ids:
            new_entities = [
                DhlShipmentSensor(coordinator, entry.entry_id, shipment_id)
                for shipment_id in new_ids
            ]
            for entity in new_entities:
                entities[entity.shipment_id] = entity
            known_ids.update(new_ids)
            async_add_entities(new_entities)

        removed_ids = known_ids - current_ids
        for shipment_id in removed_ids:
            entity = entities.pop(shipment_id, None)
            known_ids.discard(shipment_id)
            if entity is not None:
                hass.async_create_task(entity.async_remove())

    entry.async_on_unload(coordinator.async_add_listener(_sync_entities))
    _sync_entities()

    async_add_entities([DhlOutForDeliveryTodaySensor(coordinator, entry.entry_id)])


class DhlShipmentSensor(CoordinatorEntity[DhlDataUpdateCoordinator], SensorEntity):
    """Represents a single DHL shipment."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DhlDataUpdateCoordinator,
        entry_id: str,
        shipment_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.shipment_id = shipment_id
        self._attr_unique_id = f"{entry_id}_{shipment_id}"

    @property
    def _shipment(self) -> dict:
        return (self.coordinator.data or {}).get(self.shipment_id, {})

    @property
    def available(self) -> bool:
        return super().available and self.shipment_id in (self.coordinator.data or {})

    @property
    def name(self) -> str:
        info = self._shipment.get("sendungsinfo", {})
        return info.get("sendungsname") or f"DHL {self.shipment_id}"

    @property
    def native_value(self) -> str:
        details = self._shipment.get("sendungsdetails", {})
        verlauf = details.get("sendungsverlauf", {})
        status = verlauf.get("kurzStatus") or verlauf.get("status")
        if status:
            return status
        fortschritt = verlauf.get("fortschritt")
        return PROGRESS_STATUS.get(fortschritt, DEFAULT_STATUS)

    @property
    def icon(self) -> str:
        details = self._shipment.get("sendungsdetails", {})
        fortschritt = details.get("sendungsverlauf", {}).get("fortschritt")
        return PROGRESS_ICONS.get(fortschritt, DEFAULT_ICON)

    @property
    def extra_state_attributes(self) -> dict:
        info = self._shipment.get("sendungsinfo", {})
        details = self._shipment.get("sendungsdetails", {})
        verlauf = details.get("sendungsverlauf", {})
        zustellung = details.get("zustellung", {})
        # DHL always returns the shipment's full event history, even for
        # tracking numbers added long after the shipment was on its way -
        # expose it so past status updates aren't lost, newest first.
        events = [
            {"datum": event.get("datum"), "status": event.get("status")}
            for event in verlauf.get("events", [])
            if event.get("status")
        ]
        events.reverse()
        return {
            "tracking_id": self.shipment_id,
            "progress": verlauf.get("fortschritt"),
            "direction": info.get("sendungsrichtung"),
            "delivery_window_from": zustellung.get("zustellzeitfensterVon"),
            "delivery_window_to": zustellung.get("zustellzeitfensterBis"),
            "tracking_url": TRACKING_PAGE_URL.format(id=self.shipment_id),
            "events": events,
        }


class DhlOutForDeliveryTodaySensor(
    CoordinatorEntity[DhlDataUpdateCoordinator], SensorEntity
):
    """Counts tracked shipments DHL currently has out for delivery.

    DHL only sets the "In Zustellung" progress step on the day the
    courier actually has the parcel loaded onto the delivery vehicle, so
    this doubles as "out for delivery today".
    """

    _attr_has_entity_name = True
    _attr_name = "Heute in Zustellung"
    _attr_icon = "mdi:truck-delivery"
    _attr_native_unit_of_measurement = "Sendungen"

    def __init__(self, coordinator: DhlDataUpdateCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_out_for_delivery_today"

    @property
    def _out_for_delivery(self) -> list[dict]:
        return [
            shipment
            for shipment in (self.coordinator.data or {}).values()
            if shipment.get("sendungsdetails", {})
            .get("sendungsverlauf", {})
            .get("fortschritt")
            == PROGRESS_OUT_FOR_DELIVERY
        ]

    @property
    def native_value(self) -> int:
        return len(self._out_for_delivery)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "shipments": [
                {
                    "tracking_id": shipment["id"],
                    "status": shipment.get("sendungsdetails", {})
                    .get("sendungsverlauf", {})
                    .get("status"),
                    "tracking_url": TRACKING_PAGE_URL.format(id=shipment["id"]),
                }
                for shipment in self._out_for_delivery
            ]
        }
