"""Constants for the Paketverfolgung integration."""
from datetime import timedelta

DOMAIN = "paketverfolgung"

# Fixed values reverse-engineered from the official DHL app / login flow.
# These are public client parameters (no per-user secret), the same ones
# used by the open-source ioBroker.parcel adapter.
CLIENT_ID = "83471082-5c13-4fce-8dcb-19d2a3fca413"
CODE_VERIFIER = "zmVs5AKfGvv45a9aUvuOid9a_erOirp7XL1sn9kWT_o"
CODE_CHALLENGE = "MAhrhXXZP-Owy-R7ruyB7Fn-Z8ODW6qxCoHg4uXELCw"
BASIC_AUTH_HEADER = "Basic ODM0NzEwODItNWMxMy00ZmNlLThkY2ItMTlkMmEzZmNhNDEzOg=="
REDIRECT_URI = "dhllogin://de.deutschepost.dhl/login"

AUTH_BASE = "https://login.dhl.de/af5f9bb6-27ad-4af4-9445-008e7a5cddb8/login"
AUTHORIZE_URL = (
    f"{AUTH_BASE}/authorize?redirect_uri={REDIRECT_URI}"
    f"&client_id={CLIENT_ID}&response_type=code&scope=openid%20offline_access"
    f"&code_challenge={CODE_CHALLENGE}&code_challenge_method=S256"
    "&prompt=login&ui_locales=de-DE"
)
TOKEN_URL = f"{AUTH_BASE}/token"

SEARCH_URL = "https://www.dhl.de/int-verfolgen/data/search"
TRACKING_PAGE_URL = "https://www.dhl.de/de/privatkunden/dhl-sendungsverfolgung.html?piececode={id}"

USER_AGENT = "DHLPaket_PROD/1367 CFNetwork/1240.0.4 Darwin/20.6.0"

# CONF keys
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ID_TOKEN = "id_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_EXPIRES_AT = "expires_at"
CONF_UPDATE_INTERVAL = "update_interval_minutes"

DEFAULT_UPDATE_INTERVAL_MINUTES = 15
MIN_UPDATE_INTERVAL_MINUTES = 5
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES)

# Token is refreshed this long before it actually expires (mirrors the
# 29-minute preemptive refresh used by ioBroker.parcel for ~30min tokens).
TOKEN_REFRESH_MARGIN_SECONDS = 90

ARCHIVED_STATUS = "ARCHIVIERT"

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
