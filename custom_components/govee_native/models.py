"""Data and capability validation for Govee's documented OpenAPI schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from time import monotonic
import math

from .const import DIY_COLOR, MODE, MUSIC, RANGE, SCENE, TOGGLE, WORK_MODE

SETTING_KINDS = frozenset((MODE, MUSIC, SCENE, DIY_COLOR, WORK_MODE, RANGE, TOGGLE))


class InvalidCapabilityValue(ValueError):
    """The requested command does not match the device's advertised schema."""


def valid_value(parameters: dict[str, Any], value: Any) -> bool:
    """Validate a control value against discovery metadata; reject unknown STRUCTs."""
    data_type = str(parameters.get("dataType", "")).upper()
    if data_type == "ENUM":
        for option in parameters.get("options", []):
            if option.get("value", object()) == value or option.get("defaultValue", object()) == value:
                return True
            if any(child.get("value", object()) == value for child in option.get("options", [])):
                return True
        return False
    if data_type in ("INTEGER", "FLOAT", "DOUBLE"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not math.isfinite(value) or (data_type == "INTEGER" and int(value) != value):
            return False
        bounds = parameters.get("range", {})
        return (bounds.get("min", float("-inf")) <= value <= bounds.get("max", float("inf")))
    if data_type in ("STRUCT", "OBJECT"):
        if not isinstance(value, dict):
            return False
        fields = parameters.get("fields", [])
        allowed = {f.get("fieldName") for f in fields}
        if set(value) - allowed:
            return False
        for spec in fields:
            key = spec.get("fieldName")
            if spec.get("required", False) and key not in value:
                return False
            if key in value and not valid_value(spec, value[key]):
                return False
        return True
    if data_type in ("ARRAY",):
        if not isinstance(value, list):
            return False
        size = parameters.get("size", {})
        if not (size.get("min", 0) <= len(value) <= size.get("max", 10000)):
            return False
        choices = parameters.get("options", [])
        if choices:
            permitted = {item["value"] for item in choices if isinstance(item, dict) and isinstance(item.get("value"), (int, str))}
            return bool(permitted) and all(type(item) in (int, str) and item in permitted for item in value)
        element_spec = {"dataType": parameters.get("elementType", "INTEGER"), "range": parameters.get("elementRange", {})}
        return all(valid_value(element_spec, element) for element in value)
    # The API sometimes omits a dataType for read-only properties. Never send unknown writes.
    return False


@dataclass
class Device:
    sku: str
    device_id: str
    name: str
    kind: str
    capabilities: dict[tuple[str, str], dict] = field(default_factory=dict)
    states: dict[tuple[str, str], Any] = field(default_factory=dict)
    online: bool | None = None
    last_error: str | None = None
    scene_labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    app_data: dict[str, Any] = field(default_factory=dict)
    app_source: bool = False
    cloud_source: bool = False
    cloud_capabilities: set[tuple[str, str]] = field(default_factory=set)
    last_report_at: float = 0.0
    last_transport: str | None = None
    last_command_result: str | None = None
    last_command_transport: str | None = None
    assumed_keys: set[tuple[str, str]] = field(default_factory=set)
    state_times: dict[tuple[str, str], float] = field(default_factory=dict)
    local_settings: dict[str, Any] = field(default_factory=dict)
    segment_colors: dict[int, int] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, data: dict) -> "Device":
        device = cls(
            sku=data["sku"],
            device_id=data["device"],
            name=data.get("deviceName") or data["sku"],
            kind=data.get("type", "devices.types.unknown").removeprefix("devices.types."),
            app_data=data.get("private", {}),
            app_source=bool(data.get("private")),
        )
        for item in data.get("capabilities", []):
            kind, instance = item.get("type"), item.get("instance")
            if kind and instance:
                device.capabilities[(kind, instance)] = item
                if kind in ("devices.capabilities.dynamic_scene", "devices.capabilities.diy_color_setting"):
                    for opt in item.get("parameters", {}).get("options", []):
                        if "value" not in opt or "name" not in opt:
                            continue
                        prefix = "DIY" if instance == "diyScene" else ("Snapshot" if instance == "snapshot" else "Scene")
                        label = f"{prefix}: {opt['name']}"
                        device.scene_labels[label] = {"type": kind, "instance": instance, "value": opt["value"]}
        return device

    def has(self, kind: str, instance: str) -> bool:
        return (kind, instance) in self.capabilities

    def capability(self, kind: str, instance: str) -> dict | None:
        return self.capabilities.get((kind, instance))

    def options(self, kind: str, instance: str) -> list[dict]:
        cap = self.capability(kind, instance) or {}
        return cap.get("parameters", {}).get("options", [])

    def range(self, kind: str, instance: str) -> dict:
        cap = self.capability(kind, instance) or {}
        return cap.get("parameters", {}).get("range", {})

    def state(self, kind: str, instance: str) -> Any:
        return self.states.get((kind, instance))

    def valid_setting(self, kind: str, instance: str, value: Any) -> bool:
        """Only retain writable settings that still match discovery metadata."""
        if kind not in SETTING_KINDS:
            return False
        try:
            self.validate(kind, instance, value)
        except (InvalidCapabilityValue, TypeError, AttributeError):
            return False
        return True

    def accepts_report(self, kind: str, instance: str, value: Any) -> bool:
        # Some write-only controls report empty/unknown/unsupported sentinels.
        return value is not None and value != "" and (
            kind not in SETTING_KINDS or self.valid_setting(kind, instance, value))

    def _remember_native_setting(self, kind: str, instance: str, value: Any) -> None:
        # H6199 sends area/style/saturation (or mode/sensitivity) together.
        # Its next command must also use settings restored or read from a device.
        if self.sku == "H6199" and kind in SETTING_KINDS:
            self.local_settings[instance] = value

    def update_state(self, payload: dict, *, requested_at: float = 0.0) -> None:
        for item in payload.get("capabilities", []):
            key = (item.get("type"), item.get("instance"))
            if key[0] == "devices.capabilities.online" and key[1] == "online":
                value = item.get("state", {}).get("value")
                if isinstance(value, bool) and self.last_report_at <= requested_at:
                    self.online = value
                continue
            if self.state_times.get(key, 0) > requested_at:
                continue
            if "state" in item:
                value = item["state"].get("value")
                if (key in self.assumed_keys and monotonic() - self.state_times.get(key, 0) < 6
                        and value != self.states.get(key)):
                    # A cloud cache immediately after a write is often older
                    # than the command. Real push reports bypass this grace.
                    continue
                # Govee uses '' for values that cannot be queried.
                if self.accepts_report(*key, value):
                    self.states[key] = value
                    self.assumed_keys.discard(key)
                    self._remember_native_setting(*key, value)
                # An empty cloud readback means "not retrievable"; retain a
                # valid MQTT/LAN/BLE value instead of changing it to unknown.
        self.last_error = None

    def report(self, states: dict, transport: str) -> dict:
        """Apply device feedback; a sent command must never call this method."""
        states = {key: value for key, value in states.items() if self.accepts_report(*key, value)}
        if not states:
            return {}
        now = monotonic()
        self.states.update(states)
        self.state_times.update({key: now for key in states})
        self.assumed_keys.difference_update(states)
        self.last_report_at = now
        self.last_transport = transport
        self.online = True
        self.last_error = None
        for key, value in states.items():
            self._remember_native_setting(*key, value)
        return states

    def assume(self, kind: str, instance: str, value: Any) -> None:
        """Keep explicitly labelled command-derived state for write-only BLE."""
        self.states[(kind, instance)] = value
        self.assumed_keys.add((kind, instance))
        self.state_times[(kind, instance)] = monotonic()
        self._remember_native_setting(kind, instance, value)

    def validate(self, kind: str, instance: str, value: Any) -> None:
        cap = self.capability(kind, instance)
        if not cap:
            raise InvalidCapabilityValue(f"{instance}: capability not advertised")
        params = cap.get("parameters", {})
        if not valid_value(params, value):
            raise InvalidCapabilityValue(f"{instance}: invalid value for advertised schema")
        if kind == "devices.capabilities.work_mode" and instance == "workMode" and isinstance(value, dict):
            fields = params.get("fields", [])
            primary = next((f for f in fields if f.get("fieldName") == "workMode"), None)
            secondary = next((f for f in fields if f.get("fieldName") == "modeValue"), None)
            if primary and secondary and "modeValue" in value:
                current_mode = next((o for o in primary.get("options", []) if o.get("value") == value.get("workMode")), None)
                branch = next((o for o in secondary.get("options", []) if o.get("name") == (current_mode or {}).get("name")), None)
                if branch is None or not any(
                    o.get("value") == value["modeValue"] for o in branch.get("options", [])
                ) and branch.get("defaultValue", object()) != value["modeValue"]:
                    raise InvalidCapabilityValue("workMode: modeValue does not match selected working mode")
