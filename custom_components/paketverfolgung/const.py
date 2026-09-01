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

# --- Optional DHL account login (auto-discovery of the account's shipments) ---
# The same int-verfolgen/data/search endpoint, called WITHOUT a piececode
# but WITH `Cookie: dhli=<id_token>`, returns the shipments linked to the
# logged-in DHL account. The login is the DHL app's OAuth/PKCE flow: the
# user opens a login URL, signs in, DHL redirects to a `dhllogin://` URL,
# and that URL's `code` is exchanged for tokens. Parameters lifted from
# the iOS DHL app (package de.deutschepost.dhl) - may break if DHL rotates
# them. This whole feature is opt-in and off by default.
DHL_AUTH_BASE = "https://login.dhl.de/af5f9bb6-27ad-4af4-9445-008e7a5cddb8/login"
DHL_CLIENT_ID = "83471082-5c13-4fce-8dcb-19d2a3fca413"
DHL_REDIRECT_URI = "dhllogin://de.deutschepost.dhl/login"
DHL_LOGIN_STATE = (
    "eyJycyI6dHJ1ZSwicnYiOmZhbHNlLCJmaWQiOiJhcHAtbG9naW4tbWVoci1mb290ZXIiLCJoaWQi"
    "OiJhcHAtbG9naW4tbWVoci1oZWFkZXIiLCJycCI6ZmFsc2V9"
)
DHL_LOGIN_CLAIMS = (
    '{"id_token":{"email":null,"post_number":null,"twofa":null,'
    '"service_mask":null,"deactivate_account":null,"last_login":null,'
    '"customer_type":null,"display_name":null,'
    '"data_confirmation_required":null}}'
)
APP_USER_AGENT = "DHLPaket_PROD/1367 CFNetwork/1240.0.4 Darwin/20.6.0"

CONF_DHL_AUTO_DISCOVERY = "dhl_auto_discovery"
CONF_DHL_SESSION = "dhl_session"
CONF_DHL_REDIRECT = "dhl_redirect"

CONF_TRACKING_NUMBERS = "tracking_numbers"
CONF_UPDATE_INTERVAL = "update_interval_minutes"
# Optional {tracking_number: carrier} map that pins a number to a carrier
# when auto-detection gets it wrong.
CONF_CARRIER_OVERRIDES = "carrier_overrides"
# Optional {tracking_number: name} map - a user-given label shown instead
# of the carrier's own shipment name.
CONF_NAMES = "names"
# Optional recipient ZIP, used as a fallback for DPD parcels whose public
# tracking is postcode-protected (both on the tracking-number entry and
# the DPD-account entry).
CONF_DEFAULT_POSTCODE = "default_postcode"

# notify target (e.g. "mobile_app_galaxy_s22") to send a message to on a
# new shipment or a status change. Empty = notifications off. A
# `paketverfolgung_notification` event is also fired when a target is set.
CONF_NOTIFY_TARGET = "notify_target"
NOTIFY_OFF = "aus"  # sentinel dropdown value meaning "no notifications"
EVENT_NOTIFICATION = f"{DOMAIN}_notification"

# Amazon.de account login. Email/password/OTP are used only during the
# config flow and never persisted - only the authenticated cookie store
# is kept (in entry.data). Those cookies grant full account access, so
# treat entry.data / backups accordingly.
CONF_AMAZON_USERNAME = "amazon_username"
CONF_AMAZON_PASSWORD = "amazon_password"
CONF_AMAZON_OTP = "amazon_otp"
CONF_AMAZON_COOKIES = "amazon_cookies"

SERVICE_ADD_TRACKING_NUMBER = "add_tracking_number"
SERVICE_REMOVE_TRACKING_NUMBER = "remove_tracking_number"
SERVICE_SET_CARRIER = "set_tracking_carrier"
SERVICE_SET_NAME = "set_tracking_name"
ATTR_TRACKING_NUMBER = "tracking_number"
ATTR_CARRIER = "carrier"
ATTR_NAME = "name"

# Carrier a tracking number was detected to belong to. Kept per number in
# the tracking-number coordinator (in memory - re-detected after a
# restart, which is cheap).
CARRIER_DHL = "dhl"
CARRIER_DPD = "dpd"
CARRIER_HERMES = "hermes"
CARRIER_UNKNOWN = "unknown"
CARRIER_AUTO = "auto"  # override value meaning "go back to auto-detect"

CARRIERS = (CARRIER_DHL, CARRIER_DPD, CARRIER_HERMES)

# Custom sidebar panel (buildless web component served from ./frontend).
PANEL_URL_PATH = "paketverfolgung"
PANEL_STATIC_URL = "/paketverfolgung_static"
PANEL_TITLE = "Paketverfolgung"
PANEL_ICON = "mdi:package-variant-closed"
# Bump on every panel .js change to bust the browser cache.
PANEL_VERSION = "1.12.5"

CONF_PROVIDER = "provider"
# Historic value "dhl" kept for config-entry stability: this provider is
# now a carrier-neutral tracking-number list (DHL *and* DPD numbers, with
# the carrier auto-detected per number).
PROVIDER_NUMBERS = "dhl"
PROVIDER_DHL = PROVIDER_NUMBERS
PROVIDER_DPD = "dpd"
PROVIDER_AMAZON = "amazon"

CONF_DPD_USERNAME = "dpd_username"
CONF_DPD_PASSWORD = "dpd_password"

# DPD "Paketnavigator3" SOAP API (Android app v4.1.2, package de.dpd.mobile).
# Partner credentials are public constants baked into the app, reverse
# engineered via the open-source ioBroker.parcel adapter. Verified working
# 2026-08-27. Used for the myDPD *account* login (auto-detects all of an
# account's parcels). Tracking single numbers without an account goes
# through DPD_PLC_URL above instead.
DPD_NS = "https://cloud.dpd.com/"
DPD_SERVICE_URL = "https://api.paketnavigator.de/services/v1/Navigator3Service.asmx"
DPD_PARTNER_NAME = "Android Paketnavigator3"
DPD_PARTNER_TOKEN = "A33363237662F5945576"
DPD_PARTNER_PASSWORD = "272 WetFd2mpXrgD"
DPD_API_VERSION = 100
DPD_LANGUAGE = "de_DE"
DPD_TRACKING_PAGE_URL = "https://tracking.dpd.de/status/de_DE/parcel/{id}"

# DPD's public "parcel life cycle" JSON endpoint - the one the consumer
# tracking page (tracking.dpd.de) itself calls. Works by parcel number
# without a login; some parcels are postcode-protected and need `?zip=`.
# This gives the full scan history the SOAP account API doesn't return.
DPD_PLC_URL = "https://tracking.dpd.de/rest/plc/de_DE/{id}"

# Hermes Germany's public tracking JSON endpoint (the v2 API the
# myhermes.de "Sendungsverfolgung" page calls). Works by parcel number,
# no login, no postcode. Unofficial - schema undocumented.
HERMES_PLC_URL = "https://api.my-deliveries.de/tnt/v2/shipments/search/{id}"
HERMES_TRACKING_PAGE_URL = (
    "https://www.myhermes.de/empfangen/sendungsverfolgung/sendungsinformation/#{id}"
)

# Broad lifecycle group shared by both carriers - drives the icon and the
# combined "out for delivery" count regardless of provider.
GROUP_REGISTERED = "registered"
GROUP_TRANSIT = "transit"
GROUP_OUT_FOR_DELIVERY = "out_for_delivery"
GROUP_DELIVERED = "delivered"
GROUP_UNKNOWN = "unknown"

GROUP_ICONS = {
    GROUP_REGISTERED: "mdi:package-variant-closed",
    GROUP_TRANSIT: "mdi:truck-outline",
    GROUP_OUT_FOR_DELIVERY: "mdi:truck-delivery",
    GROUP_DELIVERED: "mdi:package-variant-closed-check",
    GROUP_UNKNOWN: "mdi:package-variant-closed-remove",
}

# DPD StatusID (string) -> lifecycle group, from the app's Constant.smali /
# observed live responses.
DPD_STATUS_GROUP = {
    "NO_TRACKINGDATA": GROUP_REGISTERED,
    "DATA_TRANSMITTED": GROUP_REGISTERED,
    "ACCEPTED": GROUP_REGISTERED,
    "START": GROUP_REGISTERED,
    "COLLECTED": GROUP_TRANSIT,
    "AT_SENDING_DEPOT": GROUP_TRANSIT,
    "ON_THE_ROAD": GROUP_TRANSIT,
    "AT_DELIVERY_DEPOT": GROUP_TRANSIT,
    "SORTED": GROUP_TRANSIT,
    "SORTED_TO_PICKUP_LOCATION": GROUP_TRANSIT,
    "PARCEL_PROCESSING": GROUP_TRANSIT,
    "OUT_FOR_DELIVERY": GROUP_OUT_FOR_DELIVERY,
    "IN_DELIVERY": GROUP_OUT_FOR_DELIVERY,
    "AT_PARCELSHOP": GROUP_OUT_FOR_DELIVERY,
    "DELIVERED": GROUP_DELIVERED,
    "PICKED_UP": GROUP_DELIVERED,
    "RETURN_TO_SENDER": GROUP_DELIVERED,
}

DEFAULT_UPDATE_INTERVAL_MINUTES = 15
MIN_UPDATE_INTERVAL_MINUTES = 5
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES)

# A delivered shipment moves into the panel's "Archiv" section this many
# hours after it was delivered (and stops being re-queried).
ARCHIVE_AFTER_HOURS = 24

# DHL "fortschritt" progress step (0-5) -> German status text + lifecycle group
PROGRESS_STATUS = {
    0: "Auftrag erfasst",
    1: "Abgeholt",
    2: "Im Zustellzentrum",
    3: "Im Zielzustellzentrum",
    4: "In Zustellung",
    5: "Zugestellt",
}
PROGRESS_GROUP = {
    0: GROUP_REGISTERED,
    1: GROUP_TRANSIT,
    2: GROUP_TRANSIT,
    3: GROUP_TRANSIT,
    4: GROUP_OUT_FOR_DELIVERY,
    5: GROUP_DELIVERED,
}
DEFAULT_STATUS = "Unbekannt"
DEFAULT_ICON = "mdi:package-variant-closed"
# Shown for a number no carrier has (yet) claimed. It stays in the list
# and keeps being re-checked every poll until the user removes it.
NO_DATA_STATUS = "In Prüfung"
