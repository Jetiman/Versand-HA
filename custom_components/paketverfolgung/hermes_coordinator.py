"""Coordinator for the optional myhermes.de account mode.

Kept in its own module so the existing coordinators stay untouched. The
account list gives the shipment ids; each one is then enriched through the
public Hermes tracking client (history + announced delivery window), which
is the very same parser the number-based mode uses.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    CARRIER_HERMES,
    CONF_HERMES_PASSWORD,
    CONF_HERMES_USERNAME,
    CONF_NAMES,
    DEFAULT_STATUS,
    DOMAIN,
    GROUP_DELIVERED,
    HERMES_TRACKING_PAGE_URL,
)
from .coordinator import _BaseCoordinator
from .hermes_account import HermesAccountClient, HermesAccountError, HermesAuthError
from .hermes_tracking_api import HermesTrackingApiClient, HermesTrackingApiError

_LOGGER = logging.getLogger(__name__)

# In-flight parcels are refreshed every poll; delivered ones only backfilled
# once, a few per poll, so an account with many old parcels stays cheap.
_DETAIL_FETCHES_PER_POLL = 12
# Hermes fills these in when the user gave no label / the sender is unknown.
_PLACEHOLDER_LABELS = {"Meine Sendung"}
_PLACEHOLDER_SENDERS = {"Versender"}


def merge_shipment(entry: dict, detail: dict | None) -> dict:
    """Combine a list entry with the public tracking detail (may be None)."""
    shipment_id = entry["id"]
    if detail:
        item = dict(detail)
    else:
        delivered = entry["group"] == GROUP_DELIVERED
        item = {
            "id": shipment_id,
            "carrier": CARRIER_HERMES,
            "name": shipment_id,
            "status": entry["status"] or DEFAULT_STATUS,
            "group": entry["group"],
            "direction": "receive",
            "delivery_from": None,
            "delivery_to": None,
            "tracking_url": HERMES_TRACKING_PAGE_URL.format(id=shipment_id),
            "events": [],
            "delivered": delivered,
            "protected": False,
        }
    # Account shipments are always incoming.
    item["direction"] = "receive"
    label = entry.get("description")
    sender = entry.get("sender")
    if label and label not in _PLACEHOLDER_LABELS:
        item["name"] = label
    elif (
        item.get("name") in (None, "", shipment_id)
        and sender
        and sender not in _PLACEHOLDER_SENDERS
    ):
        item["name"] = sender
    return item


class HermesAccountDataUpdateCoordinator(_BaseCoordinator):
    """Fetches every incoming parcel on a myhermes.de account."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_hermes", update_interval=update_interval
        )
        self.entry = entry
        # Own session = own cookie jar; the login cookies stay in memory.
        self.client = HermesAccountClient(async_create_clientsession(hass))
        self.tracking = HermesTrackingApiClient(async_get_clientsession(hass))
        self._logged_in = False
        self._details: dict[str, dict] = {}

    async def _login(self) -> None:
        try:
            await self.client.login(
                self.entry.data[CONF_HERMES_USERNAME],
                self.entry.data[CONF_HERMES_PASSWORD],
            )
        except HermesAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HermesAccountError as err:
            raise UpdateFailed(f"Hermes login failed: {err}") from err
        self._logged_in = True

    async def _poll(self) -> dict[str, dict]:
        self._mark_polled()
        await self._load_archive()
        if not self._logged_in:
            await self._login()

        try:
            entries = await self.client.fetch_shipments()
        except HermesAuthError:
            _LOGGER.info("Hermes session expired, logging in again")
            self._logged_in = False
            await self._login()
            try:
                entries = await self.client.fetch_shipments()
            except HermesAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except HermesAccountError as err:
                raise UpdateFailed(f"Error fetching Hermes parcels: {err}") from err
        except HermesAccountError as err:
            raise UpdateFailed(f"Error fetching Hermes parcels: {err}") from err

        names = {
            str(k).strip(): str(v).strip()
            for k, v in (self.entry.options.get(CONF_NAMES, {}) or {}).items()
            if str(v).strip()
        }
        ordered = sorted(entries, key=lambda e: e["group"] == GROUP_DELIVERED)
        budget = _DETAIL_FETCHES_PER_POLL

        result: dict[str, dict] = {}
        for entry in ordered:
            shipment_id = entry["id"]
            detail = self._details.get(shipment_id)
            wants = entry["group"] != GROUP_DELIVERED or detail is None
            if wants and budget > 0:
                budget -= 1
                fetched = await self._lookup(shipment_id)
                if fetched:
                    detail = fetched
                    self._details[shipment_id] = fetched
            item = merge_shipment(entry, detail)
            # The list is authoritative for "delivered" if it says so.
            if entry["group"] == GROUP_DELIVERED:
                item["delivered"] = True
                item["group"] = GROUP_DELIVERED
            item["forced"] = None
            item["carrier_name"] = item["name"]
            item["custom_name"] = names.get(shipment_id)
            item["name"] = names.get(shipment_id) or item["carrier_name"]
            await self._apply_archive(item)
            result[shipment_id] = item

        for stale in [k for k in self._details if k not in result]:
            self._details.pop(stale, None)
        self._apply_direction_overrides(result)
        await self._save_archive(set(result))
        self._notify_changes(result)
        _LOGGER.debug("Paketverfolgung (Hermes-Konto): %d parcel(s)", len(result))
        return result

    async def _lookup(self, shipment_id: str) -> dict | None:
        try:
            return await self.tracking.fetch(shipment_id)
        except HermesTrackingApiError as err:
            _LOGGER.debug("Hermes detail for %s failed: %s", shipment_id, err)
            return None
