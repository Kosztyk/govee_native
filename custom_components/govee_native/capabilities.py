"""Capability discovery with narrow, documented model corrections.

Protocol corrections are adapted from lasswellt/govee-homeassistant 2026.10.2.
See THIRD_PARTY_NOTICES.md. A command's array size limit is not generally a
physical segment count; overrides are used only for the named models.
"""
from __future__ import annotations

from .const import COLOR, ON_OFF, PROPERTY, SEGMENT, TOGGLE

SEGMENT_COUNTS = {"H7075": 3, "H7076": 4, "H7026": 16, "H1232": 16}
MASKED_RING_MODELS = {"H1232", "H1252"}
MAIN_PANEL_MODELS = {"H1270"}
MULTI_OUTLET_MODELS = {"H5160": 3, "H5161": 3}
THERMOMETER_MODELS = {"H5301", "H5310", "H5179", "H5112", "H5111"}
PROBE_MODELS = {"H5192": 2, "H5194": 4}
SEGMENT_MODES = ("individual", "grouped", "both", "disabled")
NO_STATUS_QUERY_MODELS = {"H5110", "H5220", "H5111", "H5075", "H5074", "H5108"}


def _integer(value):
    return value if type(value) is int and 0 < value <= 200 else None


def segment_indices(device) -> tuple[int, ...]:
    """Resolve addressable indices, keeping batch size separate from index range."""
    cap = device.capability(SEGMENT, "segmentedColorRgb")
    if not cap:
        return ()
    override = SEGMENT_COUNTS.get(device.sku.upper())
    if override:
        return tuple(range(override))
    params = cap.get("parameters", {})
    direct = _integer(params.get("segmentCount"))
    if direct:
        return tuple(range(direct))
    field = next((f for f in params.get("fields", []) if f.get("fieldName") == "segment"), {})
    options = field.get("options", [])
    indices = sorted({o["value"] for o in options if isinstance(o, dict)
                      and type(o.get("value")) is int and 0 <= o["value"] < 200})
    if indices:
        return tuple(indices)
    bounds = field.get("elementRange", {})
    low, high = bounds.get("min", 0), bounds.get("max")
    if type(low) is int and type(high) is int and 0 <= low <= high < 200:
        return tuple(range(low, high + 1))
    count = _integer(field.get("size", {}).get("max"))
    return tuple(range(count)) if count else ()


def segment_batch_size(device, instance="segmentedColorRgb") -> int:
    cap = device.capability(SEGMENT, instance) or {}
    field = next((f for f in cap.get("parameters", {}).get("fields", [])
                  if f.get("fieldName") == "segment"), {})
    return _integer(field.get("size", {}).get("max")) or 1


def segment_mode(options, device_id):
    value = options.get("segment_mode_by_device", {}).get(device_id, "individual")
    return value if value in SEGMENT_MODES else "individual"


def add_model_capabilities(device) -> None:
    """Expose only fields whose exact model's protocol is known."""
    sku = device.sku.upper()

    def prop(instance):
        device.capabilities.setdefault((PROPERTY, instance), {"type": PROPERTY, "instance": instance})

    def toggle(instance):
        device.capabilities.setdefault((TOGGLE, instance), {"type": TOGGLE, "instance": instance,
            "parameters": {"dataType": "ENUM", "options": [{"name": "Off", "value": 0}, {"name": "On", "value": 1}]}})

    if sku in MULTI_OUTLET_MODELS:
        for index in range(1, MULTI_OUTLET_MODELS[sku] + 1):
            toggle(f"socketToggle{index}")
    if sku in MASKED_RING_MODELS:
        if sku == "H1252":
            toggle("nativeMainPanel")
            toggle("nativeRing")
            toggle("mainLightToggle")
            toggle("backgroundLightToggle")
        # Extend only the native addressability, never the cloud schema.
        device.local_settings["masked_ring"] = True
    if sku == "H7073" and device.app_data.get("topic"):
        device.capabilities.setdefault((COLOR, "colorRgb"), {"type": COLOR, "instance": "colorRgb",
            "parameters": {"dataType": "INTEGER", "range": {"min": 0, "max": 0xFFFFFF}}})
    if sku == "H5086":
        for name in ("voltage", "current", "power", "energyToday", "powerFactor", "onTime"):
            prop(name)
    if sku in {"H7152", "H5106"} | THERMOMETER_MODELS:
        prop("sensorTemperature")
        if sku not in {"H5310", "H5111"}:
            prop("sensorHumidity")
    if sku == "H7152":
        prop("pumpFault")
        prop("drainMode")
    if sku == "H5106":
        prop("pm25")
    if sku == "H5112":
        prop("sensorTemperature2")
    for probe in range(1, PROBE_MODELS.get(sku, 0) + 1):
        prop(f"probe{probe}CoreTemperature")
        prop(f"probe{probe}AmbientTemperature")
    if sku in THERMOMETER_MODELS or sku in PROBE_MODELS or sku == "H5106":
        if device.kind == "private_app":
            device.kind = "sensor"
    # Keep private app plugs classified as sockets, not generic devices.
    if sku in MULTI_OUTLET_MODELS or sku == "H5086":
        device.kind = "socket"


def native_outlet_value(device, instance, value):
    """MQTT's addressed bitmask is not the REST master-power value."""
    count = MULTI_OUTLET_MODELS.get(device.sku.upper(), 0)
    if not count or (TOGGLE, instance) in device.cloud_capabilities:
        return None
    if instance == "powerSwitch":
        mask = (1 << count) - 1
        return (mask << 4) | (mask if value else 0)
    for index in range(count):
        if instance == f"socketToggle{index + 1}":
            bit = 1 << index
            return (bit << 4) | (bit if value else 0)
    return None


def apply_outlet_report(device, value) -> dict:
    if type(value) is not int or device.sku.upper() not in MULTI_OUTLET_MODELS:
        return {}
    count = MULTI_OUTLET_MODELS[device.sku.upper()]
    return {(TOGGLE, f"socketToggle{i + 1}"): int(bool(value & (1 << i))) for i in range(count)} | {
        (ON_OFF, "powerSwitch"): int(bool(value & ((1 << count) - 1)))}
