# Actions and automations

Use normal Home Assistant entity actions for routine operation, such as
`light.turn_on`, `light.turn_off`, `switch.turn_on`, `select.select_option` and
`number.set_value`. Only use actions and values supported by the selected entity.

The integration also registers these actions under the **`govee_native`** domain:

| Action | Required fields | Purpose |
|---|---|---|
| `govee_native.set_segments` | `device_id`, `segments`, exactly one of `rgb` or `brightness` | Sets advertised RGBIC regions. H6199 RGB regions are numbered 0–14; independent brightness needs a supported API capability. |
| `govee_native.send_capability` | `device_id`, `capability_type`, `instance`, `value` | Validates and sends a capability advertised for that device. |
| `govee_native.refresh_scenes` | `device_id` | Refreshes the device's scene catalog. |
| `govee_native.refresh_app_catalogs` | None | Refreshes available app catalogs; may reload the entry if its executable shortcuts change. |
| `govee_native.request_app_status` | `device_id` | Requests device status over the account MQTT connection. |
| `govee_native.app_request` | `operation`; optional `entry_id`, `query`, `body` | Calls an allowlisted private route; writes require the advanced-write option. |

The `device_id` fields in these advanced actions refer to the Govee cloud
device identifier, not a Home Assistant device-registry ID. Use the normal
entity actions and entity selector for ordinary automations.

For `set_segments`, `segments` is a list of zero-based indices and `rgb` is
one packed integer from 0 to 16777215. For example, red is 16711680. An H6199
supports 15 native RGB regions; the generic action can also serve different
region counts when a device explicitly advertises them.

The action editor displays the field selectors and descriptions from
[`services.yaml`](../custom_components/govee_native/services.yaml).

Private routes are an advanced interface. An extracted endpoint name alone
does not establish that its request body is correct for a device or that the
current server accepts it. Prefer the supported entities for normal use.
