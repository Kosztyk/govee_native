# Protocol notes and implementation scope

The integration combines an optional Govee Developer API client, private Govee
Home account discovery/MQTT, published LAN behavior and model-specific native
frames. The encrypted BLE helper includes fixed vendor protocol constants.
Account credentials, APKs, user logs and account dumps are not distributed.
See the component's [third-party notices](../custom_components/govee_native/THIRD_PARTY_NOTICES.md)
for the adapted code, source version, copyrights and full MIT license.

## Implementation references

- [Original H6199 hardware captures](https://github.com/Obi2000/Govee-H6199-Reverse-Engineering).
- [Govee app MQTT envelope investigation](https://github.com/jwmoss/goveetl/blob/main/internal/govee/mqtt.go).
- [Independent MQTT/state implementation](https://github.com/wez/govee2mqtt/blob/main/src/service/iot.rs).
- [H6199 Bluetooth model/protocol reference](https://github.com/lasswellt/govee-homeassistant/tree/main/custom_components/govee/api).
- [Govee LAN guide](https://app-h5.govee.com/user-manual/wlan-guide).
- [Home Assistant Bluetooth documentation](https://developers.home-assistant.io/docs/bluetooth/).

## App analysis limits

Earlier accessible app modules and independent protocol implementations informed
the account routes and command formats. The examined Govee Home 7.7.11 base APK
was protected; its main lighting implementation was not recovered as a complete
decompiled codebase. Accessible assets and scene/SKU catalogs are reference
material, not proof that a given device can execute every listed feature.

The account client retains its existing compatibility profile. It does not
claim complete Govee Home 7.7.11 behavior. H6199 packet builders are restricted
to that model; original and updated firmware profiles select explicit protocol
families instead of guessing that all devices accept the same commands.

Private routes exposed by `app_request` have fixed allowlisted methods/paths.
Writes are opt-in and depend on the correct device-specific body and the
current backend. They do not constitute full schedule, firmware, DIY drawing,
camera-calibration or multi-device DreamView editors.

See [feature coverage](FEATURE_MATRIX.md) and [testing](TESTING.md) for the
implemented behavior and its verification limits.
