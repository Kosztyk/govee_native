"""Documented Govee OpenAPI transport, independent of Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from time import monotonic
from typing import Any
from uuid import uuid4

import aiohttp

from .const import API_BASE

_LOGGER = logging.getLogger(__name__)


class GoveeError(Exception):
    """Govee request rejected or unavailable."""


class GoveeAuthError(GoveeError):
    """Invalid or revoked API key."""


class GoveeRateLimit(GoveeError):
    """Govee request limit reached; slow down."""


class GoveeAPI:
    """Async client with fixed host and redacted errors."""

    def __init__(self, session: aiohttp.ClientSession, key: str):
        self._session = session
        self._key = key
        self._device_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_control: dict[str, float] = {}
        self._request_sem = asyncio.Semaphore(4)
        self._account_control_lock = asyncio.Lock()
        self._last_account_control = 0.0

    async def _request(self, method: str, endpoint: str, payload: dict | None = None) -> dict:
        headers = {"Govee-API-Key": self._key, "Content-Type": "application/json"}
        body = {"requestId": str(uuid4()), "payload": payload} if payload is not None else None
        async with self._request_sem:
            try:
                async with self._session.request(
                    method, f"{API_BASE}/{endpoint}", headers=headers, json=body,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status in (401, 403):
                        raise GoveeAuthError("Govee API key is invalid or unauthorized")
                    if response.status == 429:
                        raise GoveeRateLimit("Govee API rate limit reached")
                    if response.status >= 400:
                        raise GoveeError(f"Govee HTTP {response.status}")
                    result = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                raise GoveeError(f"Govee request failed: {type(exc).__name__}") from exc
        if not isinstance(result, dict):
            raise GoveeError("Malformed API response")
        code = result.get("code", 200)
        if str(code) in ("401", "403"):
            raise GoveeAuthError("Govee API key rejected")
        if str(code) == "429":
            raise GoveeRateLimit("Govee API rate limit reached")
        if str(code) != "200":
            # Don't interpolate a remote message: it may contain private request data.
            raise GoveeError(f"Govee API rejected request with code {code}")
        return result

    async def devices(self) -> list[dict]:
        result = await self._request("GET", "user/devices")
        devices = result.get("data")
        if not isinstance(devices, list):
            raise GoveeError("Device discovery did not return a device list")
        return devices

    async def state(self, sku: str, device: str) -> dict:
        result = await self._request("POST", "device/state", {"sku": sku, "device": device})
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise GoveeError("Govee state response missing payload")
        return payload

    async def scenes(self, sku: str, device: str, diy: bool = False) -> dict:
        endpoint = "device/diy-scenes" if diy else "device/scenes"
        result = await self._request("POST", endpoint, {"sku": sku, "device": device})
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise GoveeError("Govee scene response missing payload")
        return payload

    async def control(self, sku: str, device: str, kind: str, instance: str, value: Any) -> None:
        """Serialize per-device writes to respect the documented 2/sec cap."""
        async with self._device_locks[device]:
            previous = self._last_control.get(device, 0)
            elapsed = monotonic() - previous
            if elapsed < 0.6:
                await asyncio.sleep(0.6 - elapsed)
            async with self._account_control_lock:
                # Under Govee's 12 writes/second/account sustained quota.
                account_gap = monotonic() - self._last_account_control
                if account_gap < 0.11:
                    await asyncio.sleep(0.11 - account_gap)
                self._last_control[device] = monotonic()
                self._last_account_control = monotonic()
            await self._request("POST", "device/control", {
                "sku": sku,
                "device": device,
                "capability": {"type": kind, "instance": instance, "value": value},
            })
