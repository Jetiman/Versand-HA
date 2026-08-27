"""Constants for the Paketverfolgung integration."""
from datetime import timedelta

DOMAIN = "paketverfolgung"

# Sentinel key (alongside the entry_id keys) in hass.data[DOMAIN] marking
# that the one shared "Heute in Zustellung" sensor has already been added.
COMBINED_SENSOR_ADDED_KEY = "_combined_out_for_delivery_sensor_added"

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

SERVICE_ADD_TRACKING_NUMBER = "add_tracking_number"
ATTR_TRACKING_NUMBER = "tracking_number"

# Custom sidebar panel (buildless web component served from ./frontend).
PANEL_URL_PATH = "paketverfolgung"
PANEL_STATIC_URL = "/paketverfolgung_static"
PANEL_TITLE = "Paketverfolgung"
PANEL_ICON = "mdi:package-variant-closed"
# Bump on every panel .js change to bust the browser cache.
PANEL_VERSION = "1.6.0"

CONF_PROVIDER = "provider"
PROVIDER_DHL = "dhl"
PROVIDER_DPD = "dpd"

CONF_DPD_USERNAME = "dpd_username"
CONF_DPD_PASSWORD = "dpd_password"

# DPD "Paketnavigator3" SOAP API (Android app v4.1.2, package de.dpd.mobile).
# Partner credentials are public constants baked into the app, reverse
# engineered via the open-source ioBroker.parcel adapter. Verified working
# 2026-08-27. Unlike DHL, DPD's public tracking-number-only lookup requires
# a ZIP code plus a CAPTCHA-guarded ASP.NET form - not worth automating -
# so this uses a real myDPD account login instead, same as the app does.
DPD_NS = "https://cloud.dpd.com/"
DPD_SERVICE_URL = "https://api.paketnavigator.de/services/v1/Navigator3Service.asmx"
DPD_PARTNER_NAME = "Android Paketnavigator3"
DPD_PARTNER_TOKEN = "A33363237662F5945576"
DPD_PARTNER_PASSWORD = "272 WetFd2mpXrgD"
DPD_API_VERSION = 100
DPD_LANGUAGE = "de_DE"
DPD_TRACKING_PAGE_URL = "https://tracking.dpd.de/status/de_DE/parcel/{id}"

# DPD StatusID (string) -> broad group, from the app's Constant.smali /
# observed live responses.
DPD_STATUS_GROUP = {
    "NO_TRACKINGDATA": "registered",
    "DATA_TRANSMITTED": "registered",
    "ACCEPTED": "registered",
    "START": "registered",
    "COLLECTED": "transit",
    "AT_SENDING_DEPOT": "transit",
    "ON_THE_ROAD": "transit",
    "AT_DELIVERY_DEPOT": "transit",
    "SORTED": "transit",
    "SORTED_TO_PICKUP_LOCATION": "transit",
    "PARCEL_PROCESSING": "transit",
    "OUT_FOR_DELIVERY": "out_for_delivery",
    "IN_DELIVERY": "out_for_delivery",
    "AT_PARCELSHOP": "out_for_delivery",
    "DELIVERED": "delivered",
    "PICKED_UP": "delivered",
    "RETURN_TO_SENDER": "delivered",
}
DPD_GROUP_ICONS = {
    "registered": "mdi:package-variant-closed",
    "transit": "mdi:truck-outline",
    "out_for_delivery": "mdi:truck-delivery",
    "delivered": "mdi:package-variant-closed-check",
}
DPD_OUT_FOR_DELIVERY_STATUS_IDS = {"OUT_FOR_DELIVERY", "IN_DELIVERY"}

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

# fortschritt step meaning "out for delivery" (DHL only shows this on the
# day the courier actually has the parcel loaded for delivery)
PROGRESS_OUT_FOR_DELIVERY = 4
