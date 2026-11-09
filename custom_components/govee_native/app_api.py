"""Govee Home private account transport.

Account/routes were recovered in an earlier 6.5.02 audit and corroborated
with independent protocol implementations. The supplied 7.7.11 base APK
is protected; its hidden lighting code is not claimed as decompiled here.
No app secrets are extracted or embedded. Sensitive responses are never logged.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import time
import uuid
from typing import Any

import aiohttp

APP_HOST = "https://app2.govee.com"
APP_VERSION = "7.4.10"  # Preserve the working account compatibility profile; not the supplied APK version.
APP_AGENT = f"GoveeHome/{APP_VERSION} (com.ihoment.GoVeeSensor; build:2; iOS 18.4.0) Alamofire/5.10.2"
LOGIN = "/account/rest/account/v2/login"
VERIFY = "/account/rest/account/v1/verification"
DEVICE_LIST = "/device/rest/devices/v1/list"
BFF_DEVICES = "/bff-app/v1/device/list"
IOT_KEY = "/app/v1/account/iot/key"
APP_SHORTCUTS = "/bff-app/v1/exec-plat/home"


class AppAPIError(Exception):
    """Private app API unavailable or rejected a request."""


class AppAuthenticationError(AppAPIError):
    """Credentials or session rejected by Govee."""


class AppVerificationRequired(AppAuthenticationError):
    """A short-lived email verification code must be submitted."""


@dataclass
class AppSession:
    token: str
    account_id: str = ""
    account_topic: str = ""


def stable_client_id(email: str) -> str:
    """Use stable ID to avoid repeated new-device 2FA prompts."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, email.strip().lower()).hex


def parse_response(raw: Any, *, login: bool = False) -> dict:
    """Reject HTTP-200-with-error envelopes; never treat auth failure as zero devices."""
    if not isinstance(raw, dict):
        raise AppAPIError("Malformed Govee app response")
    status = raw.get("status")
    if status is not None and str(status) not in ("200", "0"):
        if str(status) == "454":
            raise AppVerificationRequired("Govee email verification required or rejected")
        if str(status) in ("401", "403"):
            raise AppAuthenticationError("Govee account token rejected")
        raise AppAPIError(f"Govee app API rejected request: status {status}")
    return raw


class GoveeAppAPI:
    """Strict fixed-host client for the reverse-engineered account and BFF APIs."""

    def __init__(self, session: aiohttp.ClientSession, email: str, token: str = ""):
        self._http = session
        self.email = email.strip().lower()
        self.client_id = stable_client_id(email)
        self.session = AppSession(token=token)
        self._lock = asyncio.Lock()

    def _headers(self, auth: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "appVersion": APP_VERSION,
            "clientId": self.client_id,
            "clientType": "1",
            "iotVersion": "0",
            "timestamp": str(int(time.time() * 1000)),
            "User-Agent": APP_AGENT,
        }
        if auth:
            if not self.session.token:
                raise AppAuthenticationError("Govee account session is not authenticated")
            headers["Authorization"] = f"Bearer {self.session.token}"
        return headers

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      body: dict | None = None, auth: bool = True) -> dict:
        """Only allows URL paths under the fixed app2.govee.com host.

        Private endpoints sometimes return status=401 with HTTP 200.
        """
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path or ".." in path or "\\" in path:
            raise ValueError("Not a valid relative Govee API path")
        if method not in ("GET", "POST", "PUT", "DELETE"):
            raise ValueError("Unsupported method")
        if params and any(not isinstance(k, str) or isinstance(v, (dict, list)) for k, v in params.items()):
            raise ValueError("Only scalar query parameters are allowed")
        try:
            async with self._http.request(
                method, APP_HOST + path, headers=self._headers(auth),
                params=params, json=body if method != "GET" else None,
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise AppAuthenticationError("Govee private session expired")
                if response.status == 429:
                    raise AppAPIError("Govee private API rate limited")
                if response.status >= 300:
                    raise AppAPIError(f"Govee private API HTTP {response.status}")
                raw = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise AppAPIError(f"Govee private transport error: {type(exc).__name__}") from exc
        return parse_response(raw)

    async def login(self, password: str, code: str | None = None) -> AppSession:
        """2026 email-code login (APK endpoint + updated app headers)."""
        payload = {"email": self.email, "password": password, "client": self.client_id, "clientType": "1"}
        if code:
            payload["code"] = code.strip()
        async with self._lock:
            raw = await self.request("POST", LOGIN, body=payload, auth=False)
            info = raw.get("client") or raw.get("data") or {}
            if not isinstance(info, dict) or not info.get("token"):
                raise AppAuthenticationError("Govee login returned no account token")
            self.session = AppSession(
                token=str(info["token"]),
                account_id=str(info.get("accountId", "")),
                account_topic=str(info.get("topic", "")),
            )
            return self.session

    async def request_email_code(self) -> None:
        """Govee does not send the 454 challenge email until verification is called."""
        await self.request("POST", VERIFY, body={"type": 8, "email": self.email}, auth=False)

    async def devices(self) -> list[dict]:
        """Fetch BFF list first; legacy POST fallback; do not silently hide auth errors."""
        errors = []
        for method, route, body in (("GET", BFF_DEVICES, None), ("POST", DEVICE_LIST, {})):
            try:
                raw = await self.request(method, route, body=body)
                data = raw.get("data")
                rows = (data.get("devices") if isinstance(data, dict) else None)
                if rows is None:
                    rows = raw.get("devices")
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
                errors.append(f"{route}: missing devices array")
            except AppAuthenticationError:
                raise
            except AppAPIError as exc:
                errors.append(type(exc).__name__)
        raise AppAPIError("Could not get app devices from either confirmed endpoint: " + ", ".join(errors))

    async def mqtt_credentials(self) -> dict:
        """Read IoT TLS certificate material; only held in memory (never added to logs)."""
        result = await self.request("GET", IOT_KEY)
        data = result.get("data")
        if not isinstance(data, dict):
            raise AppAPIError("No IoT credential object")
        return data

    async def one_click_shortcuts(self) -> dict:
        """Fetch real Tap-to-Run MQTT packets from the authenticated APK BFF."""
        return await self.request("GET", APP_SHORTCUTS)

    async def groups(self) -> dict:
        """GET method and path verified in classes10.dex IScenesNet.getGroupList."""
        return await self.request("GET", "/account/rest/v2/groups-devices")

    async def lighting_scenes(self, sku: str, device: str, goods_type: int = 0) -> dict:
        """APK ScenesApi.getNewScenes; all query names verified from parameter annotations."""
        return await self.request("GET", "/bff-app/v1/devices/scenes/new", params={
            "sku": sku, "device": device, "updateTime": 0,
            "goodsType": goods_type, "sourceType": 0,
        })

    async def snapshots(self, sku: str, device: str, **kwargs: Any) -> dict:
        return await self.request("GET", "/bff-app/v1/devices/snapshots", params={"sku": sku, "device": device, **kwargs})

    async def diy(self, sku: str, device: str, *, request: dict | None = None) -> dict:
        """APK DiyMApi.getDiyGroupList is POST with RequestDiyGroupList body."""
        if request is None:
            raise ValueError("DIY request requires an APK-verified RequestDiyGroupList JSON body; do not guess")
        return await self.request("POST", "/bff-app/v1/diy/list", body=request)

    async def get_app_route(self, operation: str, query: dict | None = None,
                            body: dict | None = None, *, allow_write: bool = False) -> dict:
        """Execute explicitly requested APK-verified operation; no arbitrary URLs.

        Writes are opt-in and only for supported request bodies; absence of a
        tested schema is not a reason to guess how to change device settings.
        """
        from .app_routes import APP_ROUTE_INDEX
        spec = APP_ROUTE_INDEX.get(operation)
        if spec is None:
            raise ValueError("Unknown APK-verified app method")
        method, path, query_names, requires_body = spec
        if method not in ("GET", "POST") and not allow_write:
            raise ValueError("Private-app mutations disabled")
        if method == "POST" and not allow_write and operation not in READ_POST_OPERATIONS:
            raise ValueError("POST operation is not confirmed read-only")
        if query and set(query) - set(query_names):
            raise ValueError("Query contains a parameter absent from APK method signature")
        if requires_body and body is None:
            raise ValueError("APK method requires a JSON request body")
        return await self.request(method, path, params=query, body=body)


# Class method names checked in the APK; POST does not imply a mutation.
READ_POST_OPERATIONS = frozenset({"deviceList", "getDiyGroupList", "getDiySingleGroup", "getAllDiy", "getMyDiy"})


def normalize_app_device(raw: dict) -> dict:
    """Use actual BFF/legacy keys without guessing a device's controls."""
    ext = raw.get("deviceExt") or raw.get("device_ext") or {}
    if not isinstance(ext, dict):
        ext = {}
    identity = raw.get("device") or raw.get("deviceId") or raw.get("device_id") or raw.get("bleAddress")
    sku = raw.get("sku") or raw.get("model") or raw.get("productModel")
    if not identity or not sku:
        return {}
    settings = ext.get("deviceSettings") or ext.get("device_settings") or {}
    if isinstance(settings, str):
        try: settings = json.loads(settings)
        except (TypeError, ValueError): settings = {}
    if not isinstance(settings, dict):settings = {}
    last = ext.get("lastDeviceData") or ext.get("last_device_data") or raw.get("lastDeviceData") or {}
    if isinstance(last, str):
        try: last = json.loads(last)
        except (TypeError, ValueError): last = {}
    if not isinstance(last, dict):last = {}
    return {
        "device": str(identity), "sku": str(sku),
        "name": str(raw.get("deviceName") or raw.get("name") or sku),
        "topic": str(settings.get("topic") or raw.get("topic") or ""),
        "battery": raw.get("battery") if raw.get("battery") is not None else last.get("battery"),
        "fahrenheit": settings.get("fahOpen") if type(settings.get("fahOpen")) is bool else None,
        "last": last,
        "ble_address": str(settings.get("bleAddress") or raw.get("bleAddress") or ""),
        "ble_name": str(settings.get("bleName") or raw.get("bleName") or ""),
        "firmware": str(settings.get("versionSoft") or raw.get("versionSoft") or ""),
        "hardware": str(settings.get("versionHard") or raw.get("versionHard") or ""),
        "goods_type": raw.get("goodsType", settings.get("goodsType", 0)),
        "source": "govee_private_app",
    }
