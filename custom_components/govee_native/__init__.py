"""Govee Native by Kosztyk — mobile-app account API + IoT + optional OpenAPI."""
from __future__ import annotations
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import GoveeAPI
from .app_api import GoveeAppAPI, AppAPIError, AppAuthenticationError, AppVerificationRequired
from .app_mqtt import GoveeAppMqtt
from .const import (
    CONF_API_KEY,CONF_EMAIL,CONF_PASSWORD,CONF_APP_TOKEN,CONF_APP_ACCOUNT_ID,
    CONF_APP_ACCOUNT_TOPIC,CONF_POLL_SECONDS,CONF_MQTT,CONF_LAN,
    CONF_APP_MQTT,CONF_APP_CATALOGS,CONF_PRIVATE_MUTATIONS,
    DEFAULT_POLL_SECONDS,DOMAIN,PLATFORMS,SEGMENT,CONF_USE_LEGACY_API,
    CONF_BLUETOOTH,CONF_PREFER_BLUETOOTH,CONF_BLUETOOTH_ADDRESSES,
    CONF_H6199_PROFILE,
)
from .coordinator import GoveeCoordinator
from .mqtt_events import GoveeEvents
from .lan import GoveeLan
from .bluetooth import GoveeBluetooth
from .inventory import InventoryCache

_LOGGER=logging.getLogger(__name__)


def _entry_coordinator(hass: HomeAssistant, device_id: str):
    for c in hass.data.get(DOMAIN,{}).values():
        if device_id in c.devices:return c,c.devices[device_id]
    raise HomeAssistantError("Unknown Govee device ID")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    http=async_get_clientsession(hass)
    key=entry.data.get(CONF_API_KEY) or None
    if not key and entry.options.get(CONF_USE_LEGACY_API,False):
        # Explicit opt-in: reuse the user's already configured Developer API
        # key without duplicating it in this entry or exposing it in logs.
        for legacy in hass.config_entries.async_entries("govee"):
            key=legacy.options.get(CONF_API_KEY) or legacy.data.get(CONF_API_KEY) or None
            if key:
                break
        if not key:
            _LOGGER.warning("Govee legacy-key fallback enabled but no existing Govee API key is configured")
    api=GoveeAPI(http,key) if key else None
    email=entry.data.get(CONF_EMAIL) or None
    app=GoveeAppAPI(http,email,entry.data.get(CONF_APP_TOKEN,"")) if email else None
    if app and not app.session.token:
        try:
            login=await app.login(entry.data[CONF_PASSWORD])
        except (AppVerificationRequired,AppAuthenticationError) as exc:
            raise ConfigEntryAuthFailed("Govee account needs email verification; reauthenticate") from exc
        except AppAPIError as exc:
            raise ConfigEntryNotReady("Could not authenticate private Govee account") from exc
        else:
            hass.config_entries.async_update_entry(entry,data={**entry.data,
                CONF_APP_TOKEN:login.token,CONF_APP_ACCOUNT_ID:login.account_id,
                CONF_APP_ACCOUNT_TOPIC:login.account_topic})
    coordinator=GoveeCoordinator(hass,api,entry.options.get(CONF_POLL_SECONDS,DEFAULT_POLL_SECONDS),app,entry)
    coordinator.h6199_profile = entry.options.get(CONF_H6199_PROFILE, "classic")
    if entry.options.get(CONF_BLUETOOTH, False):
        coordinator.ble = GoveeBluetooth(hass, entry.options.get(CONF_BLUETOOTH_ADDRESSES, {}))
        coordinator.prefer_bluetooth = entry.options.get(CONF_PREFER_BLUETOOTH, False)
    coordinator.inventory_cache = InventoryCache(hass, entry.entry_id)
    coordinator.allow_cached_inventory = bool(coordinator.ble or entry.options.get(CONF_LAN, False))
    if coordinator.allow_cached_inventory:
        for cached in (await coordinator.inventory_cache.load()).values():
            coordinator._merge_device(cached)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:raise
    except Exception as exc:
        raise ConfigEntryNotReady(f"Govee device discovery failed: {type(exc).__name__}") from exc
    hass.data.setdefault(DOMAIN,{})[entry.entry_id]=coordinator
    # Real APK account push and native device MQTT writes.
    if coordinator.app and not coordinator.app_unavailable and entry.options.get(CONF_APP_MQTT,True):
        try:
            iot=await app.mqtt_credentials()
            mqtt=GoveeAppMqtt(iot,entry.data.get(CONF_APP_ACCOUNT_TOPIC,""),
                              entry.data.get(CONF_APP_ACCOUNT_ID,""),app.client_id,
                              coordinator.mqtt_update,coordinator.mqtt_connection_changed)
            await mqtt.start()
            coordinator.mqtt=mqtt
        except (AppAPIError,ValueError,ImportError) as exc:
            _LOGGER.warning("Govee private AWS IoT unavailable, remaining platforms work: %s",type(exc).__name__)
    if entry.options.get(CONF_LAN,False):
        lan=GoveeLan(coordinator.devices,lambda:coordinator.async_set_updated_data(dict(coordinator.devices)))
        coordinator.lan = lan
        try:
            await lan.async_start()
        except Exception as exc:
            _LOGGER.debug("Govee LAN discovery unavailable: %s",type(exc).__name__)
    if coordinator.app and not coordinator.app_unavailable and entry.options.get(CONF_APP_CATALOGS,True):
        # Collect shortcuts BEFORE scene/select/light entities are constructed.
        # A catalog failure is non-fatal; light controls must still load.
        await coordinator.async_load_private_catalogs()
    await hass.config_entries.async_forward_entry_setups(entry,PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    if coordinator.api and entry.options.get(CONF_MQTT,False) and any(
        kind=="devices.capabilities.event" for d in coordinator.devices.values() for kind,_ in d.capabilities
    ):
        events=GoveeEvents(hass,coordinator,key)
        try:await events.async_start()
        except Exception as exc:_LOGGER.warning("Official MQTT events unavailable: %s",type(exc).__name__)
        else:hass.data.setdefault(f"{DOMAIN}_events",{})[entry.entry_id]=events
    _register_services(hass)
    return True


def _register_services(hass:HomeAssistant)->None:
    async def segments(call:ServiceCall):
        c,d=_entry_coordinator(hass,call.data["device_id"])
        if ("rgb" in call.data)==("brightness" in call.data):raise HomeAssistantError("Exactly one of rgb or brightness is required")
        color="rgb" in call.data
        await c.async_control(d,SEGMENT,"segmentedColorRgb" if color else "segmentedBrightness",
            {"segment":call.data["segments"],"rgb" if color else "brightness":call.data["rgb" if color else "brightness"]})

    async def control(call:ServiceCall):
        c,d=_entry_coordinator(hass,call.data["device_id"])
        await c.async_control(d,call.data["capability_type"],call.data["instance"],call.data["value"])

    async def scenes(call:ServiceCall):
        c,d=_entry_coordinator(hass,call.data["device_id"])
        await c.async_load_scenes(d)

    async def app_request(call:ServiceCall):
        entry_id=call.data.get("entry_id")
        candidates=[c for eid,c in hass.data.get(DOMAIN,{}).items() if c.app and (not entry_id or eid==entry_id)]
        if len(candidates)!=1:
            raise HomeAssistantError("Specify entry_id when multiple Govee app accounts exist")
        c=candidates[0]
        allow_write=next((hass.config_entries.async_get_entry(eid).options.get(CONF_PRIVATE_MUTATIONS,False)
            for eid,other in hass.data.get(DOMAIN,{}).items() if other is c),False)
        try:
            data=await c.app.get_app_route(call.data["operation"],call.data.get("query"),
                 call.data.get("body"),allow_write=allow_write)
        except (AppAPIError,ValueError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        # Service-response allows manual inspection without exposing account token.
        return {"result":data}

    async def refresh_app(call:ServiceCall):
        for eid,coordinator in list(hass.data.get(DOMAIN,{}).items()):
            if coordinator.app:
                before={(k, v.name) for k,v in coordinator.one_clicks.items()}
                await coordinator.async_load_private_catalogs()
                if before != {(k,v.name) for k,v in coordinator.one_clicks.items()}:
                    # Scene entities only exist if they were present at setup.
                    await hass.config_entries.async_reload(eid)

    async def request_status(call:ServiceCall):
        c,d=_entry_coordinator(hass,call.data["device_id"])
        if not c.mqtt or not d.app_data.get("topic"):
            raise HomeAssistantError("Govee app MQTT or device topic is unavailable")
        await c.mqtt.publish(d.app_data["topic"],"status",query=True,cmd_version=2)

    specs={
      "set_segments":(segments,vol.Schema({vol.Required("device_id"):str,
          vol.Required("segments"):vol.All([vol.Coerce(int)],vol.Length(min=1,max=200)),
          vol.Optional("rgb"):vol.All(vol.Coerce(int),vol.Range(min=0,max=16777215)),
          vol.Optional("brightness"):vol.All(vol.Coerce(int),vol.Range(min=0,max=100))})),
      "send_capability":(control,vol.Schema({vol.Required("device_id"):str,
          vol.Required("capability_type"):str,vol.Required("instance"):str,vol.Required("value"):object})),
      "refresh_scenes":(scenes,vol.Schema({vol.Required("device_id"):str})),
      "refresh_app_catalogs":(refresh_app,vol.Schema({})),
      "request_app_status":(request_status,vol.Schema({vol.Required("device_id"):str})),
      "app_request":(app_request,vol.Schema({vol.Required("operation"):str,
          vol.Optional("entry_id"):str,vol.Optional("query"):dict,vol.Optional("body"):dict})),
    }
    for name,(handler,schema) in specs.items():
        if not hass.services.has_service(DOMAIN,name):
            kw={"supports_response":SupportsResponse.OPTIONAL} if name=="app_request" else {}
            hass.services.async_register(DOMAIN,name,handler,schema=schema,**kw)


async def _async_options_updated(hass:HomeAssistant,entry:ConfigEntry)->None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass:HomeAssistant,entry:ConfigEntry)->bool:
    if not await hass.config_entries.async_unload_platforms(entry,PLATFORMS):return False
    c=hass.data[DOMAIN].pop(entry.entry_id,None)
    if c:
        await c.async_close()
    events=hass.data.get(f"{DOMAIN}_events",{}).pop(entry.entry_id,None)
    if events:await events.async_stop()
    if not hass.data[DOMAIN]:
        for name in ("set_segments","send_capability","refresh_scenes","refresh_app_catalogs","app_request","request_app_status"):
            hass.services.async_remove(DOMAIN,name)
    return True
