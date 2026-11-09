# Changelog

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
