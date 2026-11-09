# Changelog

## 0.5.0

- Send light commands without waiting for a device report; confirm each property in background tasks. Supersede stale confirmations and queued slider values.
- Require actual device responses before selecting MQTT control. Add a bounded silent-session reconnect, status-query quarantine, credential refresh and transport diagnostics.
- Shorten LAN readback and temporarily skip repeatedly failing writes while keeping LAN read health independent.
- Discover RGBIC segments from capability metadata; keep per-command batch limits separate from physical indices. Add individual/grouped/both/hidden modes and persist colours and supported independent brightness.
- Reuse direct Bluetooth connections; support the known H1270 encrypted protocol alongside H6199 profiles.
- Add H1232/H1252 ceiling-light zone paths, H1270 main-panel/ring preservation, H5160/H5161 individual outlets, H5086 energy/power, H7152 ambient/pump data, H5106 air quality and supported thermometer/probe readings.
- Persist energy reset/reboot tracking; reserve a daily Developer API read budget and honor rate-limit backoff.
- Keep private app polling independent and prevent push traffic from postponing cloud polls. Cache concurrent scene requests.
- Preserve domain, existing entity IDs, account options and consistent toggles. Include upstream MIT attribution.
- Validate with 158 simulated-network regression tests; no new physical-device compatibility claim.

## 0.4.2

- Require a real MQTT/LAN device report before declaring the connection fresh,
  including immediately after host startup; always send the first status query.
- Add deterministic freshness tests for new and long-running hosts.
- Add HACS, hassfest and regression-test GitHub Actions without ignored checks.
- Add the Govee brand icon inside the integration folder.
- Add Marketplace categories and normalize manifest ordering.
- Keep the HACS manifest minimal and document catalog submission requirements.
- Preserve the `govee_native` domain and existing entity IDs.

## 0.4.1

- Keep one slider style for lights, switches, fans, humidifiers and H6199 segments.
- Keep confirmation/assumption diagnostics independent of the slider appearance.
- Use an explicit visual off fallback before a binary device value is known.
- Add real Home Assistant state-serialization regressions; 90 tests pass.
- Prepare the public `Kosztyk/govee_native` repository: correct documentation
  and issue links, document Marketplace/manual setup and publish synthetic test fixtures.

## 0.4.0

- Add H6199 direct Bluetooth, firmware profiles and native video/music controls.
- Add optional H6199 region lights and diagnostic refresh buttons.
- Improve account MQTT envelopes, status queries, confirmation and fallback behavior.
- Protect fresh push state from stale cloud reads and retain device identity on rediscovery.
- Improve LAN freshness checks, optional-key handling and cached local inventory.
- Update climate properties for Home Assistant 2026.11.

## Earlier versions

The initial integration provided account/API discovery, advertised capability
platforms and private route access. This repository publishes the current
source; earlier working archives are not presented as GitHub releases.
