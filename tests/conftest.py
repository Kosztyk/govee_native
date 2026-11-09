import sys
from pathlib import Path
import pytest_asyncio
from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.govee_native.coordinator import GoveeCoordinator
from custom_components.govee_native.models import Device
from custom_components.govee_native.app_models import discover_app_device
from custom_components.govee_native.h6199 import add_capabilities

DEVICE_ID = "10:20:30:40:50:60:70:80"


def make_device():
    d = Device.from_payload(discover_app_device({"device": DEVICE_ID, "sku": "H6199",
        "deviceName": "Backlight", "deviceExt": {"deviceSettings":
        '{"topic":"GD/test-owned","bleAddress":"AA:BB:CC:DD:EE:FF","versionSoft":"1.0"}'}}))
    add_capabilities(d)
    return d


@pytest_asyncio.fixture
async def coordinator(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    c = GoveeCoordinator(hass, None, 60)
    d = make_device()
    c.devices[d.device_id] = d
    c.data = dict(c.devices)
    from time import monotonic
    c._discovered = True
    c._last_discovery = monotonic()
    yield c
    await c.async_close()
    await hass.async_stop()
