"""Constants for the Paketverfolgung integration."""
from datetime import timedelta

DOMAIN = "paketverfolgung"

# DHL's public shipment-tracking search. Works fully anonymously for a
# known tracking number (piececode) - no login required. Verified against
# the real endpoint: https://www.dhl.de/int-verfolgen/data/search
SEARCH_URL = "https://www.dhl.de/int-verfolgen/data/search"
TRACKING_PAGE_URL = "https://www.dhl.de/de/privatkunden/dhl-sendungsverfolgung.html?piececode={id}"

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_8 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

CONF_TRACKING_NUMBERS = "tracking_numbers"
CONF_UPDATE_INTERVAL = "update_interval_minutes"

DEFAULT_UPDATE_INTERVAL_MINUTES = 15
MIN_UPDATE_INTERVAL_MINUTES = 5
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES)

# "fortschritt" progress step (0-5) -> German status text + icon
PROGRESS_STATUS = {
    0: "Auftrag erfasst",
    1: "Abgeholt",
    2: "Im Zustellzentrum",
    3: "Im Zielzustellzentrum",
    4: "In Zustellung",
    5: "Zugestellt",
}
PROGRESS_ICONS = {
    0: "mdi:package-variant-closed",
    1: "mdi:package-variant-closed",
    2: "mdi:truck-outline",
    3: "mdi:truck-outline",
    4: "mdi:truck-delivery",
    5: "mdi:package-variant-closed-check",
}
DEFAULT_STATUS = "Unbekannt"
DEFAULT_ICON = "mdi:package-variant-closed"
