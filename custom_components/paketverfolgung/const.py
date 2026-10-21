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

# --- DHL account login (auto-discovery of the account's shipments) ---
# The same int-verfolgen/data/search endpoint, called WITHOUT a piececode
# but WITH `Cookie: dhli=<id_token>`, returns the shipments linked to the
# logged-in DHL account. The login is the DHL app's OAuth/PKCE flow: the
# user opens a login URL, signs in, DHL redirects to a `dhllogin://` URL,
# and that URL's `code` is exchanged for tokens. Parameters lifted from
# the iOS DHL app (package de.deutschepost.dhl) - may break if DHL rotates
# them. Opt-in: either as its own "DHL-Konto" entry (recommended) or - as
# before - as a switch on the "Sendungsnummern" entry.
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

# Notifications: when enabled, a message is sent to each configured notify
# target (e.g. "mobile_app_galaxy_s22") on a new shipment or a status
# change; a `paketverfolgung_notification` event is also fired. These live
# in the config entry options and are kept in sync across all entries by
# the `set_notifications` service, so the panel can toggle them globally.
CONF_NOTIFY_ENABLED = "notify_enabled"
CONF_NOTIFY_TARGETS = "notify_targets"
# The two notification triggers are independent switches, both defaulting
# to on: a brand-new shipment being detected, and any later status change.
# Turning one off must not silently affect the other.
CONF_NOTIFY_ON_NEW = "notify_on_new"
CONF_NOTIFY_ON_STATUS_CHANGE = "notify_on_status_change"
# When set, restricts the *status-change* trigger above to just the moment
# a shipment reaches "out for delivery" (or "delivered") - no message for
# every intermediate scan. Has no effect on the new-shipment trigger.
CONF_NOTIFY_OUT_FOR_DELIVERY_ONLY = "notify_out_for_delivery_only"
# When set, the shipment name in the notification body is trimmed (long
# Amazon product titles etc.).
CONF_NOTIFY_SHORT_NAME = "notify_short_name"
EVENT_NOTIFICATION = f"{DOMAIN}_notification"
SERVICE_SET_NOTIFICATIONS = "set_notifications"
SERVICE_TEST_NOTIFICATION = "test_notification"

# Dispatcher signal fired after every coordinator refresh (success or not).
# The cross-provider summary sensors listen on this instead of subscribing
# to each coordinator individually - the per-coordinator approach missed
# the DPD-account and Amazon coordinators when they finished their first
# refresh after the summary sensors were already added (parallel entry
# setup), leaving "Nächste Aktualisierung" stuck on the numbers coordinator.
SIGNAL_COORDINATOR_UPDATED = f"{DOMAIN}_coordinator_updated"

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
SERVICE_SET_DIRECTION = "set_tracking_direction"
SERVICE_ARCHIVE_NOW = "archive_now"
SERVICE_UNARCHIVE = "unarchive"
ATTR_TRACKING_NUMBER = "tracking_number"
ATTR_CARRIER = "carrier"
ATTR_NAME = "name"
ATTR_DIRECTION = "direction"

# Sender/recipient direction, shown as "Richtung" in the panel. Anonymous
# carrier tracking (a plain number, no account) can't tell whether *you*
# are the sender or the recipient - DHL's public tracking always reports
# "ANKOMMEND" - so a shipment can be pinned per number/id. "auto" clears it.
CONF_DIRECTION_OVERRIDES = "direction_overrides"
DIRECTION_SEND = "send"
DIRECTION_RECEIVE = "receive"
DIRECTION_AUTO = "auto"
DIRECTIONS = (DIRECTION_SEND, DIRECTION_RECEIVE)

# Per-id flag (dict keyed by tracking number/parcel id -> True) letting the
# user move a delivered shipment into the archive right away instead of
# waiting for the ARCHIVE_AFTER_HOURS grace period.
CONF_MANUAL_ARCHIVE = "manual_archive"

# Carrier a tracking number was detected to belong to. Kept per number in
# the tracking-number coordinator (in memory - re-detected after a
# restart, which is cheap).
CARRIER_DHL = "dhl"
CARRIER_DPD = "dpd"
CARRIER_HERMES = "hermes"
CARRIER_UPS = "ups"
CARRIER_UNKNOWN = "unknown"
CARRIER_AUTO = "auto"  # override value meaning "go back to auto-detect"

CARRIERS = (CARRIER_DHL, CARRIER_DPD, CARRIER_HERMES, CARRIER_UPS)

# Shown in the panel's settings footer so a build (esp. a beta) is
# identifiable at a glance. INTEGRATION_VERSION carries the full label
# incl. any beta suffix; INTEGRATION_COMMIT is stamped by the release
# commit (see the release flow) - "0000000" on an unreleased working tree.
INTEGRATION_VERSION = "1.18.2"
INTEGRATION_COMMIT = "d20c225"

# Custom sidebar panel (buildless web component served from ./frontend).
PANEL_URL_PATH = "paketverfolgung"
PANEL_STATIC_URL = "/paketverfolgung_static"
PANEL_TITLE = "Paketverfolgung"
PANEL_ICON = "mdi:package-variant-closed"
# Query-param on the panel .js URL - busts the browser cache. Tied to the
# build label so every release (betas included) reloads the panel.
PANEL_VERSION = INTEGRATION_VERSION

CONF_PROVIDER = "provider"
# Historic value "dhl" kept for config-entry stability: this provider is
# now a carrier-neutral tracking-number list (DHL *and* DPD numbers, with
# the carrier auto-detected per number).
PROVIDER_NUMBERS = "dhl"
PROVIDER_DHL = PROVIDER_NUMBERS
PROVIDER_DPD = "dpd"
PROVIDER_AMAZON = "amazon"
PROVIDER_HERMES = "hermes"
# Dedicated DHL-account entry (the provider id "dhl" is taken by the
# tracking-number list, see above).
PROVIDER_DHL_ACCOUNT = "dhl_account"
# Picnic grocery delivery account (see picnic_account.py).
PROVIDER_PICNIC = "picnic"

CONF_DPD_USERNAME = "dpd_username"
CONF_DPD_PASSWORD = "dpd_password"
CONF_HERMES_USERNAME = "hermes_username"
CONF_HERMES_PASSWORD = "hermes_password"

# myDPD *web account* (Germany), used for the "DPD-Konto" entry. DPD retired
# its app SOAP API for third parties (it now needs a Play Integrity token) and
# the public tracking page in 2026, so the account is read the way the website
# does it: login on dpd.com -> portal.dpd.de -> parcel list on my.dpd.de.
# See dpd_api.py. my.dpd.de resets connections from non-browser user agents.
DPD_LOGIN_URL = "https://www.dpd.com/de/de/mydpd-anmelden-und-registrieren/?lng=DEU"
DPD_PORTAL_HOME_URL = "https://portal.dpd.de/home_pn.aspx"
DPD_INBOX_URL = "https://my.dpd.de/myParcel.aspx"
DPD_WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
# Link to a parcel on DPD's page (asks for the postcode / CAPTCHA there).
DPD_TRACKING_PAGE_URL = "https://my.dpd.de/redirect.aspx?action=12&parcelno={id}"

# DPD's old public "parcel life cycle" JSON endpoint (tracking.dpd.de). DPD
# switched the classic tracking off in April 2026 and the path no longer
# answers reliably, so looking up a bare DPD *number* may fail; parcels of a
# myDPD account are read through the account instead (see above).
DPD_PLC_URL = "https://tracking.dpd.de/rest/plc/de_DE/{id}"

# Hermes Germany's public tracking JSON endpoint (the v2 API the
# myhermes.de "Sendungsverfolgung" page calls). Works by parcel number,
# no login, no postcode. Unofficial - schema undocumented.
# myhermes.de account (login + "Empfangsübersicht"), see hermes_account.py.
HERMES_ACCOUNT_BASE = "https://www.myhermes.de"
HERMES_PLC_URL = "https://api.my-deliveries.de/tnt/v2/shipments/search/{id}"
HERMES_TRACKING_PAGE_URL = (
    "https://www.myhermes.de/empfangen/sendungsverfolgung/sendungsinformation/#{id}"
)

# UPS's account-less tracking endpoint (the one ups.com/track's own page
# calls). A GET to the API host seeds the session cookies incl. a CSRF
# cookie; the POST then needs that cookie echoed as a header plus a
# Chrome-consistent header set (see ups_tracking_api.py). Unofficial and
# undocumented. UPS grants only a few lookups per internet connection - see
# the request budget below.
UPS_TRACKING_API_URL = "https://webapis.ups.com/track/api/Track/GetStatus?loc={locale}"
UPS_TRACKING_PAGE_URL = "https://www.ups.com/track?loc=de_DE&tracknum={id}"
UPS_LOCALE = "de_DE"
UPS_COOKIE_XSRF = "X-XSRF-TOKEN-ST"
UPS_HEADER_XSRF = "X-XSRF-TOKEN"
# A UPS 1Z number: "1Z" + 6-char shipper + 2-digit service + 8 digits (18).
UPS_NUMBER_PATTERN = r"^1Z[0-9A-Z]{16}$"
# UPS answers an unvalidated session only a handful of times and then goes
# silent for hours. Measured by the ha-ups project (MIT): 3 requests, then
# roughly one per 75 minutes. Modelled as a persisted token bucket shared by
# all UPS numbers; a request without a token is never sent.
UPS_BUDGET_CAPACITY = 3
UPS_BUDGET_REFILL_SECONDS = 4500
# After any failed UPS request (a hang, a block) no further request is sent
# for this long. Asking again too early prolongs UPS's silence, so retrying
# on every poll would only make it worse.
UPS_STAND_DOWN_SECONDS = 7200

# --- Picnic (grocery delivery) account -------------------------------------
# Picnic has no public tracking page; the app talks to a JSON API at
# storefront-prod.<country>.picnicinternational.com. Login is e-mail +
# MD5(password) (+ optional SMS two-factor); afterwards only the auth token
# (``x-picnic-auth``) is kept - the password is never stored. Reverse
# engineered by the community (python-picnic-api2, used by Home Assistant's
# own "picnic" integration). Unofficial and undocumented - may change.
CARRIER_PICNIC = "picnic"
CONF_PICNIC_USERNAME = "picnic_username"
CONF_PICNIC_PASSWORD = "picnic_password"  # only used inside the config flow
CONF_PICNIC_COUNTRY = "picnic_country"
CONF_PICNIC_TOKEN = "picnic_token"
CONF_PICNIC_CODE = "picnic_code"
# How many days a delivered order stays listed (it moves into the archive
# 24 h after delivery, see ARCHIVE_AFTER_HOURS). 0 = only open deliveries.
CONF_PICNIC_HISTORY_DAYS = "picnic_history_days"
DEFAULT_PICNIC_HISTORY_DAYS = 2
MAX_PICNIC_HISTORY_DAYS = 90
PICNIC_COUNTRIES = ("DE", "NL", "FR")
PICNIC_DEFAULT_COUNTRY = "DE"
PICNIC_API_URL = "https://storefront-prod.{country}.picnicinternational.com/api/{version}"
PICNIC_API_VERSION = "15"
PICNIC_CLIENT_ID = 30100
PICNIC_AGENT = "30100;1.206.1-#15408"
PICNIC_DEVICE_ID = "598F770380CA54B6"
PICNIC_USER_AGENT = "okhttp/3.12.2"
# The delivery ETA is shown rounded outwards to this many minutes so the
# "Lieferungen" calendar entry is only rewritten when the window really moves.
# Picnic's session token (a JWT with its feature flags) can exceed aiohttp's
# default 8190-byte header limit, so the client session needs more room.
PICNIC_SESSION_KWARGS = {"max_line_size": 65536, "max_field_size": 65536}
PICNIC_ETA_ROUND_MINUTES = 5
# Polling cadence while a delivery is close (see picnic_account.choose_interval).
PICNIC_LIVE_INTERVAL = timedelta(minutes=1)
PICNIC_SLOT_INTERVAL = timedelta(minutes=3)

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

# While an Amazon parcel is on the delivery van and Amazon is showing a live
# "N Stopps entfernt" countdown, the Amazon coordinator polls this often
# instead of the configured interval, so the stop count stays current.
AMAZON_STOPS_UPDATE_INTERVAL = timedelta(minutes=1)

# Upper bound for one whole poll. Every HTTP call has its own timeout, but
# if anything in a poll ever hangs, the coordinator would never schedule
# another refresh and nothing would be logged. This aborts such a poll with
# a visible error so the next one still runs.
POLL_WATCHDOG_SECONDS = 900

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
