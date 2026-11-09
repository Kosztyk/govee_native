"""Typed, conservative parser for Govee Home's APK Tap-to-Run shortcuts.

The authenticated /bff-app/v1/exec-plat/home response carries exact IoT
messages for the shortcuts the user created in the Govee app. Unlike scene
catalog IDs, these are executable, device-specific packets; don't invent
commands from the APK's scene names.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class OneClickCommand:
    topic: str
    packet: dict
    device_id: str


@dataclass(frozen=True)
class OneClickShortcut:
    key: str
    name: str
    commands: tuple[OneClickCommand, ...]
    device_ids: tuple[str, ...]


def decode_iot_message(raw: Any) -> dict | None:
    """Validate an APK-provided MQTT command envelope without generating one."""
    if isinstance(raw, str):
        if len(raw) > 32768:
            return None
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict) or not isinstance(raw.get("msg"), dict):
        return None
    msg = raw["msg"]
    if not isinstance(msg.get("cmd"), str) or not msg["cmd"] or len(msg["cmd"]) > 80:
        return None
    if msg.get("type") != 1 or not isinstance(msg.get("data"), dict):
        return None
    try:
        payload = json.dumps(raw, separators=(",", ":"))
    except (ValueError, TypeError):
        return None
    return raw if len(payload.encode("utf-8")) <= 32768 else None


def parse_one_click_shortcuts(raw: dict, allowed_devices: dict[str, str]) -> dict[str, OneClickShortcut]:
    """Return executable shortcuts targeting known devices from this account.

    `allowed_devices` maps the currently discovered device topics to device IDs.
    If even one MQTT operation is malformed or points outside those topics, do
    not expose a partially functional shortcut that could control only a subset
    of its devices. BLE-only/automation-only rules are not MQTT shortcuts.
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    groups = data.get("components") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return {}
    shortcuts: dict[str, OneClickShortcut] = {}
    owned_ids = set(allowed_devices.values())
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "Home").strip()[:90]
        clicks = group.get("oneClicks")
        if not isinstance(clicks, list):
            continue
        for click in clicks:
            if not isinstance(click, dict) or not str(click.get("name") or "").strip():
                continue
            rules = click.get("iotRules")
            if not isinstance(rules, list) or not rules:
                continue
            commands: list[OneClickCommand] = []
            valid = True
            for item in rules:
                device_info = item.get("deviceObj") if isinstance(item, dict) else None
                topic = device_info.get("topic") if isinstance(device_info, dict) else None
                operations = item.get("rule") if isinstance(item, dict) else None
                linked_id = device_info.get("device") if isinstance(device_info, dict) else None
                # Shortcut packets sometimes refer to a secondary topic for
                # the same device. Trust that topic only if the private BFF
                # explicitly identifies a device already found in this account.
                owned_id = allowed_devices.get(topic) if isinstance(topic, str) else None
                if owned_id is None and isinstance(linked_id, str) and linked_id in owned_ids:
                    owned_id = linked_id
                if (not isinstance(topic, str) or not topic or len(topic) > 512
                        or any(c in topic for c in ("#", "+", "\\"))
                        or owned_id is None or not isinstance(operations, list) or not operations):
                    valid = False
                    break
                for operation in operations:
                    packet = decode_iot_message(operation.get("iotMsg") if isinstance(operation, dict) else None)
                    if packet is None:
                        valid = False
                        break
                    commands.append(OneClickCommand(topic, packet, owned_id))
                if not valid:
                    break
            if not valid or not commands or len(commands) > 40:
                continue
            # Stable numeric ID from APK when provided, otherwise name hash.
            rid = click.get("siriEngineId") or click.get("presetId")
            identity = f"{group.get('componentId', 0)}:{rid or click['name']}"
            key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:22]
            if key in shortcuts:
                continue
            device_ids = tuple(dict.fromkeys(op.device_id for op in commands))
            shortcuts[key] = OneClickShortcut(
                key=key, name=f"{group_name} — {str(click['name']).strip()[:90]}",
                commands=tuple(commands), device_ids=device_ids,
            )
    return shortcuts
