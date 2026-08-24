# DHL account auto-discovery test

Test branch: `feature/dhl-account-autodiscovery`

This branch adds optional DHL account login and automatic discovery of non-archived shipments linked to the account. Manual tracking numbers continue to work and are merged with account-discovered shipment IDs.

## Test flow

1. Install the contents of `custom_components/paketverfolgung` from this branch into Home Assistant.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Paketverfolgung**.
4. If this is a new setup, enable **Sendungen automatisch aus meinem DHL-Konto erkennen**. For an existing setup, open the integration options and enable it there.
5. Open the displayed **DHL Login** link and sign in to DHL.
6. After successful login the browser will try to open a `dhllogin://...` URL. Copy the complete URL and paste it into the Home Assistant form.
7. Finish the flow and wait for the first coordinator refresh.

## Expected result

- No DHL password is stored in Home Assistant.
- Home Assistant stores the returned OAuth session including the refresh token.
- The coordinator sets the DHL `dhli` cookie from the current ID token and queries the account-linked shipment list.
- Archived shipments are ignored.
- Account shipment IDs and manually configured tracking numbers are de-duplicated and then queried through the existing shipment detail endpoint.

## Debug logging

Add temporarily to `configuration.yaml` if needed:

```yaml
logger:
  logs:
    custom_components.paketverfolgung: debug
```

Restart Home Assistant after changing logger configuration.

## Known test points

This uses an undocumented DHL endpoint and OAuth flow derived from current DHL app behavior. DHL may change either without notice. The first live test should verify:

- authorization-code exchange succeeds,
- account search returns shipment IDs,
- token refresh succeeds after the ID token approaches expiry,
- manual tracking remains functional when account auto-discovery is disabled.

Do not open an upstream pull request until these live tests pass.
