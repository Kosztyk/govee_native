# Response, segments and model support in 0.5.0

## What changes when you click a control

A command uses a responsive, compatible transport. The UI can show the
requested value as soon as sending succeeds. `state_assumed` and
`last_command_result` distinguish that display from an actual device report.
Confirmation runs in the background, independently for each light property;
it no longer holds the device's command lock for eight seconds.

Rapid slider changes replace obsolete values waiting to be sent. Power
commands are not coalesced. A newer colour, mode or power command invalidates
older confirmation work so an old retry cannot overwrite it. An automatic
retry is restricted to a light whose fresh report still shows the previous
value; silence does not authorize a resend, and power-on is never retried.

The integration learns MQTT response health per device and per connection.
Being connected to the broker does not qualify an unheard device for control.
Known firmware that refuses status queries is excluded. Repeated query-related
disconnects quarantine that model until reload. A completely silent session
gets one automatic restart before the issue remains visible in diagnostics.

LAN applies immediate command-derived UI state, then verifies readback with a
short window. Three readback failures temporarily suppress LAN writes for five
minutes; LAN read health is kept separate. Other configured transports can
then carry the command. Bluetooth connections and encryption sessions are
reused until a disconnect, failure or integration unload.

## Choose segment controls

1. Open **Settings → Devices & services → Govee Native → Configure**.
2. Select **Configure segment controls for devices** and continue.
3. Select devices and one mode: **Individual**, **All segments together**,
   **Individual and grouped**, or **Hide segment entities**.
4. Submit the second form. Existing settings for other devices are retained.

The domain and existing entity IDs are unchanged. H6199 still has 15 regions,
numbered 1–15 in entity names and 0–14 in actions. Previously integration-disabled
regions become available for the selected mode. An entity you explicitly
disabled yourself stays disabled until you enable it.

Segment indices come from capability metadata. A per-command array limit is
used for batching, not as a universal count of physical segments. Narrow
corrections are included for H7075 (3), H7076 (4), H7026 (16 cloud-addressable)
and H1232 (16 native ring segments). Unknown devices retain their advertised
index range. A grouping request is split into validated batches where needed.

Individual and grouped controls share one local colour map. Colours, remembered
non-black colours and supported independent brightness survive restarts.
Restoring colours does not mark a device reachable or manufacture confirmation.
Group writes update individual lights; individual edits leave a mixed group
without a falsely uniform colour.

H6199 has no independently supported segment-brightness command. Its region
lights reject brightness requests before sending anything. Use the main
light's brightness. Other models get independent brightness only when their
capability or specifically supported native protocol provides it.

## Additional model handling

| Model | Added handling | Connection / evidence boundary |
|---|---|---|
| H1232 | Main-panel colour/brightness/white temperature; 16 ring segments | Model-specific masked MQTT frames; advertised REST main-panel toggle. Extended indices never go to an incompatible REST schema. |
| H1252 | Independent main panel and ring, RGB/white temperature/brightness, live zone on/off reports | Masked MQTT frames and the checksummed two-zone status layout. |
| H1270 | Main-panel control with ring-colour preservation | Existing colour channel plus segment replay; direct Bluetooth supports the known encrypted session. |
| H5160 / H5161 | Three individual outlets | Addressed MQTT bitmask; advertised REST outlet capabilities take precedence. |
| H5086 | Voltage, current, power, power factor, on-time and daily energy | Model-gated MQTT decoder. Energy reset/reboot state is persisted to avoid counting a restored daily value twice. |
| H7152 | Ambient temperature/humidity, pump fault and drain mode | Read-only, model-gated MQTT frames. |
| H5106 | Temperature/humidity and PM2.5 | Read-only MQTT decoder with settings-frame and plausibility filters. |
| H5301 / H5310 / H5179 / H5112 / H5111 | Account-reported thermometer values; H5112 second temperature | Known BFF fields, scaling, missing-reading sentinels and account display-unit metadata. |
| H5192 / H5194 | Two/four core and ambient cooking-probe readings | MQTT passthrough read requests and status decoding. Missing probes stay unknown. |

These are software implementations based on the uploaded upstream integration's
model-specific work. The command and state formats are regression-tested with
simulated transports here. This does not claim new physical testing on those
models, all firmware revisions, or every Bluetooth proxy. See
[third-party attribution](../custom_components/govee_native/THIRD_PARTY_NOTICES.md).

## Polling and request limits

The Developer API read budget defaults to 9000 requests per day. Counts survive
reloads and reset on a new UTC day. Fleet size, remaining budget and rate-limit
headers determine the effective read interval. Control commands retain access
to the vendor allowance outside the configured read budget. HTTP 429 backoff
is honored without waiting inside a service call.

Recent local reports skip unnecessary cloud reads for a bounded number of
cycles, then reconcile. Devices with every registered entity disabled are
not read. Pushes do not postpone pending polls of unrelated devices. Private
app reads and MQTT status queries run independently of a slowed Developer API
poll. Concurrent scene fetches share a cache; **Refresh scenes** bypasses it.

## Verify an upgrade

Update the existing `custom_components/govee_native` directory and restart Home
Assistant. Keep the integration entry; do not remove/re-add it. Check the
connection sensor and test one control, several rapid slider changes, a segment
colour, a grouped write and a restart. The intended colour should restore,
while confirmation and reachability should still follow real reports.

See [Testing](TESTING.md) for the reproducible software checks and their limits.
