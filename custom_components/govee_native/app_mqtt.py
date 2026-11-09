"""Govee Android-account AWS IoT MQTT control and state updates.

Protocol validated against independently reverse-engineered Govee Home clients.
Not the Govee Developer API event broker. No certificate is logged or stored.
"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
from pathlib import Path
import ssl
import tempfile
import time
from typing import Any, Callable

from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, pkcs12

_LOGGER = logging.getLogger(__name__)


def mqtt_packet(cmd: str, data: dict | None = None, *, cmd_version: int = 0, query: bool = False,
                account_topic: str = "") -> dict:
    """Build Govee Home MQTT envelope: query type=0; write type=1."""
    msg = {"cmd": cmd, "cmdVersion": cmd_version,
           "transaction": f"v_{time.time_ns() // 1000}", "type": 0 if query else 1}
    if account_topic:
        msg["accountTopic"] = account_topic
    if not query:
        msg["data"] = data or {}
        msg["origin"] = 32
    return {"msg": msg}


def mqtt_control(kind: str, instance: str, value: Any, sku: str) -> tuple[str, dict, int] | None:
    """Only model-independent commands with verified packet shapes are sent."""
    if kind == "devices.capabilities.on_off" and instance == "powerSwitch":
        power = bool(value)
        return "turn", {"val": (17 if power else 16) if sku in ("H5080", "H5083") else int(power)}, 0
    if kind == "devices.capabilities.range" and instance == "brightness":
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 <= value <= 100:
            raise ValueError("Invalid brightness")
        return "brightness", {"val": max(1, min(100, round(value)))}, 0
    if kind == "devices.capabilities.color_setting" and instance == "colorRgb":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFF:
            raise ValueError("Invalid RGB color")
        return "colorwc", {"color": {"r": (value >> 16) & 255, "g": (value >> 8) & 255, "b": value & 255}, "colorTemInKelvin": 0}, 0
    if kind == "devices.capabilities.color_setting" and instance == "colorTemperatureK":
        if type(value) is not int or not 2000 <= value <= 9000:
            raise ValueError("Invalid Kelvin temperature")
        return "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": value}, 0
    return None


def _ssl_context_sync(credentials: dict) -> ssl.SSLContext:
    """Load PKCS12/PEM in temporary 0600 files then immediately delete files."""
    cert, key = credentials.get("certificatePem"), credentials.get("privateKey")
    if not cert or not key:
        b64 = credentials.get("p12")
        if not b64:
            raise ValueError("IoT response has no client certificate")
        decoded = base64.b64decode(b64)
        passphrase = credentials.get("p12Pass") or credentials.get("p12_pass") or ""
        priv, certificate, others = pkcs12.load_key_and_certificates(decoded, passphrase.encode())
        if not priv or not certificate:
            raise ValueError("IoT PKCS12 contains no key/certificate")
        cert = certificate.public_bytes(Encoding.PEM).decode() + "".join(c.public_bytes(Encoding.PEM).decode() for c in (others or []))
        key = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory(prefix="govee-native-") as td:
        certpath, keypath = Path(td)/"cert.pem", Path(td)/"key.pem"
        certpath.touch(mode=0o600)
        keypath.touch(mode=0o600)
        certpath.write_text(cert)
        keypath.write_text(key)
        ctx.load_cert_chain(str(certpath), str(keypath))
    return ctx


class GoveeAppMqtt:
    def __init__(self, credentials: dict, account_topic: str, account_id: str,
                 client_id: str, callback: Callable[[dict], None],
                 connection_callback: Callable[[bool], None] | None = None):
        self.credentials = credentials
        self.account_topic = account_topic
        self.identifier = f"AP/{account_id}/a_{client_id}" if account_id else f"a_{client_id}"
        self.callback = callback
        self.connection_callback = connection_callback
        self._task: asyncio.Task | None = None
        self._client: Any = None
        self._stopping = False
        self.connected = False
        self.messages_received = 0
        self.last_message_at = None
        self.commands_published = 0
        self.last_command_at = None
        self.last_command_name = None
        self._context: ssl.SSLContext | None = None
        self._publish_lock = asyncio.Lock()
        self._last_publish = 0.0
        self.last_error: str | None = None

    async def start(self) -> None:
        if not self.account_topic:
            raise ValueError("Private app account topic not returned by Govee")
        self._context = await asyncio.to_thread(_ssl_context_sync, self.credentials)
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="govee_native_app_mqtt")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
        self._task = None
        self.connected = False
        self._client = None

    async def _run(self) -> None:
        import aiomqtt
        delay = 5
        endpoint = self.credentials.get("endpoint", "aqm3wd1qlc3dy-ats.iot.us-east-1.amazonaws.com")
        while not self._stopping:
            try:
                async with aiomqtt.Client(hostname=endpoint, port=8883, identifier=self.identifier,
                                          tls_context=self._context, keepalive=60) as client:
                    grant = await client.subscribe(self.account_topic, qos=1)
                    # MQTT 3.1.1 SUBACK 0x80 means policy denied, even though
                    # the underlying broker connection remains established.
                    if subscription_refused(grant):
                        raise ConnectionError("Govee account MQTT subscription was denied")
                    self._client = client
                    self.connected = True
                    self.last_error = None
                    if self.connection_callback:
                        self.connection_callback(True)
                    delay = 5
                    async for message in client.messages:
                        try:
                            # Some older Govee firmware embeds Latin-1 scene names in JSON.
                            value = json.loads(bytes(message.payload).decode("utf-8", errors="replace"))
                            if isinstance(value, dict):
                                value["_govee_topic"] = str(message.topic)
                                self.messages_received += 1
                                self.last_message_at = time.time()
                                try:
                                    self.callback(value)
                                except Exception as exc:
                                    _LOGGER.debug("Ignored app state callback error: %s", type(exc).__name__)
                        except (ValueError, TypeError):
                            _LOGGER.debug("Ignored invalid app IoT message")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                _LOGGER.warning("Govee account IoT unavailable: %s", type(exc).__name__)
            finally:
                self._client = None
                self.connected = False
                if self.connection_callback:
                    self.connection_callback(False)
            if not self._stopping:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def publish_app_packet(self, topic: str, packet: dict) -> None:
        """Send an unmodified, authenticated Govee Home Tap-to-Run IoT packet.

        The packet comes from Govee's own shortcut catalog. Its structure is
        validated before publication and the target topic is restricted by
        the shortcut parser to the account's discovered device topics.
        """
        from .one_click import decode_iot_message
        if not self.connected or not self._client:
            raise ConnectionError("Govee account MQTT is disconnected")
        if not isinstance(topic, str) or not topic or len(topic) > 512 or any(c in topic for c in ("#", "+", "\\")):
            raise ValueError("Invalid Govee device MQTT topic")
        checked = decode_iot_message(packet)
        if checked is None:
            raise ValueError("Govee Tap-to-Run command is malformed")
        checked = {**checked, "msg": {**checked["msg"], "accountTopic": self.account_topic,
                   "transaction": f"v_{time.time_ns() // 1000}", "origin": 32}}
        await self._publish(topic, checked, qos=1)
        self.commands_published += 1
        self.last_command_at = time.time()
        self.last_command_name = "app_one_click"

    async def publish(self, topic: str, cmd: str, data: dict | None = None,
                      *, cmd_version: int = 0, query: bool = False) -> None:
        if not self.connected or not self._client:
            raise ConnectionError("Govee account MQTT is disconnected")
        if not isinstance(topic, str) or not topic or len(topic) > 512 or any(c in topic for c in ("#", "+", "\\")):
            raise ValueError("No valid device MQTT topic")
        payload = mqtt_packet(cmd, data, cmd_version=cmd_version, query=query, account_topic=self.account_topic)
        await self._publish(topic, payload, qos=0 if query else 1)
        if not query:
            self.commands_published += 1
            self.last_command_at = time.time()
            self.last_command_name = cmd

    async def _publish(self, topic: str, payload: dict, *, qos: int) -> None:
        # Keep command/query ordering deterministic and avoid firmware drops
        # when a slider, segment sequence, and status poll arrive together.
        async with self._publish_lock:
            elapsed = time.monotonic() - self._last_publish
            if elapsed < 0.15:
                await asyncio.sleep(0.15 - elapsed)
            if not self.connected or not self._client:
                raise ConnectionError("Govee account MQTT disconnected before publication")
            await self._client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=qos, timeout=10)
            self._last_publish = time.monotonic()


def subscription_refused(grant: Any) -> bool:
    """Handle aiomqtt/paho result variants and denied SUBACK codes."""
    if isinstance(grant, (list, tuple)):
        return any(subscription_refused(item) for item in grant)
    if isinstance(grant, int):
        return grant == 0x80
    name = getattr(grant, 'name', None)
    if isinstance(name, str) and ('FAIL' in name.upper() or 'NOT_AUTHORIZED' in name.upper()):
        return True
    value = getattr(grant, 'value', None)
    if isinstance(value, int) and value >= 128:
        return True
    return False
