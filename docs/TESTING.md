# Testing and validation

## Recorded environment

- CPython 3.14.9 on Linux.
- Home Assistant 2026.11.0, using its real entity, coordinator, config-entry and selector classes.
- aiomqtt 2.5.1 and the pinned Bluetooth/testing dependencies in
  [`requirements-test.txt`](../requirements-test.txt).

The 0.5.2 suite passed **184 tests**, with zero failures. Five warnings came
from Home Assistant HTTP/backoff dependencies. The runtime `F` and `E9` lint
checks and Python compilation passed. The suite was rerun for this repository
HACS preparation after the manifest metadata and version were updated.

## What the tests cover

| Area | Checks |
|---|---|
| Account/API parsing | Device normalization, authentication errors, capability validation and scene/shortcut parsing. |
| MQTT | Account reply routing, command/query envelopes, genuine status decoding, echo rejection, pacing and concurrent write ordering. |
| Confirmation | Nonblocking send, matching-field feedback, superseded retries, silence cooldown, bounded reconnect, query quarantine and no false confirmation from broker acceptance. |
| State | Missing report timestamps never imply a fresh connection, even at zero uptime; the first MQTT status query is never throttled. Fresh push data survives stale/empty cloud reads; real device reports reconcile assumptions. |
| Bluetooth | H6199 original/updated frames, connection reuse, encrypted-frame round trips, address validation, stale discovery and unload cleanup. |
| LAN | Device identification, color mode, fresh status and UDP handling. |
| Entities | Platform construction, stable unique IDs, setup/unload and action cleanup. |
| Controls | Real HA state serialization through startup, unconfirmed on/off commands and later device feedback for slider controls. |
| Selection retention | Actual HA Store save/load and coordinator recreation; zero-valued choices, video/music parameters, numerical controls, toggles, structured work modes, scenes, invalid readbacks and HA restore fallback. No commands are resent by restoration. |
| Options | Existing device settings and explicit entity disables are retained; Bluetooth and segment forms submit correctly. |
| HACS review fixes | Setup rejects access to another integration's entries, uses only its own key, and discards the retired option. Official MQTT events use the cached verified SSL context with certificate loading forbidden in the test. Reauth handles rejected email-code requests and completes after a successful retry. |
| RGBIC | Generic discovery, model overrides, batch limits, grouping, real brightness writes, invalid-value rejection and restored colours. |
| Extra models | Outlet bitmasks, independent light-zone masks/status/temperature, energy reset/reboot accounting, dehumidifier/AQI/probe decoding and missing-reading sentinels. |
| Request policy | Daily counter persistence/reset, quota pacing, rate-limit headers, backoff recovery, local-state reconciliation and push/poll independence. |
| Diagnostics | Identity redaction and distinction between broker availability and device feedback. |

## Run the suite

From the repository root, create a separate Python 3.14 environment:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m pytest -q
```

Optional static checks:

```bash
python -m pip install ruff
python -m ruff check --select F,E9 custom_components/govee_native
python -m compileall -q custom_components/govee_native
```

`requirements-test.txt` is for development. Home Assistant manages the runtime
dependencies listed in the integration manifest.

## What these results do not establish

HTTP, MQTT broker and Bluetooth/GATT boundaries are simulated. Packet tests
use recorded/reference protocol behavior; they are not physical acceptance
tests of every device, firmware, Bluetooth adapter or network. Read-only
inspection of a configured Home Assistant installation informed development,
but it does not prove every write control works on hardware.

The reported options-dialog Submit issue has not been reproduced. A successful
ordinary save-path check does not certify every browser interaction or input.

## Practical hardware check after installation

1. Use the main light to turn on/off, change brightness and set RGB; check the
   physical light as well as its displayed state.
2. Inspect the connection sensor, transport/result attributes and confirmation
   counters. A broker acknowledgment alone should not be treated as device feedback.
3. If configured, check LAN and direct Bluetooth separately on supported hardware.
4. For an H6199, check video mode, Gradient, music and one enabled segment;
   select the compatible firmware profile.
5. Change one integration option, submit it and reopen Configure to confirm it
   was retained. Include this step when reporting an options-form issue.
6. Test existing automations with the intended Native entities before retiring
   another integration.
