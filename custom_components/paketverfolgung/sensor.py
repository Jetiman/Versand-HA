"""Sensor platform for Paketverfolgung (DHL tracking numbers, DPD account).

Creates one sensor entity per tracked shipment/parcel, plus a single
"Heute in Zustellung" summary sensor shared across *all* configured
providers (not one per provider). Entities are added as items appear in
the coordinator's data and removed again once they drop out (tracking
number removed from config, DPD parcel no longer returned, ...).
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
    CONF_PROVIDER,
    DEFAULT_ICON,
    DEFAULT_STATUS,
    DOMAIN,
    DPD_GROUP_ICONS,
    DPD_OUT_FOR_DELIVERY_STATUS_IDS,
    DPD_STATUS_GROUP,
    DPD_TRACKING_PAGE_URL,
    PROGRESS_ICONS,
    PROGRESS_OUT_FOR_DELIVERY,
    PROGRESS_STATUS,
    PROVIDER_DPD,
    TRACKING_PAGE_URL,
)
from .coordinator import DhlDataUpdateCoordinator, DpdDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if entry.data.get(CONF_PROVIDER) == PROVIDER_DPD:
        _setup_dynamic_entities(
            entry, coordinator, async_add_entities, entity_cls=DpdParcelSensor
        )
    else:
        _setup_dynamic_entities(
            entry, coordinator, async_add_entities, entity_cls=DhlShipmentSensor
        )

    # One combined summary entity for all providers together, added under
    # whichever entry sets up first - guarded (by that owning entry_id, not
    # just a bool) so a second provider entry doesn't add a duplicate, and
    # so a *different* entry reloading doesn't wrongly re-trigger it while
    # the owner's copy is still alive.
    if hass.data[DOMAIN].get(COMBINED_SENSOR_ADDED_KEY) is None:
        hass.data[DOMAIN][COMBINED_SENSOR_ADDED_KEY] = entry.entry_id
        async_add_entities([CombinedOutForDeliveryTodaySensor(hass)])


def _setup_dynamic_entities(
    entry: ConfigEntry,
    coordinator,
    async_add_entities: AddEntitiesCallback,
    *,
    entity_cls: type,
) -> None:
    """Add one entity per item key in coordinator.data, kept in sync.

    Shared between DHL (shipments) and DPD (parcels): both coordinators
    expose the same `dict[id, dict]` shape, just with different item
    schemas, so the add/remove bookkeeping is identical.
    """
    known_ids: set[str] = set()
    entities: dict[str, CoordinatorEntity] = {}

    @callback
    def _sync_entities() -> None:
        current_ids = set(coordinator.data or {})

        new_ids = current_ids - known_ids
        if new_ids:
            new_entities = [
                entity_cls(coordinator, entry.entry_id, item_id) for item_id in new_ids
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


class DhlShipmentSensor(CoordinatorEntity[DhlDataUpdateCoordinator], SensorEntity):
    """Represents a single DHL shipment."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DhlDataUpdateCoordinator,
        entry_id: str,
        item_id: str,
    ) -> None:
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
        info = self._shipment.get("sendungsinfo", {})
        return info.get("sendungsname") or f"DHL {self.item_id}"

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
            "tracking_id": self.item_id,
            "progress": verlauf.get("fortschritt"),
            "direction": info.get("sendungsrichtung"),
            "delivery_window_from": zustellung.get("zustellzeitfensterVon"),
            "delivery_window_to": zustellung.get("zustellzeitfensterBis"),
            "tracking_url": TRACKING_PAGE_URL.format(id=self.item_id),
            "events": events,
        }


class DpdParcelSensor(CoordinatorEntity[DpdDataUpdateCoordinator], SensorEntity):
    """Represents a single DPD parcel (sent, received, or a return)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DpdDataUpdateCoordinator,
        entry_id: str,
        item_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.item_id = item_id
        self._attr_unique_id = f"{entry_id}_{item_id}"

    @property
    def _parcel(self) -> dict:
        return (self.coordinator.data or {}).get(self.item_id, {})

    @property
    def available(self) -> bool:
        return super().available and self.item_id in (self.coordinator.data or {})

    @property
    def name(self) -> str:
        return self._parcel.get("name") or f"DPD {self.item_id}"

    @property
    def native_value(self) -> str:
        return self._parcel.get("status") or DEFAULT_STATUS

    @property
    def icon(self) -> str:
        group = DPD_STATUS_GROUP.get(self._parcel.get("status_id"))
        return DPD_GROUP_ICONS.get(group, DEFAULT_ICON)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "tracking_id": self.item_id,
            "status_id": self._parcel.get("status_id"),
            "direction": self._parcel.get("direction"),
            "delivered": self._parcel.get("delivered"),
            "tracking_url": DPD_TRACKING_PAGE_URL.format(id=self.item_id),
        }


def _dhl_out_for_delivery(coordinator: DhlDataUpdateCoordinator) -> list[dict]:
    out = []
    for shipment in (coordinator.data or {}).values():
        verlauf = shipment.get("sendungsdetails", {}).get("sendungsverlauf", {})
        if verlauf.get("fortschritt") != PROGRESS_OUT_FOR_DELIVERY:
            continue
        out.append(
            {
                "tracking_id": shipment["id"],
                "status": verlauf.get("status"),
                "tracking_url": TRACKING_PAGE_URL.format(id=shipment["id"]),
                "provider": "DHL",
            }
        )
    return out


def _dpd_out_for_delivery(coordinator: DpdDataUpdateCoordinator) -> list[dict]:
    out = []
    for parcel in (coordinator.data or {}).values():
        if parcel.get("status_id") not in DPD_OUT_FOR_DELIVERY_STATUS_IDS:
            continue
        out.append(
            {
                "tracking_id": parcel["id"],
                "status": parcel.get("status"),
                "tracking_url": DPD_TRACKING_PAGE_URL.format(id=parcel["id"]),
                "provider": "DPD",
            }
        )
    return out


class CombinedOutForDeliveryTodaySensor(SensorEntity):
    """Counts shipments/parcels currently out for delivery, across *all*
    configured providers (DHL + DPD) combined into a single sensor.

    Not a CoordinatorEntity since it isn't tied to one coordinator - it
    manually subscribes to every coordinator present in hass.data[DOMAIN]
    at the time it's added, and recomputes across all of them on any of
    their updates.
    """

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
            if not isinstance(
                coordinator, (DhlDataUpdateCoordinator, DpdDataUpdateCoordinator)
            ):
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

    def _out_for_delivery(self) -> list[dict]:
        items: list[dict] = []
        for coordinator in self.hass.data[DOMAIN].values():
            if isinstance(coordinator, DhlDataUpdateCoordinator):
                items.extend(_dhl_out_for_delivery(coordinator))
            elif isinstance(coordinator, DpdDataUpdateCoordinator):
                items.extend(_dpd_out_for_delivery(coordinator))
        return items

    @property
    def native_value(self) -> int:
        return len(self._out_for_delivery())

    @property
    def extra_state_attributes(self) -> dict:
        return {"shipments": self._out_for_delivery()}
