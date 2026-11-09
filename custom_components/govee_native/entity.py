"""Shared Govee entity class and identification."""

from __future__ import annotations

from typing import Any
from time import monotonic

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import GoveeError
from .const import DOMAIN, ON_OFF
from .coordinator import GoveeCoordinator
from .models import Device


def readable(instance: str) -> str:
    import re
    return re.sub(r"(?<!^)(?=[A-Z])", " ", instance.replace("_", " ")).strip().title()


class GoveeEntity(CoordinatorEntity[GoveeCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: GoveeCoordinator, device: Device, suffix: str):
        super().__init__(coordinator)
        self.device = device
        self._attr_unique_id = f"{device.device_id}_{suffix}".lower().replace(":", "")

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.device_id)},
            name=self.device.name,
            manufacturer="Govee",
            model=self.device.sku,
            sw_version=self.device.app_data.get("firmware") or None,
            hw_version=self.device.app_data.get("hardware") or None,
        )

    @property
    def available(self) -> bool:
        # For a private-app device, live MQTT or LAN is an independent source.
        # OpenAPI auth/rate-limit failures must never hide a healthy push entity.
        if self.device.app_source and self.device.kind in ("light", "socket"):
            if self.coordinator.ble and self.coordinator.ble.available(self.device):
                return True
            if self.coordinator.lan and self.coordinator.lan.available(self.device):
                return True
            if (self.coordinator.mqtt and self.coordinator.mqtt.connected and
                    self.device.last_transport is not None and
                    monotonic() - self.device.last_report_at < 180):
                return True
            if self.coordinator.api is None:
                return False
        return bool(self.coordinator.last_update_success and
                    self.device.last_error is None and self.device.online is not False)

    @property
    def assumed_state(self) -> bool:
        return bool(self.device.assumed_keys)

    @property
    def extra_state_attributes(self) -> dict:
        return {"last_command_transport": self.device.last_command_transport,
                "last_command_result": self.device.last_command_result,
                "state_assumed": bool(self.device.assumed_keys)}

    async def async_write(self, kind: str, instance: str, value: Any) -> None:
        from homeassistant.exceptions import HomeAssistantError
        try:
            await self.coordinator.async_control(self.device, kind, instance, value)
        except (ValueError, GoveeError) as exc:
            # API-layer errors are converted to HA service errors, never silently ignored.
            raise HomeAssistantError(f"Govee command failed: {exc}") from exc


class GoveeToggleEntity(GoveeEntity):
    """Keep binary controls as sliders while retaining uncertainty diagnostics."""

    _toggle_key = (ON_OFF, "powerSwitch")

    @property
    def assumed_state(self) -> bool:
        # HA uses this flag to replace a slider with separate on/off buttons.
        # Confirmation remains exposed through state_assumed and command results.
        return False

    @property
    def is_on(self) -> bool:
        # HA also renders two buttons for unknown. Until a value is available,
        # show the slider in its off position and label the state as assumed.
        return bool(self.device.state(*self._toggle_key))

    @property
    def extra_state_attributes(self) -> dict:
        attributes = super().extra_state_attributes
        attributes["state_assumed"] |= self.device.state(*self._toggle_key) is None
        return attributes
