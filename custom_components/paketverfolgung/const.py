"""Constants for the Paketverfolgung integration."""
from datetime import timedelta

DOMAIN = "paketverfolgung"

SEARCH_URL = "https://www.dhl.de/int-verfolgen/data/search"
DHL_AUTH_BASE = "https://login.dhl.de/af5f9bb6-27ad-4af4-9445-008e7a5cddb8/login"
DHL_CLIENT_ID = "83471082-5c13-4fce-8dcb-19d2a3fca413"
DHL_REDIRECT_URI = "dhllogin://de.deutschepost.dhl/login"
# DHL's mobile-app OAuth client currently expects this PKCE pair. Keep this in sync
# with the login URL used by the official-app compatible browser flow.
DHL_CODE_VERIFIER = "zmVs5AKfGvv45a9aUvuOid9a_erOirp7XL1sn9kWT_o"
DHL_CODE_CHALLENGE = "MAhrhXXZP-Owy-R7ruyB7Fn-Z8ODW6qxCoHg4uXELCw"
DHL_LOGIN_STATE = "eyJycyI6dHJ1ZSwicnYiOmZhbHNlLCJmaWQiOiJhcHAtbG9naW4tbWVoci1mb290ZXIiLCJoaWQiOiJhcHAtbG9naW4tbWVoci1oZWFkZXIiLCJycCI6ZmFsc2V9"
DHL_LOGIN_CLAIMS = '{"id_token":{"email":null,"post_number":null,"twofa":null,"service_mask":null,"deactivate_account":null,"last_login":null,"customer_type":null,"display_name":null,"data_confirmation_required":null}}'
TRACKING_PAGE_URL = "https://www.dhl.de/de/privatkunden/dhl-sendungsverfolgung.html?piececode={id}"

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_8 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)
APP_USER_AGENT = "DHLPaket_PROD/1367 CFNetwork/1240.0.4 Darwin/20.6.0"

CONF_TRACKING_NUMBERS = "tracking_numbers"
CONF_UPDATE_INTERVAL = "update_interval_minutes"
CONF_DHL_SESSION = "dhl_session"
CONF_AUTO_DISCOVERY = "auto_discovery"
CONF_DHL_REDIRECT = "dhl_redirect"

SERVICE_ADD_TRACKING_NUMBER = "add_tracking_number"
ATTR_TRACKING_NUMBER = "tracking_number"

DEFAULT_UPDATE_INTERVAL_MINUTES = 15
MIN_UPDATE_INTERVAL_MINUTES = 5
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES)

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
PROGRESS_OUT_FOR_DELIVERY = 4
