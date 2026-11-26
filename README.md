# Govee Native

[![HACS](https://github.com/Kosztyk/govee_native/actions/workflows/hacs.yaml/badge.svg)](https://github.com/Kosztyk/govee_native/actions/workflows/hacs.yaml)
[![Hassfest](https://github.com/Kosztyk/govee_native/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/Kosztyk/govee_native/actions/workflows/hassfest.yaml)
[![Tests](https://github.com/Kosztyk/govee_native/actions/workflows/tests.yaml/badge.svg)](https://github.com/Kosztyk/govee_native/actions/workflows/tests.yaml)

Control Govee devices from Home Assistant using a Govee Home account, the
Developer API, and supported local connections.

**Version:** [0.5.3](https://github.com/Kosztyk/govee_native/releases/tag/v0.5.3) · **Home Assistant:** 2026.11.0 or newer ·
**Integration domain:** `govee_native`

This is a community custom integration. It discovers the capabilities available
for your devices and creates the matching Home Assistant controls and sensors.
You can use those entities in dashboards, scenes, scripts and automations.

## What it does

| Feature | What you get |
|---|---|
| Lights | Power, brightness, RGB, supported white temperature and advertised scenes. |
| Govee Home account | Device discovery, private app MQTT state updates and supported app controls. |
| Optional Developer API | Additional advertised devices, capabilities, scene catalogs and cloud fallback. |
| Supported LAN devices | Local discovery, control and status without sending each command through the cloud. |
| H6199 backlights | Direct Bluetooth, Color/Video/Music modes, video settings, music sensitivity, Gradient and DreamView/video toggles. |
| RGBIC segments | Automatic capability-based segment lights, grouped controls, saved colours, and per-device display options. H6199 keeps its 15 region identities. |
| Other device families | Switches, fans, humidifiers, heaters, sensors and events when their advertised capabilities support them. |
| Responsive controls | Immediate command-derived UI feedback, background confirmation, and replacement of obsolete queued slider values. |
| Bluetooth | Reused connections on supported H6199/H1270 controllers, including the known encrypted protocol. |
| Extra model features | Independent ceiling-light zones, H5160/H5161 outlets, H5086 energy/power, H7152 pump/ambient readings, H5106 air quality and supported thermometer/probe readings. |
| Diagnostics | Transport health, device feedback, confirmation, query quarantine and API budget information. |

On/off controls keep the same slider in both states. Before any value is known,
the slider starts visually off and `state_assumed` remains true. A successful
command submission is distinguished from actual device confirmation.

Selections and numerical settings are saved, including H6199 Video Area,
Video Style, Video Saturation and music controls. They survive integration
reloads and Home Assistant restarts. Empty readbacks keep the last value;
valid device feedback updates it. Restored settings remain marked as assumed.
The Scene and DIY selector displays the last successfully selected action.
Since 0.5.3, device rediscovery keeps the scene IDs loaded from the Developer
API catalogs. Use **Refresh scenes** to pick up added, renamed or deleted DIYs.

Device and firmware support varies. This integration does not reproduce every
feature of the Govee Home app. See [feature coverage](docs/FEATURE_MATRIX.md).

## Before installing

- Use Home Assistant **2026.11.0 or newer**.
- Have either your **Govee Home email and password**, a **Govee Developer API key**,
  or both. A Developer API key is optional when using your Govee Home account.
- Add your devices to Govee Home first.
- For H6199 Bluetooth, Home Assistant needs a working Bluetooth adapter or
  connectable Bluetooth proxy in range of the backlight.

## Install through Home Assistant Marketplace

1. Open **Settings → Marketplace**.
2. Search for **Govee Native**, open it, and select **Install**.
3. Restart Home Assistant, then follow the setup steps below.

If it has not appeared in your catalog yet, use **Add from link** with
`https://github.com/Kosztyk/govee_native`. Connect GitHub if prompted.
See [Installation](docs/INSTALLATION.md) for details.

## Install manually from GitHub

1. Open [Releases](https://github.com/Kosztyk/govee_native/releases) and download
   **Source code (zip)** for the version you want.
2. Extract the ZIP.
3. Copy **only** `custom_components/govee_native` into your Home Assistant
   configuration directory. The installed manifest must be at:
   `/config/custom_components/govee_native/manifest.json`
4. Restart Home Assistant, then follow the setup steps below.

Do not copy the whole repository into `custom_components`, and do not nest a
second `govee_native` folder inside the first one.

Full instructions, updates, rollback and migration are in
[Installation](docs/INSTALLATION.md).

## Set up your account

1. Open **Settings → Devices & services → Add integration**.
2. Search for **Govee Native**.
3. Enter your Govee Home email and password. Leave the Developer API key blank
   unless you want to use it too. For API-only setup, leave email/password blank
   and enter your Developer API key.
4. If Govee asks for email verification, enter the code sent to your email.
5. Finish setup and open the discovered device to see its controls.

To change connection settings, select **Configure** on the integration entry.
Start with the defaults and enable optional connections as needed.

For supported local Bluetooth, enable **direct Bluetooth control**. If no Bluetooth
address was discovered, select **Set a Bluetooth address for a supported light**, choose
the backlight and enter its real six-byte MAC address. Its cloud device ID is
not necessarily its Bluetooth address.

To choose individual segment lights, a grouped light, both, or neither, open
**Configure → Configure segment controls for devices**. Saved colours survive
restarts. H6199 brightness remains a whole-light control.

Existing device and entity IDs are retained; do not remove and re-add the
integration. Explicitly user-disabled segment entities remain disabled.

All options, firmware profiles and connection priorities are explained in
[Configuration](docs/CONFIGURATION.md).

## Documentation

| Guide | Contents |
|---|---|
| [Installation](docs/INSTALLATION.md) | Marketplace, GitHub ZIP, updates, rollback and existing integrations. |
| [Configuration](docs/CONFIGURATION.md) | Accounts, options, local connections and H6199 profiles. |
| [Response and segment controls](docs/RESPONSIVENESS_AND_SEGMENTS.md) | Sending, confirmation, model support and upgrade behavior. |
| [Feature coverage](docs/FEATURE_MATRIX.md) | Implemented controls and model-specific limitations. |
| [Actions](docs/ACTIONS.md) | Additional `govee_native` actions and automation use. |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Setup, Submit, unavailable devices and unconfirmed commands. |
| [Testing](docs/TESTING.md) | Test environment, commands, results and hardware validation limits. |
| [Protocol notes](docs/PROTOCOL_NOTES.md) | Implementation references and reverse-engineering scope. |
| [Changelog](CHANGELOG.md) | Version history. |
| [HACS submission](docs/HACS_SUBMISSION.md) | Repository requirements and validation evidence for a catalog PR. |

## How it was tested

Version 0.5.3 passed **189 tests** against Home Assistant **2026.11.0** on Python
**3.14.10**. Five new cases cover DIY activation after rediscovery, stale or
missing discovery entries, and catalog refreshes. Runtime lint and Python
compilation also passed.

HTTP, MQTT and Bluetooth device boundaries are simulated in these tests. They
do not establish physical compatibility for every device or firmware. The
[testing guide](docs/TESTING.md) explains the checks and a practical device test.

## Get help

Open an [issue](https://github.com/Kosztyk/govee_native/issues) with your Home
Assistant version, integration version, device model, connection type and the
exact error. Include diagnostics and relevant logs after reviewing them for
personal information.
