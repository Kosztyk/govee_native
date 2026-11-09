"""PII-safe discovery diagnostics, including app-only device coverage."""
from __future__ import annotations
import hashlib
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .const import DOMAIN, VERSION
from time import monotonic


def _hash_id(value: str)->str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _shape(value,level=0):
    if level>4:return "max-depth"
    if isinstance(value,dict):
        return {str(k):_shape(v,level+1) for k,v in value.items()
                if not any(s in str(k).lower() for s in
                    ("device","name","email","topic","ssid","address","password","token","key","mac"))}
    if isinstance(value,list):return {"count":len(value),"first":_shape(value[0],level+1) if value else None}
    return type(value).__name__


async def async_get_config_entry_diagnostics(hass:HomeAssistant,entry:ConfigEntry)->dict:
    coordinator=hass.data[DOMAIN][entry.entry_id]
    return {
        "integration":"govee_native","version":VERSION,
        "sources":{"private_app":bool(coordinator.app),"openapi":bool(coordinator.api),
                   "private_mqtt_connected":bool(coordinator.mqtt and coordinator.mqtt.connected),
                   "bluetooth_enabled": coordinator.ble is not None},
        "openapi_diagnostics": {
            "enabled": coordinator.api is not None,
            "auth_error": coordinator.openapi_auth_error,
        },
        "private_app_auth_error": coordinator.app_auth_error,
        "private_app_unavailable": coordinator.app_unavailable,
        "cached_local_inventory_allowed": coordinator.allow_cached_inventory,
        "lan_error": coordinator.lan.last_error if coordinator.lan else None,
        "private_mqtt_diagnostics": {
            "broker_connected": bool(coordinator.mqtt and coordinator.mqtt.connected),
            "messages_received": coordinator.mqtt.messages_received if coordinator.mqtt else 0,
            "commands_published": coordinator.mqtt.commands_published if coordinator.mqtt else 0,
            "last_command": coordinator.mqtt.last_command_name if coordinator.mqtt else None,
            "confirmed_device_updates": coordinator.confirmed_device_updates,
            "commands_confirmed": coordinator.confirmed_commands,
            "commands_unconfirmed": coordinator.unconfirmed_commands,
            "pending_command_count":len(coordinator.pending_commands),
        },
        "app_features": {
            "tap_to_run_shortcuts": len(coordinator.one_clicks),
            "tap_to_run_activations": coordinator.one_click_runs,
            "tap_to_run_fetch_error": coordinator.one_click_last_error,
            "apk_scene_catalog_retrieved": sum(1 for k in coordinator.app_catalogs if k.startswith("scenes_")),
        },
        "device_count":len(coordinator.devices),
        "devices":[{"id_hash":_hash_id(d.device_id),"sku":d.sku,"kind":d.kind,
                    "source_app":d.app_source,
                    "source_openapi":d.cloud_source,
                    "last_command_transport":d.last_command_transport,
                    "last_command_result":d.last_command_result,
                    "state_assumed":bool(d.assumed_keys),
                    "last_report_age":round(monotonic()-d.last_report_at,1) if d.last_report_at else None,
                    "bluetooth_address_known":bool(coordinator.ble and coordinator.ble.address(d)),
                    "bluetooth_available":bool(coordinator.ble and coordinator.ble.available(d)),
                    "bluetooth_error":coordinator.ble.failures.get(d.device_id) if coordinator.ble else None,
                    "lan_route_available":bool(coordinator.lan and coordinator.lan.available(d)),
                    "capabilities":[{"type":kind,"instance":instance} for kind,instance in d.capabilities],
                    "app_reading_keys":sorted(d.app_data.get("last",{}).keys()) if isinstance(d.app_data.get("last"),dict) else []}
                   for d in coordinator.devices.values()],
        "catalog_shapes":{("scenes_"+_hash_id(k[7:]) if k.startswith("scenes_") else k):_shape(v)
                          for k,v in coordinator.app_catalogs.items()},
    }
