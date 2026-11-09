"""Cache discovery metadata so local control can start during a cloud outage.

No credentials or device state are cached. Saved metadata is not evidence
that a device is currently reachable; live Bluetooth/LAN checks still apply.
"""
from homeassistant.helpers.storage import Store
from .models import Device
from .h6199 import add_capabilities
from .capabilities import add_model_capabilities

PRIVATE_FIELDS = ("device", "sku", "name", "topic", "ble_address", "ble_name", "firmware", "hardware", "goods_type", "source")


class InventoryCache:
    def __init__(self, hass, entry_id):
        self.store = Store(hass, 1, f"govee_native.{entry_id}.inventory")

    async def load(self):
        data = await self.store.async_load()
        result = {}
        for item in data.get("devices", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ("sku", "device")):
                continue
            try:
                d = Device.from_payload(item)
                d.cloud_source = bool(item.get("cloud_source"))
                d.cloud_capabilities = {tuple(key) for key in item.get("cloud_capabilities", [])
                    if isinstance(key, list) and len(key) == 2 and all(isinstance(k, str) for k in key)}
                add_capabilities(d)
                add_model_capabilities(d)
                result[d.device_id] = d
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
        return result

    async def save(self, devices):
        await self.store.async_save({"devices": [{
            "sku": d.sku, "device": d.device_id, "deviceName": d.name,
            "type": "devices.types." + d.kind,
            "capabilities": list(d.capabilities.values()),
            "cloud_source": d.cloud_source,
            "cloud_capabilities": [list(key) for key in sorted(d.cloud_capabilities)],
            "private": {key: d.app_data[key] for key in PRIVATE_FIELDS if key in d.app_data},
        } for d in devices.values()]})
