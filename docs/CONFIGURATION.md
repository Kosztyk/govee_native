# Accounts and configuration

## Choose an account connection

| Setup | Fields to enter | Purpose |
|---|---|---|
| Govee Home account | Email and password | App inventory, private app MQTT and supported app controls. |
| Developer API only | Developer API key | Devices and controls advertised by Govee's Developer API. |
| Both | Email, password and API key | Account features plus additional advertised API capabilities and fallback. |

Email and password must be entered together. At least an account login or an
API key is required. Email verification is shown when Govee challenges a new
login. Account/session data is stored in Home Assistant's integration entry;
keep Home Assistant backups private.

## Integration options

Open **Settings → Devices & services → Govee Native → Configure**.

| Option | Default | Meaning |
|---|---|---|
| Cloud polling interval | 60 seconds | Base interval, 30–600 seconds. Developer API reads may slow to preserve the budget. Private app polling and MQTT queries continue independently. |
| Developer API read budget | 9000 requests/day | Allowed range: 500–10000. Read polling accounts for requests already spent; controls retain the vendor allowance beyond this budget. |
| Private AWS IoT MQTT | On for account setup | Uses the Govee account's cloud MQTT connection for supported commands and reports; it does not require your own MQTT broker. |
| Read Android app catalogs | On for account setup | Loads available private catalogs, including executable shortcuts when supplied by the account. |
| Local LAN control | Off | Discovers and uses devices that actually respond to Govee's supported LAN protocol. |
| Official Developer API events | Off | Enables advertised API event capabilities when an API key is available. |
| Advanced private-app write methods | Off | Allows the advanced `app_request` action to call supported write routes. It does not enable a full app feature editor. |
| Existing legacy Developer API key | Off | Explicitly reuses a key already stored by a legacy `govee` entry when Native has no key of its own. |
| Direct Bluetooth control | Off | Supports H6199 and the known H1270 protocol through Home Assistant Bluetooth. Connections are reused. |
| Prefer Bluetooth | Off | Tries Bluetooth before LAN and account MQTT for commands with supported Bluetooth frames. |
| H6199 firmware protocol profile | Original / `classic` | Chooses original or updated native command formats. |
| Configure segment controls | Off | Opens a form to choose devices and individual, grouped, both, or disabled segment entities. Other devices retain their settings. |
| Set a Bluetooth address | Off | Opens an additional form to select a supported light and store its actual Bluetooth MAC address. |

Submitting options preserves existing saved Bluetooth addresses. The temporary
“Set a Bluetooth address” choice is not a persistent device-control switch.
Saving changed options reloads the integration so connection settings take effect.

## Connection selection

For supported commands, the normal order is **LAN → account MQTT → Bluetooth
→ Developer API**. With Prefer Bluetooth, it becomes **Bluetooth → LAN →
account MQTT → Developer API**. A route is skipped when it is disabled,
unavailable or does not support that command. Native H6199 controls are not
sent to an invented Developer API capability.

Account MQTT control requires a device report in the current session. A broker
connection alone does not qualify. Writes update command-derived state as
unconfirmed and matching reports confirm them in the background. A stale
command cannot retry over a newer one; power-on is never automatically resent.
Diagnostic attributes distinguish sending, assumptions and confirmation.

## H6199 Bluetooth

Enable Bluetooth on Home Assistant and use an adapter or active, connectable
proxy within range. The integration uses Home Assistant's shared discovery,
not a separate scanner. If account metadata omits the Bluetooth address, enter
the real six-byte address using the additional Bluetooth form.

An eight-byte Govee cloud ID must not be shortened or treated as a Bluetooth
address. A successful Bluetooth write remains unconfirmed until appropriate
device feedback is available.

- **Original H6199 firmware:** original captured RGB and music command formats.
- **Updated H6199 firmware:** later segmented RGB and V3 music formats.

Choose the profile that works with the physical controller. The names describe
protocol families; they do not detect firmware compatibility automatically.
The known encrypted Bluetooth session is supported; its vendor protocol
constants and upstream MIT attribution are included in the component. Direct
Bluetooth Kelvin commands are still not implemented. H1252 zone Kelvin control
uses the separate, model-gated MQTT passthrough path.

## H6199 controls and segments

Video settings include area, style and saturation. Music controls include
native mode and sensitivity. Setting a video or music option enters that mode.
Write-only combined settings use values last sent by this integration, or
defaults until first set; phone-app edits are not all read back.

Segment entities now follow advertised addressable indices. H6199 keeps its
15 regions. Configure each device as individual, grouped, both, or hidden.
Previously integration-disabled H6199 segments are enabled for an individual
mode; explicitly user-disabled entities stay disabled.

Colours and supported independent brightness persist across restarts. Black
represents segment off. H6199 does not provide independent segment brightness:
use the main light. Unsupported brightness requests raise an error before
sending anything. Segment colours remain last-command assumptions.

See [Response and segment controls](RESPONSIVENESS_AND_SEGMENTS.md) for model
corrections, independent ceiling-light zones and additional sensor support.
