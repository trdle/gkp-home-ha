# Changelog

All notable changes to this integration are documented here. The section for
a version is used as the GitHub release notes when its `v*` tag is pushed.

## v0.1.0

First release of the GKP Home custom integration for Home Assistant.

**What it does**

- Signs in to the GKP Home cloud (Watts) with your own GKP account, using the
  app's login flow (authorization code + PKCE). Your password is used once and
  never stored; only the refresh token is kept.
- One `climate` entity per radiator, named after its room (for example
  `climate.kitchen`): current and target temperature (0.5 °C steps, 5–30 °C),
  HVAC modes off / heat / auto (weekly program), presets comfort / eco /
  frost protection / boost, heating action.
- Binary sensors *Online* (reported within 30 minutes) and *Heating*.
- Diagnostic sensors: rated power, firmware, last report, and (disabled by
  default) the eco / comfort / frost protection setpoints.
- One Home Assistant device per radiator (MAC, model, firmware) suggested into
  an area named after the room.
- Re-authentication flow, poll interval option (default 60 s, minimum 30 s),
  diagnostics download with tokens, secrets and account ids redacted.
- English and Swedish translations.

**Installation**

Add `https://github.com/trdle/gkp-home-ha` as a custom repository (category
*Integration*) in HACS, install *GKP Home*, restart Home Assistant and add the
integration with your GKP Home email and password. The API credentials of the
GKP Home app are built in (they can be overridden in the dialog's collapsed
*API credentials* section).

**Notes**

- Requires Home Assistant 2025.1 or newer.
- Unofficial; not affiliated with Gnosjö Klimatprodukter or Watts Water
  Technologies.
- Apple Home (through the HomeKit Bridge) gets off / heat / auto and the target
  temperature; presets are only available in Home Assistant.
