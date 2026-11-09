"""Opt-in documented Govee LAN UDP discovery and verified light control.

This is the manufacturer's *published* LAN protocol, not the private app HTTP
backend. Commands are considered successful only when devStatus confirms them;
otherwise callers can fall back to authenticated cloud control.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from collections import defaultdict
from time import monotonic
from typing import Any, Callable

from .const import COLOR, ON_OFF, RANGE
from .models import Device

_LOGGER = logging.getLogger(__name__)
MULTICAST = ("239.255.255.250", 4001)
RECEIVE_PORT = 4002
CONTROL_PORT = 4003
SCAN = {"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}


def normalize_id(value: str) -> str:
    return value.upper().replace(":", "").replace("-", "")


def lan_command(kind: str, instance: str, value: Any) -> tuple[str, dict, str, Any] | None:
    """Return (LAN command, data, status key, expected state), if supported."""
    if kind == ON_OFF and instance == "powerSwitch" and value in (0, 1):
        return ("turn", {"value": value}, "onOff", value)
    if kind == RANGE and instance == "brightness" and isinstance(value, int) and 1 <= value <= 100:
        return ("brightness", {"value": value}, "brightness", value)
    if kind == COLOR and instance == "colorRgb" and isinstance(value, int) and 0 <= value <= 0xFFFFFF:
        rgb = {"r": value >> 16 & 255, "g": value >> 8 & 255, "b": value & 255}
        return ("colorwc", {"color": rgb, "colorTemInKelvin": 0}, "color", rgb)
    if kind == COLOR and instance == "colorTemperatureK" and isinstance(value, int) and 2000 <= value <= 9000:
        return ("colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": value}, "colorTemInKelvin", value)
    return None


def _safe_local_ip(value: Any) -> bool:
    try:
        ip = ipaddress.ip_address(value)
        return isinstance(ip, ipaddress.IPv4Address) and ip.is_private and not ip.is_loopback and not ip.is_link_local
    except (ValueError, TypeError):
        return False


class _LanProtocol(asyncio.DatagramProtocol):
    def __init__(self, handler: Callable[[bytes, tuple[str, int]], None]):
        self.handler = handler

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.handler(data, addr)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Govee LAN UDP error: %s", type(exc).__name__)


class GoveeLan:
    def __init__(self, devices: dict[str, Device], on_update: Callable[[], None]):
        self.devices = devices
        self._on_update = on_update
        self.transport: asyncio.DatagramTransport | None = None
        self.routes: dict[str, str] = {}  # normalized device -> vetted local IP
        self._waiters: dict[str, asyncio.Future[dict]] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_send: dict[str, float] = {}
        self._last_scan = 0.0
        self.last_seen: dict[str, float] = {}
        self.last_error: str | None = None

    async def async_start(self) -> bool:
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _LanProtocol(self._packet), local_addr=("0.0.0.0", RECEIVE_PORT)
            )
        except OSError as exc:
            # Port 4002 may already be owned by a LAN controller. Cloud still works.
            _LOGGER.warning("Govee LAN listener unavailable (UDP 4002): %s", exc)
            self.last_error = f"udp_4002_{type(exc).__name__}"
            return False
        self.transport = transport
        self.scan()
        return True

    def scan(self) -> None:
        if self.transport:
            self.transport.sendto(json.dumps(SCAN).encode("utf-8"), MULTICAST)
            self._last_scan = monotonic()

    def maybe_scan(self) -> None:
        if monotonic() - self._last_scan >= 600:
            self.scan()

    def available(self, device: Device) -> bool:
        last_seen = self.last_seen.get(normalize_id(device.device_id))
        return bool(self.transport and last_seen is not None and monotonic() - last_seen < 180)

    def query_status(self) -> None:
        if not self.transport:return
        for ip in set(self.routes.values()):
            self.transport.sendto(b'{"msg":{"cmd":"devStatus","data":{}}}', (ip, CONTROL_PORT))

    def _packet(self, raw: bytes, address: tuple[str, int]) -> None:
        if len(raw) > 8192:
            return
        try:
            envelope = json.loads(raw)
            msg = envelope.get("msg", {})
            if not isinstance(msg, dict):
                return
            data = msg.get("data", {})
            if not isinstance(data, dict):
                return
        except (ValueError, UnicodeDecodeError, AttributeError):
            return
        ip = address[0]
        if not _safe_local_ip(ip):
            return
        if msg.get("cmd") == "scan":
            if data.get("ip") != ip:
                return
            device_id = normalize_id(str(data.get("device", "")))
            device = next((d for d in self.devices.values() if normalize_id(d.device_id) == device_id), None)
            if device and data.get("sku") == device.sku:
                self.routes[device_id] = ip
                if self.transport:
                    self.transport.sendto(b'{"msg":{"cmd":"devStatus","data":{}}}', (ip, CONTROL_PORT))
            return
        if msg.get("cmd") != "devStatus":
            return
        device = next((d for d in self.devices.values() if self.routes.get(normalize_id(d.device_id)) == ip), None)
        if device is None:
            return
        states = {}
        if type(data.get("onOff")) is int and data["onOff"] in (0, 1):
            states[(ON_OFF, "powerSwitch")] = data["onOff"]
        if type(data.get("brightness")) is int and 0 <= data["brightness"] <= 100:
            states[(RANGE, "brightness")] = data["brightness"]
        color = data.get("color")
        if isinstance(color, dict) and all(isinstance(color.get(c), int) and 0 <= color[c] <= 255 for c in ("r", "g", "b")):
            rgb = color["r"] << 16 | color["g"] << 8 | color["b"]
            if rgb or not data.get("colorTemInKelvin"):
                states[(COLOR, "colorRgb")] = rgb
        kelvin = data.get("colorTemInKelvin")
        if type(kelvin) is int and (kelvin == 0 or 2000 <= kelvin <= 9000):
            states[(COLOR, "colorTemperatureK")] = kelvin
        if not states:return
        device.report({k:v for k,v in states.items() if device.has(*k)}, "lan")
        self.last_seen[normalize_id(device.device_id)] = monotonic()
        waiter = self._waiters.pop(ip, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(data)
        self._on_update()

    async def async_control(self, device: Device, kind: str, instance: str, value: Any) -> bool:
        """True only if the new state was read back; False triggers cloud fallback."""
        if device.kind != "light" or self.transport is None:
            return False
        destination = self.routes.get(normalize_id(device.device_id))
        cmd = lan_command(kind, instance, value)
        if destination is None or cmd is None or not self.available(device):
            return False
        name, data, state_field, expected = cmd
        async with self._locks[destination]:
            gap = monotonic() - self._last_send.get(destination, 0)
            if gap < 0.12:
                await asyncio.sleep(0.12 - gap)
            self.transport.sendto(json.dumps({"msg": {"cmd": name, "data": data}}).encode(), (destination, CONTROL_PORT))
            self._last_send[destination] = monotonic()
            await asyncio.sleep(0.12)
            loop = asyncio.get_running_loop()
            waiter: asyncio.Future[dict] = loop.create_future()
            self._waiters[destination] = waiter
            self.transport.sendto(b'{"msg":{"cmd":"devStatus","data":{}}}', (destination, CONTROL_PORT))
            try:
                status = await asyncio.wait_for(waiter, timeout=2)
                return status.get(state_field) == expected
            except asyncio.TimeoutError:
                return False
            finally:
                if self._waiters.get(destination) is waiter:
                    self._waiters.pop(destination, None)

    async def async_stop(self) -> None:
        if self.transport:
            self.transport.close()
            self.transport = None
        for waiter in self._waiters.values():
            if not waiter.done():
                waiter.cancel()
        self._waiters.clear()
