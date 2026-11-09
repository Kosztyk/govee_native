# Troubleshooting

## Govee Native is missing from Add integration

Check the installed path. The manifest must be directly inside
`custom_components/govee_native`, not inside an extra extracted repository
folder. Restart Home Assistant after copying or updating Python files. Check
the logs for dependency-install or integration-import errors.

## Options Submit does not appear to work

Check that the polling interval is between **30 and 600 seconds** and that a
firmware profile is selected. Selecting **Set a Bluetooth address** opens a
second form; select a backlight and enter a valid six-byte Bluetooth MAC to
finish it. An eight-byte cloud device ID is rejected.

The ordinary options-save path was exercised successfully during investigation,
but the reported UI Submit failure was not reproduced. If it persists, report
which field changed, the error shown, and whether the dialog stays open, spins,
or closes with the old value. Include a screenshot of the options form and the
relevant Home Assistant log entry. Do not delete/re-add the integration just to
change options.

## A command was sent but the device did not change

Look at the device's **App connection** sensor and the entity attributes.

| Detail | Meaning |
|---|---|
| `mqtt_broker_connected` | Connection to the cloud broker; it does not prove that the device received a command. |
| `mqtt_messages_received` | Messages received during this connection. |
| `commands_confirmed` | Matching device reports confirmed commands. |
| `commands_unconfirmed` | Commands lacked the expected device confirmation. |
| `last_command_transport` | Connection used for the latest command. |
| `last_command_result` | Pending, confirmed, failed or transport-specific unconfirmed result. |
| `state_assumed` | The displayed value includes an assumption or no device value is known yet. |

If MQTT receives no matching device reports, check that the device is online
in Govee Home. If Bluetooth is configured and available, try it on a compatible
H6199. The Developer API can serve as a fallback for capabilities it advertises.
Repeated MQTT confirmation failures temporarily skip that transport so another
configured connection can be tried.

## LAN is enabled but not used

LAN support depends on the device and firmware. Enabling the option alone
does not make a model support it. The device must answer discovery and provide
fresh status on the local network. Check any LAN error in diagnostics, UDP
4001–4003 reachability, network isolation and port 4002 conflicts with other
Govee software. No response does not establish a specific network fault.

## Bluetooth cannot connect

Check that Home Assistant or its connectable proxy hears the backlight, that
the entered Bluetooth address is correct, and that the controller is in range.
Check both the adapter/proxy and integration logs. Try the appropriate H6199
protocol profile if a connection succeeds but a supported command has no effect.

## The slider initially shows off

Before a device value is available, the on/off slider uses an off-position
fallback and exposes `state_assumed: true`. This is a presentation choice,
not confirmation that the hardware is off. A later command-derived value or
device report updates it. Segment colors remain assumed because independent
region status is not returned by this protocol.

## Scenes or additional controls are missing

Controls are created from device capabilities and supported model-specific
features. Refresh scenes/catalogs where available. Optional H6199 segment
lights must be enabled in the device's entity list. A feature in Govee Home
does not automatically imply a corresponding entity here.

## Collect information for an issue

Include Home Assistant and integration versions, device SKU/firmware, enabled
connections, exact steps, relevant logs and downloaded integration diagnostics.
Review files before posting. Remove account credentials, tokens and unrelated
personal details. Report problems at [GitHub Issues](https://github.com/Kosztyk/govee_native/issues).
