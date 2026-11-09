"""Opt-in reusable H6199/H1270 GATT connections through Home Assistant Bluetooth/proxies.

No independent scanner, guessed MAC, or command-success claim. GATT write
acceptance is reported as unconfirmed; later device status can confirm it.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
import logging
import re
from time import monotonic

from homeassistant.components import bluetooth
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .h6199 import WRITE_UUID, NOTIFY_UUID, frame, valid_frame
from .native_protocol import BLE_MODELS
from .ble_crypto import async_establish_session, async_supports_encryption

_LOGGER = logging.getLogger(__name__)
MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")


def valid_address(value: str) -> bool:
    return isinstance(value, str) and MAC.fullmatch(value) is not None


class GoveeBluetooth:
    def __init__(self, hass, overrides: dict | None = None):
        self.hass = hass
        self.overrides = overrides or {}
        self._locks = defaultdict(asyncio.Lock)
        self._clients = set()
        self._connections = {}
        self._sessions = {}
        self._last_write = {}
        self._stopped = False
        self.failures: dict[str, str] = {}
        self._retry_after: dict[str, float] = {}
        self.writes = 0

    def address(self, device) -> str | None:
        value = self.overrides.get(device.device_id) or device.app_data.get("ble_address")
        # Cloud device IDs are often EIGHT bytes. They are not Bluetooth MACs.
        return value.upper() if valid_address(value) else None

    def available(self, device) -> bool:
        address = self.address(device)
        return bool(not self._stopped and device.sku.upper() in BLE_MODELS and address
                    and monotonic() >= self._retry_after.get(device.device_id, 0)
                    and bluetooth.async_address_present(self.hass, address, connectable=True))

    async def async_write(self, device, frames: list[bytes]) -> bool:
        if not self.available(device):
            return False
        if not frames or any(not valid_frame(value) for value in frames):
            raise ValueError("Invalid Govee Bluetooth frame")
        address = self.address(device)
        async with self._locks[address]:
            if self._stopped:
                return False
            client = None
            try:
                ble_device = bluetooth.async_ble_device_from_address(self.hass, address, connectable=True)
                if ble_device is None:
                    return False
                client = self._connections.get(address)
                if client is None or not client.is_connected:
                    if client:
                        await self._disconnect(address, client)
                    client = await establish_connection(
                        BleakClientWithServiceCache, ble_device, device.name, max_attempts=2,
                        ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                            self.hass, address, connectable=True),
                    )
                    self._clients.add(client)
                    self._connections[address] = client
                    if await async_supports_encryption(client):
                        self._sessions[address] = await async_establish_session(client, WRITE_UUID, NOTIFY_UUID)
                    elif device.sku.upper() == "H1270":
                        raise ValueError("H1270 requires an encrypted connection")
                    else:
                        await client.write_gatt_char(WRITE_UUID, frame(0xAA, 0x01), response=False)
                        self._last_write[address] = monotonic()
                characteristic = client.services.get_characteristic(WRITE_UUID)
                if characteristic is None:
                    raise ValueError("Govee write characteristic is absent")
                response = "write" in characteristic.properties
                for data in frames:
                    gap = monotonic() - self._last_write.get(address, 0)
                    if gap < 0.1:
                        await asyncio.sleep(0.1 - gap)
                    session = self._sessions.get(address)
                    if session:
                        data = session.wrap(data)
                    await client.write_gatt_char(characteristic, data, response=response)
                    self._last_write[address] = monotonic()
                self.writes += len(frames)
                self.failures.pop(device.device_id, None)
                self._retry_after.pop(device.device_id, None)
                return True
            except asyncio.CancelledError:
                if client:
                    await self._disconnect(address, client)
                raise
            except Exception as exc:
                self.failures[device.device_id] = type(exc).__name__
                self._retry_after[device.device_id] = monotonic() + 60
                _LOGGER.debug("Govee Bluetooth unavailable: %s; trying another transport", type(exc).__name__)
                if client:
                    await self._disconnect(address, client)
                return False

    async def _disconnect(self, address, client):
        self._connections.pop(address, None)
        self._sessions.pop(address, None)
        self._last_write.pop(address, None)
        self._clients.discard(client)
        try:
            await client.disconnect()
        except Exception:
            _LOGGER.debug("Bluetooth disconnect failed")

    async def async_stop(self) -> None:
        self._stopped = True
        for address, client in list(self._connections.items()):
            async with self._locks[address]:
                await self._disconnect(address, client)
