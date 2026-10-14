# GKP Home for Home Assistant

A Home Assistant custom integration for **GKP WiFi radiators** (Gnosjö
Klimatprodukter) that are controlled through the **GKP Home** app. It signs in
to the GKP Home cloud with your own GKP account and exposes every radiator as a
`climate` entity, plus a few diagnostic sensors.

> **Unofficial.** This project is not affiliated with, endorsed by or supported
> by Gnosjö Klimatprodukter AB or Watts Water Technologies (which runs the GKP
> Home cloud). It was built by reverse-engineering the GKP Home Android app.
> Use it at your own risk; the cloud API can change at any time.

## What you get

- One **climate** entity per radiator, named after its room (`Kitchen`, entity
  id `climate.kitchen`):
  - current room temperature and target temperature (0.5 °C steps, 5–30 °C)
  - HVAC modes **off**, **heat** (manual) and **auto** (the radiator's weekly program)
  - presets **comfort**, **eco**, **frost protection** and **boost**
  - heating / idle action
- **Binary sensors**: *Online* (the radiator has reported within the last
  30 minutes) and *Heating* (the heating element is on).
- **Diagnostic sensors**: rated power, firmware version, last report time and
  (disabled by default) the eco, comfort and frost protection setpoints.
- One Home Assistant **device** per radiator with its WiFi MAC address, model
  and firmware, pre-assigned to an area named after the room.
- **Diagnostics download** with all tokens, secrets and account ids redacted.

## Requirements

- Home Assistant 2025.1 or newer.
- A GKP Home account that already has the radiators set up in the GKP Home app.
- Nothing else. The cloud only talks to clients that present the GKP Home app's
  own API credentials (an OAuth client secret and an API subscription key).
  Every install of the app carries the same two values, and this integration
  has them built in. If Watts ever changes them, an updated release is needed;
  until then you can enter new values yourself in the collapsed *API
  credentials (advanced)* section of the setup dialog.

## Installation

### HACS (recommended)

1. In HACS open **Integrations**, then the three-dot menu, then
   **Custom repositories**.
2. Add `https://github.com/trdle/gkp-home-ha` with category **Integration**.
3. Install **GKP Home** and restart Home Assistant.

### Manual

Copy the `custom_components/gkp_home` folder into the `custom_components`
folder of your Home Assistant configuration directory and restart Home
Assistant.

## Configuration

1. Go to **Settings → Devices & services → Add integration** and search for
   **GKP Home**.
2. Enter the **email** and **password** of your GKP Home account. The password
   is used once to sign in and is **not stored**.
3. The integration signs in, checks that the API accepts the built-in
   credentials and creates one device per radiator.

The dialog also has a collapsed **API credentials (advanced)** section with
optional *Client secret* and *Subscription key* fields that override the
built-in values. Leave them empty unless you know you need them.

### Options

Open the integration's **Configure** dialog to change the **poll interval**
(default 60 seconds, minimum 30). Every poll reads all radiators from the cloud.

## How modes map

| GKP Home mode              | Climate entity                   |
| -------------------------- | -------------------------------- |
| Off                        | HVAC mode `off`                  |
| Program (weekly schedule)  | HVAC mode `auto`                 |
| Comfort                    | HVAC mode `heat`, preset `comfort` |
| Eco                        | HVAC mode `heat`, preset `eco`   |
| Frost protection           | HVAC mode `heat`, preset `frost_protection` |
| Boost                      | HVAC mode `heat`, preset `boost` |

- Setting HVAC mode **heat** switches the radiator to **comfort**.
- Changing the **target temperature** writes the setpoint of the current mode
  (comfort, eco, frost protection or boost). When the radiator is **off** or in
  **auto**, it is switched to **comfort** with the new setpoint, which is how a
  thermostat behaves in Apple Home: changing the target turns the heating on.
- **Boost** uses the duration the radiator reports (`Bt`), or 60 minutes if it
  reports none.
- The **heating** action comes from the radiator's heating flag. Radiators that
  do not report it fall back to a heuristic: heating while the room is more
  than 0.2 °C below the setpoint.
- After every command the state is updated immediately and re-read from the
  cloud about three seconds later.

## Apple Home (HomeKit)

The integration does not talk to HomeKit itself. If your Home Assistant already
runs the **HomeKit Bridge** with the `climate` domain included, the radiators
appear in Apple Home as thermostats named after their rooms. Apple Home shows
and controls **off / heat / auto** and the **target temperature**; the presets
(comfort, eco, frost protection, boost) are only available in Home Assistant.

## Privacy

- Your password is sent to the GKP identity server once during setup (or
  re-authentication) and is never stored.
- Only the **refresh token** is kept (plus any API credentials you entered
  yourself in the advanced section) in Home Assistant's config entry storage
  (`.storage/core.config_entries`). That file is not encrypted, so protect your
  configuration directory and backups like any other secrets.
- The diagnostics download redacts tokens, secrets, your email, your user id
  and all installation ids.
- Like the GKP Home app, the integration tells the cloud once per poll that a
  user is watching each installation (`userconnected`), which is what makes the
  radiators report fresh values.

## Troubleshooting

- **"Invalid authentication"** during setup: check the email and password in
  the GKP Home app first. If signing in suddenly fails for everyone after an
  app update, the app's API credentials may have changed: update the
  integration, or enter the new values in the *API credentials (advanced)*
  section of the dialog. A rejected
  subscription key is reported as invalid authentication as well, because the
  cloud answers every API call with *401 Unauthorized*.
- **Re-authentication required**: when the cloud rejects the saved refresh
  token (for example after a password change), Home Assistant asks for your
  password again on the integration page. Nothing else needs to be re-entered.
- **Radiator shows as offline**: the *Online* binary sensor turns off when the
  radiator's own timestamp is older than 30 minutes. Check the radiator's WiFi
  connection and whether it is online in the GKP Home app.
- **Radiators added or removed in the app**: reload the integration (or restart
  Home Assistant) to pick up new radiators; removed radiators become
  unavailable until you delete their device.
- **Something else**: download the **diagnostics** from the integration page
  (three-dot menu → *Download diagnostics*) and open an issue. Enable debug
  logging for `custom_components.gkp_home` to see every API call (URLs and
  status codes only, never tokens).

## Development

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```

The tests run against a mocked cloud and an in-process fake identity server;
no GKP account is needed. The real login flow and the write commands could only
be verified by signing in with an actual account.

## License

[MIT](LICENSE)
