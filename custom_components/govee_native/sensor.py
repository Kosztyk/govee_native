"""Capability sensors plus private Govee app device readings and catalogs."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PROPERTY
from .entity import GoveeEntity, readable
from .binary_sensor import BOOLEAN_NAMES
from .app_models import app_readings
from time import monotonic

MEASUREMENTS={
    "sensorHumidity":(SensorDeviceClass.HUMIDITY,PERCENTAGE),
    "carbonDioxideConcentration":(SensorDeviceClass.CO2,"ppm"),
    "pm25":(SensorDeviceClass.PM25,"µg/m³"),
    "pm10":(SensorDeviceClass.PM10,"µg/m³"),
}


async def async_setup_entry(hass:HomeAssistant,entry:ConfigEntry,async_add_entities):
    coordinator=hass.data[DOMAIN][entry.entry_id]
    entities=[GoveeSensor(coordinator,d,kind,instance)
        for d in coordinator.devices.values()
        for kind,instance in d.capabilities
        if kind==PROPERTY and not any(fragment in instance.lower() for fragment in BOOLEAN_NAMES)]
    if coordinator.app or any(d.app_source for d in coordinator.devices.values()):
        entities.append(GoveeAppCatalogSensor(coordinator,entry.entry_id))
        for device in coordinator.devices.values():
            if not device.app_source:continue
            entities.append(GoveeAppConnectionSensor(coordinator,device))
            readings=app_readings(device.app_data.get("last",{}))
            for field in readings:
                entities.append(GoveeAppRawReading(coordinator,device,field))
            if device.app_data.get("battery") is not None and "battery" not in readings:
                entities.append(GoveeAppRawReading(coordinator,device,"battery"))
    async_add_entities(entities)


class GoveeSensor(GoveeEntity,SensorEntity):
    def __init__(self,coordinator,device,kind,instance):
        super().__init__(coordinator,device,f"sensor_{instance}")
        self.kind,self.instance=kind,instance
        self._attr_name=readable(instance)
        if instance in MEASUREMENTS:
            self._attr_device_class,self._attr_native_unit_of_measurement=MEASUREMENTS[instance]

    @property
    def native_value(self):
        value=self.device.state(self.kind,self.instance)
        return value if isinstance(value,(int,float,str,bool)) else None


class GoveeAppCatalogSensor(CoordinatorEntity,SensorEntity):
    _attr_has_entity_name=True
    _attr_name="Android app catalog"
    _attr_icon="mdi:application"

    def __init__(self,coordinator,entry_id):
        super().__init__(coordinator)
        self._attr_unique_id=f"govee_native_app_{entry_id}_catalog"
        self._coordinator=coordinator

    @property
    def native_value(self):
        return len(self._coordinator.app_rows)

    @property
    def extra_state_attributes(self):
        # Counts only: avoid leaking cloud room names/phone/account metadata
        return {"transport":"app2.govee.com", "catalogs":sorted(self._coordinator.app_catalogs),
                "app_tap_to_run_count":len(self._coordinator.one_clicks),
                "app_tap_to_run_error":self._coordinator.one_click_last_error,
                "app_tap_to_run_activations":self._coordinator.one_click_runs,
                "mqtt_connected":bool(self._coordinator.mqtt and self._coordinator.mqtt.connected)}


class GoveeAppConnectionSensor(GoveeEntity,SensorEntity):
    _attr_name="App connection"
    _attr_icon="mdi:cloud-sync"
    def __init__(self,coordinator,device):super().__init__(coordinator,device,"app_connection")
    @property
    def native_value(self):
        if self.coordinator.lan and self.coordinator.lan.available(self.device):
            return "lan"
        if self.coordinator.prefer_bluetooth and self.coordinator.ble and self.coordinator.ble.available(self.device):
            return "bluetooth"
        if (self.coordinator.mqtt and self.coordinator.mqtt.connected and self.device.app_data.get("topic")
                and monotonic() - self.device.last_report_at < 180):
            return "mqtt"
        if self.coordinator.ble and self.coordinator.ble.available(self.device):
            return "bluetooth"
        if self.coordinator.api and self.device.capabilities and self.device.kind!="private_app":
            return "openapi"
        return "waiting_for_device" if self.coordinator.mqtt and self.coordinator.mqtt.connected else "disconnected"

    @property
    def available(self):
        return True  # Connection problems must remain visible in diagnostics.

    @property
    def extra_state_attributes(self):
        mqtt=self.coordinator.mqtt
        pending=self.coordinator.pending_commands.get(self.device.device_id)
        return {
            "mqtt_broker_connected": bool(mqtt and mqtt.connected),
            "mqtt_messages_received": mqtt.messages_received if mqtt else 0,
            "mqtt_commands_published": mqtt.commands_published if mqtt else 0,
            "last_mqtt_command": mqtt.last_command_name if mqtt else None,
            "last_mqtt_message_at": mqtt.last_message_at if mqtt else None,
            "device_state_updates_confirmed": self.coordinator.confirmed_device_updates,
            "commands_confirmed": self.coordinator.confirmed_commands,
            "commands_unconfirmed": self.coordinator.unconfirmed_commands,
            "command_waiting_for_device_state": bool(pending),
            "developer_api_available": self.coordinator.api is not None,
            "developer_api_error": self.coordinator.openapi_auth_error,
            "last_command_transport": self.device.last_command_transport,
            "last_command_result": self.device.last_command_result,
            "state_assumed": bool(self.device.assumed_keys),
            "last_device_report_age": round(monotonic() - self.device.last_report_at, 1) if self.device.last_report_at else None,
            "bluetooth_enabled": self.coordinator.ble is not None,
            "bluetooth_address_known": bool(self.coordinator.ble and self.coordinator.ble.address(self.device)),
            "bluetooth_available": bool(self.coordinator.ble and self.coordinator.ble.available(self.device)),
            "bluetooth_error": self.coordinator.ble.failures.get(self.device.device_id) if self.coordinator.ble else None,
            "mqtt_command_cooldown": max(0, round(self.coordinator._mqtt_retry_after.get(self.device.device_id, 0) - monotonic())),
            "lan_error": self.coordinator.lan.last_error if self.coordinator.lan else None,
        }


class GoveeAppRawReading(GoveeEntity,SensorEntity):
    """Expose numerical BFF lastDeviceData values; raw units are model-specific."""
    def __init__(self,coordinator,device,field):
        super().__init__(coordinator,device,f"app_{field}")
        self.field=field
        names={"tem":"App reported temperature (raw)",
               "hum":"App reported humidity (raw)",
               "temperature":"App reported temperature (raw)",
               "humidity":"App reported humidity (raw)",
               "battery":"Battery", "onOff":"App reported power", "brightness":"App reported brightness"}
        self._attr_name=names.get(field,f"App {field}")
        if field=="battery":
            self._attr_native_unit_of_measurement=PERCENTAGE
            self._attr_device_class=SensorDeviceClass.BATTERY

    @property
    def native_value(self):
        if self.field=="battery" and self.device.app_data.get("battery") is not None:
            return self.device.app_data["battery"]
        return app_readings(self.device.app_data.get("last",{})).get(self.field)
