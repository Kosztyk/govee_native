"""Opt-in direct H6199 GATT writes through Home Assistant Bluetooth/proxies.

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

from .h6199 import SKU, WRITE_UUID, frame, valid_frame

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
        return bool(not self._stopped and device.sku == SKU and address
                    and monotonic() >= self._retry_after.get(device.device_id, 0)
                    and bluetooth.async_address_present(self.hass, address, connectable=True))

    async def async_write(self, device, frames: list[bytes]) -> bool:
        if not self.available(device):
            return False
        if not frames or any(not valid_frame(value) for value in frames):
            raise ValueError("Invalid H6199 Bluetooth frame")
        address = self.address(device)
        async with self._locks[address]:
            client = None
            try:
                ble_device = bluetooth.async_ble_device_from_address(self.hass, address, connectable=True)
                if ble_device is None:
                    return False
                client = await establish_connection(
                    BleakClientWithServiceCache, ble_device, device.name, max_attempts=2,
                    ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                        self.hass, address, connectable=True),
                )
                self._clients.add(client)
                characteristic = client.services.get_characteristic(WRITE_UUID)
                if characteristic is None:
                    raise ValueError("H6199 write characteristic is absent")
                response = "write" in characteristic.properties
                await client.write_gatt_char(characteristic, frame(0xAA, 0x01), response=response)
                for data in frames:
                    await asyncio.sleep(0.1)
                    await client.write_gatt_char(characteristic, data, response=response)
                self.writes += len(frames)
                self.failures.pop(device.device_id, None)
                self._retry_after.pop(device.device_id, None)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failures[device.device_id] = type(exc).__name__
                self._retry_after[device.device_id] = monotonic() + 60
                _LOGGER.debug("H6199 Bluetooth unavailable: %s; trying alternate transport", type(exc).__name__)
                return False
            finally:
                if client:
                    self._clients.discard(client)
                    try:
                        await client.disconnect()
                    except Exception:
                        _LOGGER.debug("Bluetooth disconnect failed after command")

    async def async_stop(self) -> None:
        self._stopped = True
        for client in tuple(self._clients):
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()
