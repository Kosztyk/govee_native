# Feature coverage — 0.4.2

“Implemented” means code exists and its packet/state behavior was tested locally.
It does not mean every feature was physically exercised on every H6199.

| App/device feature | Integration status | Limits |
|---|---|---|
| Consistent slider controls | Added in 0.4.1 for lights, switches, fans and humidifiers | Missing state starts visually off with `state_assumed: true`; confirmation diagnostics remain independent |
| Account login and email verification | Existing implementation retained | App compatibility profile retained; not rewritten as a 7.7.11 clone |
| Device inventory and metadata | Improved | Exact BLE address and firmware/hardware metadata retained; local metadata cache added |
| Power, global brightness, RGB | Implemented via advertised transports | MQTT basic controls require matching device feedback; direct BLE is H6199 only |
| White color temperature | LAN/native MQTT/OpenAPI | No direct BLE Kelvin lookup implementation |
| LAN control | Discovery/status/control implemented | Requires real supported firmware and fresh responses; no promise that this H6199 supports LAN |
| Direct Bluetooth | Added for H6199 | Actual MAC, connectable adapter/proxy and compatible unencrypted firmware required |
| Bluetooth encryption/provisioning | Not implemented | No extracted keys or guessed session protocol |
| Connection diagnostics | Improved | Distinguishes broker connection, reports, confirmation, assumed writes, BLE and LAN problems |
| 15 H6199 RGBIC region colors | Added | Optional disabled-by-default entities; zero-based service indices 0..14; readback assumed |
| Independent region hardware brightness | Only when advertised by OpenAPI | Not added to the H6199 BLE protocol; main light brightness is global |
| Gradient | Added native H6199 routes | MQTT ptReal or BLE; assumed setting until usable readback |
| Video/DreamView mode | Added native H6199 routes | Video off changes to last RGB; this is camera video mode, not a full multi-device DreamView editor |
| Video area, style, saturation | Added | Partial/All, Movie/Game and 0..100 saturation; settings are integration last-sent values |
| Device microphone music modes | Added H6199 profiles | Original packet modes or later V3 effects; compatible firmware needed |
| Microphone sensitivity | Added | No phone microphone capture or audio streaming |
| Built-in native scene presets | Added eight captured H6199 presets | Broader API scene catalog retained; static APK catalog membership is not proof of device compatibility |
| Developer API scenes, DIY and snapshots | Existing discovery/control retained, merge improved | Requires advertised capability and valid optional key; selected effect may not be retrievable |
| App Tap-to-Run | Existing exact-packet execution retained | Only executable packets for account-owned devices are exposed; availability depends on the account catalog |
| App groups | Read catalog retained | No full cloud group membership/editor implementation |
| Circadian rhythms and vendor schedules | Not implemented as app-equivalent editors | HA automations can call the exposed entities, but are not a port of the Govee editor |
| Cloud automation authoring | Not implemented | Raw private endpoints do not constitute working automation features |
| DreamView group topology and calibration | Not implemented | Camera calibration, screen geometry, device placement and synchronization editor remain missing |
| DIY palette/workshop drawing/upload editor | Not implemented | Executing a saved API DIY scene is distinct from creating/uploading one |
| Firmware management/update | Not implemented | Firmware version displayed when metadata supplies it; no updater |
| Device sharing, rooms and account management | Not implemented as editors | Existing raw route access is advanced access, not UI parity |
| Sensor historical data/export/calibration | Not ported from feature modules | Existing advertised sensor values/events remain; device families not present in the inspected installation were not hardware-tested |
| Fans, heaters, humidifiers, sockets, sensors | Existing OpenAPI capability platforms retained | Not a claim of full private-protocol support for every SKU in the APK |
| App shopping/community/AI features | Not implemented | No claim that the HA integration reproduces the complete phone application |

The remaining items require additional device-specific protocol work and runtime
evidence. An unpacked main application payload or a suitable Android runtime
capture is needed to analyze the protected 7.7.11 core implementation. Static
endpoint names and asset scene IDs alone are insufficient to implement those
features reliably.
