"""Coordinator for the optional Picnic account mode.

Kept in its own module so the existing coordinators stay untouched. Picnic
deliveries are normalised by ``picnic_account.normalize_delivery`` into the
shared shipment shape, so the sensors, the panel, the archive logic, the push
notifications and the user's own dashboard automations (calendar sync etc.)
treat them like any other carrier - the delivery window of the day is simply
``delivery_window_from/to``.

Polling is adaptive: the configured interval most of the time, every few
minutes around the booked slot and once a minute while the van is live.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_NAMES,
    CONF_PICNIC_COUNTRY,
    CONF_PICNIC_HISTORY_DAYS,
    CONF_PICNIC_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_PICNIC_HISTORY_DAYS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    PICNIC_DEFAULT_COUNTRY,
    PICNIC_SESSION_KWARGS,
)
from .coordinator import _BaseCoordinator
from .picnic_account import (
    PicnicAccountClient,
    PicnicAccountError,
    PicnicAuthError,
    choose_interval,
    in_history,
    normalize_delivery,
    wants_position,
)

_LOGGER = logging.getLogger(__name__)

# Order lines are only needed for the article count in the name: fetch them
# once per delivery, refresh open deliveries now and then (customers can still
# edit an order until the cut-off) and cap the calls per poll.
_DETAIL_FETCHES_PER_POLL = 6
_DETAIL_REFRESH_SECONDS = 30 * 60


class PicnicAccountDataUpdateCoordinator(_BaseCoordinator):
    """Fetches the deliveries of one Picnic customer account."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_picnic", update_interval=update_interval
        )
        self.entry = entry
        self.client = PicnicAccountClient(
            async_create_clientsession(hass, **PICNIC_SESSION_KWARGS),
            country=entry.data.get(CONF_PICNIC_COUNTRY, PICNIC_DEFAULT_COUNTRY),
            token=entry.data.get(CONF_PICNIC_TOKEN),
        )
        # id -> (fetched at (monotonic), detail)
        self._details: dict[str, tuple[float, dict | None]] = {}

    def _persist_token(self) -> None:
        """Picnic rotates the session token; keep the newest one."""
        token = self.client.token
        if token and token != self.entry.data.get(CONF_PICNIC_TOKEN):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_PICNIC_TOKEN: token}
            )

    async def _detail(self, delivery_id: str, open_: bool, budget: list[int]) -> dict | None:
        cached = self._details.get(delivery_id)
        now = time.monotonic()
        fresh = cached is not None and (
            not open_ or now - cached[0] < _DETAIL_REFRESH_SECONDS
        )
        if fresh or budget[0] <= 0:
            return cached[1] if cached else None
        budget[0] -= 1
        detail = await self.client.fetch_delivery(delivery_id)
        # Keep an older detail if this fetch failed.
        if detail is None and cached is not None:
            detail = cached[1]
        self._details[delivery_id] = (now, detail)
        return detail

    async def _poll(self) -> dict[str, dict]:
        self._mark_polled()
        await self._load_archive()
        if not self.entry.data.get(CONF_PICNIC_TOKEN):
            raise ConfigEntryAuthFailed("Picnic login required")

        now = dt_util.now()
        history_days = int(
            self.entry.options.get(CONF_PICNIC_HISTORY_DAYS, DEFAULT_PICNIC_HISTORY_DAYS)
        )
        try:
            summaries = await self.client.fetch_deliveries()
            kept = [s for s in summaries if in_history(s, now, history_days)]
            budget = [_DETAIL_FETCHES_PER_POLL]
            built: list[tuple[dict, dict | None, dict | None]] = []
            for summary in kept:
                delivery_id = str(summary.get("delivery_id") or summary.get("id") or "")
                if not delivery_id:
                    continue
                delivered = bool(
                    str(summary.get("status") or "").upper() == "COMPLETED"
                    or summary.get("delivery_time")
                )
                detail = await self._detail(delivery_id, not delivered, budget)
                position = (
                    await self.client.fetch_position(delivery_id)
                    if wants_position(summary, now)
                    else None
                )
                built.append((summary, detail, position))
        except PicnicAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PicnicAccountError as err:
            raise UpdateFailed(f"Error fetching Picnic deliveries: {err}") from err
        finally:
            self._persist_token()

        names = {
            str(k).strip(): str(v).strip()
            for k, v in (self.entry.options.get(CONF_NAMES, {}) or {}).items()
            if str(v).strip()
        }
        result: dict[str, dict] = {}
        for summary, detail, position in built:
            item = normalize_delivery(summary, detail, position, now)
            if not item:
                continue
            sid = item["id"]
            item["forced"] = None
            item["carrier_name"] = item["name"]
            item["custom_name"] = names.get(sid)
            item["name"] = names.get(sid) or item["carrier_name"]
            await self._apply_archive(item)
            result[sid] = item

        for stale in [k for k in self._details if k not in result]:
            self._details.pop(stale, None)
        self._apply_direction_overrides(result)
        await self._save_archive(set(result))
        self._notify_changes(result)
        self._tune_interval(result)
        _LOGGER.debug("Paketverfolgung (Picnic): %d delivery(ies)", len(result))
        return result

    def _tune_interval(self, result: dict[str, dict]) -> None:
        """Faster polling around the delivery, see ``choose_interval``."""
        base = timedelta(
            minutes=self.entry.options.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
            )
        )
        wanted = choose_interval(list(result.values()), dt_util.utcnow(), base)
        if self.update_interval != wanted:
            _LOGGER.debug("Paketverfolgung (Picnic): update interval -> %s", wanted)
            self.update_interval = wanted
