"""Amazon account coordinator for Paketverfolgung."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from .amazon_api import AmazonApiError, AmazonAuthError
from .amazon_session import export_cookie_store, load_cookie_store
from .amazon_tracking import AmazonTrackingApiClient
from .const import (
    AMAZON_STOPS_UPDATE_INTERVAL,
    CONF_AMAZON_COOKIES,
    CONF_NAMES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_STATUS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    GROUP_DELIVERED,
    GROUP_OUT_FOR_DELIVERY,
    GROUP_REGISTERED,
    GROUP_TRANSIT,
)
from .coordinator import _BaseCoordinator

_LOGGER = logging.getLogger(__name__)


def _group_from_status(status: str, short_status: str | None = None) -> str:
    text = f"{short_status or ''} {status or ''}".lower()
    if any(marker in text for marker in ("zugestellt", "delivered")):
        return GROUP_DELIVERED
    if any(
        marker in text
        for marker in (
            "in zustellung",
            "wird heute zugestellt",
            "heute zugestellt",
            "out for delivery",
            "unterwegs zur zustellung",
        )
    ):
        return GROUP_OUT_FOR_DELIVERY
    if any(
        marker in text
        for marker in (
            "versandt",
            "versendet",
            "unterwegs",
            "auf dem weg",
            "shipped",
            "in transit",
            "transport",
            "verlassen",
            "hat die einrichtung",
            "einrichtung des versand",
        )
    ):
        return GROUP_TRANSIT
    return GROUP_REGISTERED


def normalize_amazon_shipment(raw: dict) -> dict:
    """Map one Amazon order/tracking result to the shared shipment shape."""
    # ``id`` is the stable key (the Amazon order number); ``tracking_id`` is
    # the carrier's own number and may be missing on a not-yet-shipped order.
    shipment_id = str(raw.get("id") or raw.get("tracking_id") or "").strip()
    tracking_id = raw.get("tracking_id") or None
    status = str(raw.get("status") or DEFAULT_STATUS).strip()
    short_status = raw.get("short_status")
    events = raw.get("events") or []
    group = _group_from_status(status, short_status)
    if not tracking_id and group != GROUP_DELIVERED:
        # No carrier tracking number yet -> the parcel hasn't been handed
        # over, so it's just "registered" regardless of the delivery
        # estimate ("Ankunft morgen").
        group = GROUP_REGISTERED
    elif tracking_id and events and group == GROUP_REGISTERED:
        # The carrier has scanned it at least once -> it is on its way,
        # even if the wording didn't match a known transit phrase.
        group = GROUP_TRANSIT
    delivered = group == GROUP_DELIVERED
    return {
        "id": shipment_id,
        "carrier": "amazon",
        "delivery_carrier": raw.get("carrier"),
        "tracking_id": tracking_id,
        "name": raw.get("name") or f"Amazon {shipment_id}",
        "status": status,
        "group": group,
        "direction": "incoming",
        "delivery_from": None,
        "delivery_to": None,
        "tracking_url": raw.get("tracking_url"),
        "events": events,
        "delivered": delivered,
        "protected": False,
        "order_id": raw.get("order_id"),
        "short_status": short_status,
        "stops_away": raw.get("stops_away"),
        "forced": None,
    }


class AmazonAccountDataUpdateCoordinator(_BaseCoordinator):
    """Fetch active deliveries from one authenticated Amazon.de account."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_amazon",
            update_interval=update_interval,
        )
        self.entry = entry

    async def _async_update_data(self) -> dict[str, dict]:
        self._mark_polled()
        await self._load_archive()
        store = self.entry.data.get(CONF_AMAZON_COOKIES)
        client = AmazonTrackingApiClient()
        if not load_cookie_store(client, store):
            await client.close()
            raise ConfigEntryAuthFailed("Amazon login required")

        try:
            shipments, _ = await client.fetch_shipments()
            refreshed = export_cookie_store(client)
        except AmazonAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AmazonApiError as err:
            raise UpdateFailed(f"Error fetching Amazon deliveries: {err}") from err
        finally:
            await client.close()

        if refreshed != store:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_AMAZON_COOKIES: refreshed},
            )

        names = {
            str(k).strip(): str(v).strip()
            for k, v in (self.entry.options.get(CONF_NAMES, {}) or {}).items()
            if str(v).strip()
        }
        result: dict[str, dict] = {}
        for raw in shipments:
            item = normalize_amazon_shipment(raw)
            shipment_id = item.get("id")
            if not shipment_id:
                continue
            item["carrier_name"] = item["name"]
            item["custom_name"] = names.get(shipment_id)
            item["name"] = names.get(shipment_id) or item["carrier_name"]
            await self._apply_archive(item)
            result[shipment_id] = item

        await self._save_archive(set(result))
        self._notify_changes(result)
        self._tune_interval(result)
        _LOGGER.debug("Paketverfolgung (Amazon): %d delivery item(s)", len(result))
        return result

    def _tune_interval(self, result: dict[str, dict]) -> None:
        """Poll once a minute while a parcel is on the van with a live
        "N Stopps entfernt" countdown, otherwise use the configured interval."""
        base = timedelta(
            minutes=self.entry.options.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
            )
        )
        live = any(
            item.get("stops_away") is not None and not item.get("delivered")
            for item in result.values()
        )
        wanted = min(AMAZON_STOPS_UPDATE_INTERVAL, base) if live else base
        if self.update_interval != wanted:
            _LOGGER.debug("Paketverfolgung (Amazon): update interval -> %s", wanted)
            self.update_interval = wanted
