"""Documented Govee OpenAPI transport, independent of Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from time import monotonic
from typing import Any
from uuid import uuid4
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time

import aiohttp

from .const import API_BASE

_LOGGER = logging.getLogger(__name__)


class GoveeError(Exception):
    """Govee request rejected or unavailable."""


class GoveeAuthError(GoveeError):
    """Invalid or revoked API key."""


class GoveeRateLimit(GoveeError):
    """Govee request limit reached; slow down."""

    def __init__(self, message="Govee request limit reached", retry_after=60):
        super().__init__(message)
        self.retry_after = retry_after


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
        self.daily_budget = 9000
        self.requests_today = 0
        self._request_day = int(time.time() // 86400)
        self._backoff_until = 0.0
        self.rate_remaining = None
        self.rate_reset = None
        self.on_request = None

    def _reset_day(self):
        day = int(time.time() // 86400)
        if day != self._request_day:
            self._request_day = day
            self.requests_today = 0

    @property
    def retry_after(self):
        return max(0.0, self._backoff_until - time.monotonic())

    def can_poll(self):
        self._reset_day()
        return (self.requests_today < self.daily_budget and self.retry_after == 0
                and (self.rate_remaining is None or self.rate_remaining > 5
                     or (self.rate_reset is not None and time.time() >= self.rate_reset)))

    def poll_interval(self, base, devices):
        """Spend the remaining daily read budget evenly, retaining a write reserve."""
        self._reset_day()
        remaining = self.daily_budget - self.requests_today
        until_reset = 86400 - time.time() % 86400 + 5
        if self.retry_after:
            return max(base, math.ceil(self.retry_after))
        if self.rate_remaining is not None and self.rate_remaining <= 5 and self.rate_reset:
            return max(base, math.ceil(self.rate_reset - time.time()))
        if remaining <= 0:
            return max(base, math.ceil(until_reset))
        return max(base, min(3600, math.ceil(until_reset * devices / max(1, remaining))))

    def budget_snapshot(self):
        self._reset_day()
        return {"day": self._request_day, "requests": self.requests_today}

    def restore_budget(self, data):
        if (isinstance(data, dict) and data.get("day") == self._request_day
                and type(data.get("requests")) is int and data["requests"] >= 0):
            self.requests_today = max(self.requests_today, data["requests"])

    def _response_limits(self, headers, limited=False):
        """Honor vendor header variants without trusting an unbounded wait."""
        for header, attr in (("X-RateLimit-Remaining", "rate_remaining"), ("X-RateLimit-Reset", "rate_reset")):
            try:
                value = float(headers.get(header, ""))
                if math.isfinite(value) and value >= 0:
                    if attr == "rate_reset" and value <= 86400:
                        value += time.time()
                    setattr(self, attr, value)
            except (TypeError, ValueError):
                pass
        if self.rate_remaining is not None and self.rate_remaining <= 5 and self.rate_reset is None:
            self.rate_reset = time.time() + 60
        if limited:
            raw = headers.get("Retry-After", "60")
            try:
                delay = float(raw)
            except (TypeError, ValueError):
                try:
                    delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = 60
            if not math.isfinite(delay) or delay <= 0:
                delay = 60
            self._backoff_until = time.monotonic() + min(3600, delay)

    async def _request(self, method: str, endpoint: str, payload: dict | None = None) -> dict:
        if self.retry_after:
            raise GoveeRateLimit(retry_after=self.retry_after)
        headers = {"Govee-API-Key": self._key, "Content-Type": "application/json"}
        body = {"requestId": str(uuid4()), "payload": payload} if payload is not None else None
        async with self._request_sem:
            # Recheck after waiting for another request's response/backoff.
            if self.retry_after:
                raise GoveeRateLimit(retry_after=self.retry_after)
            if endpoint == "device/state" and not self.can_poll():
                raise GoveeRateLimit("Cloud read budget is reserved for control")
            self._reset_day()
            self.requests_today += 1
            if self.on_request:
                self.on_request()
            try:
                async with self._session.request(
                    method, f"{API_BASE}/{endpoint}", headers=headers, json=body,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    self._response_limits(response.headers, response.status == 429)
                    if response.status in (401, 403):
                        raise GoveeAuthError("Govee API key is invalid or unauthorized")
                    if response.status == 429:
                        raise GoveeRateLimit(retry_after=self.retry_after)
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
            self._response_limits({}, True)
            raise GoveeRateLimit(retry_after=self.retry_after)
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
