"""Govee Native setup: genuine private app login, email 2FA, optional OpenAPI."""
from __future__ import annotations
import hashlib

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .app_api import GoveeAppAPI, AppAuthenticationError, AppAPIError, AppVerificationRequired
from .api import GoveeAPI, GoveeAuthError, GoveeError
from .const import (
    CONF_API_KEY, CONF_EMAIL, CONF_PASSWORD, CONF_APP_TOKEN,
    CONF_APP_ACCOUNT_ID, CONF_APP_ACCOUNT_TOPIC, CONF_APP_MQTT,
    CONF_POLL_SECONDS, CONF_MQTT, CONF_LAN, CONF_APP_CATALOGS,
    CONF_PRIVATE_MUTATIONS, DEFAULT_POLL_SECONDS, MIN_POLL_SECONDS, MAX_POLL_SECONDS, DOMAIN,
    CONF_BLUETOOTH, CONF_PREFER_BLUETOOTH, CONF_BLUETOOTH_ADDRESSES,
    CONF_H6199_PROFILE,
)


class GoveeNativeFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self):
        self._pending: dict = {}
        self._account: GoveeAppAPI | None = None

    async def async_step_user(self, user_input=None):
        errors={}
        if user_input is not None:
            email=str(user_input.get(CONF_EMAIL,"")).strip().lower()
            password=user_input.get(CONF_PASSWORD,"")
            key=str(user_input.get(CONF_API_KEY,"")).strip()
            if (email and not password) or (password and not email) or not (key or email):
                errors["base"]="invalid_input"
            else:
                self._pending={CONF_EMAIL:email,CONF_PASSWORD:password,CONF_API_KEY:key}
                if email:
                    self._account=GoveeAppAPI(async_get_clientsession(self.hass),email)
                    try:
                        session=await self._account.login(password)
                    except AppVerificationRequired:
                        try:await self._account.request_email_code()
                        except AppAPIError:
                            errors["base"]="cannot_send_code"
                        else:
                            return await self.async_step_verify()
                    except AppAuthenticationError:
                        errors["base"]="invalid_auth"
                    except AppAPIError:
                        errors["base"]="cannot_connect"
                    else:
                        self._save_session(session)
                if not errors and key:
                    try:await GoveeAPI(async_get_clientsession(self.hass),key).devices()
                    except GoveeAuthError:errors["base"]="invalid_key"
                    except GoveeError:errors["base"]="cannot_connect"
                if not errors:
                    return await self._complete()
        return self.async_show_form(step_id="user",data_schema=vol.Schema({
            vol.Optional(CONF_EMAIL,default=""):str,
            vol.Optional(CONF_PASSWORD,default=""):str,
            vol.Optional(CONF_API_KEY,default=""):str,
        }),errors=errors,description_placeholders={})

    def _save_session(self, session):
        self._pending[CONF_APP_TOKEN]=session.token
        self._pending[CONF_APP_ACCOUNT_ID]=session.account_id
        self._pending[CONF_APP_ACCOUNT_TOPIC]=session.account_topic

    async def async_step_verify(self, user_input=None):
        errors={}
        if user_input is not None:
            try:
                session=await self._account.login(self._pending[CONF_PASSWORD],str(user_input["code"]))
            except AppVerificationRequired:
                errors["base"]="invalid_code"
            except AppAPIError:
                errors["base"]="cannot_connect"
            else:
                self._save_session(session)
                key=self._pending.get(CONF_API_KEY)
                if key:
                    try:await GoveeAPI(async_get_clientsession(self.hass),key).devices()
                    except GoveeAuthError:errors["base"]="invalid_key"
                    except GoveeError:errors["base"]="cannot_connect"
                if not errors:
                    return await self._complete()
        return self.async_show_form(step_id="verify",data_schema=vol.Schema({vol.Required("code"):str}),errors=errors)

    async def _complete(self):
        email=self._pending.get(CONF_EMAIL,"")
        key=self._pending.get(CONF_API_KEY,"")
        unique=hashlib.sha256((email or key).encode("utf-8")).hexdigest()
        await self.async_set_unique_id(unique)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Govee Native ({email or 'OpenAPI'})",data=self._pending,
            options={CONF_POLL_SECONDS:DEFAULT_POLL_SECONDS,CONF_APP_MQTT:bool(email),
                     CONF_LAN:False,CONF_MQTT:False,CONF_APP_CATALOGS:bool(email),
                     CONF_PRIVATE_MUTATIONS:False},
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        # Accept password or key update using the same full login workflow.
        if user_input is not None:
            self._pending={**self._get_reauth_entry().data,
                           **{k:v for k,v in user_input.items() if v}}
            email=self._pending.get(CONF_EMAIL)
            if email:
                self._account=GoveeAppAPI(async_get_clientsession(self.hass),email)
                try:session=await self._account.login(self._pending[CONF_PASSWORD])
                except AppVerificationRequired:
                    try:
                        await self._account.request_email_code()
                    except AppAPIError:
                        return self.async_show_form(step_id="reauth_confirm", data_schema=self._reauth_schema(),
                                                    errors={"base": "cannot_send_code"})
                    return await self.async_step_reauth_verify()
                except AppAPIError:
                    return self.async_show_form(step_id="reauth_confirm",data_schema=self._reauth_schema(),errors={"base":"invalid_auth"})
                self._save_session(session)
            return self.async_update_reload_and_abort(self._get_reauth_entry(),data_updates=self._pending)
        return self.async_show_form(step_id="reauth_confirm",data_schema=self._reauth_schema())

    def _reauth_schema(self):
        return vol.Schema({vol.Optional(CONF_PASSWORD,default=""):str,vol.Optional(CONF_API_KEY,default=""):str})

    async def async_step_reauth_verify(self,user_input=None):
        errors={}
        if user_input is not None:
            try:session=await self._account.login(self._pending[CONF_PASSWORD],user_input["code"])
            except AppAPIError:errors["base"]="invalid_code"
            else:
                self._save_session(session)
                return self.async_update_reload_and_abort(self._get_reauth_entry(),data_updates=self._pending)
        return self.async_show_form(step_id="reauth_verify",data_schema=vol.Schema({vol.Required("code"):str}),errors=errors)

    @staticmethod
    def async_get_options_flow(config_entry):
        return GoveeNativeOptionsFlow()


class GoveeNativeOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            user_input = dict(user_input)
            configure = user_input.pop("configure_bluetooth", False)
            self._configure_segments = user_input.pop("configure_segments", False)
            self._pending_options = {**self.config_entry.options, **user_input}
            self._pending_options.pop("use_legacy_api_key", None)
            if configure:
                return await self.async_step_bluetooth()
            return await self._finish_options()
        opts=self.config_entry.options
        return self.async_show_form(step_id="init",data_schema=vol.Schema({
            vol.Required(CONF_POLL_SECONDS,default=opts.get(CONF_POLL_SECONDS,DEFAULT_POLL_SECONDS)):
                vol.All(vol.Coerce(int),vol.Range(min=MIN_POLL_SECONDS,max=MAX_POLL_SECONDS)),
            vol.Required("daily_request_budget", default=opts.get("daily_request_budget", 9000)):
                vol.All(vol.Coerce(int), vol.Range(min=500, max=10000)),
            vol.Required(CONF_APP_MQTT,default=opts.get(CONF_APP_MQTT,bool(self.config_entry.data.get(CONF_EMAIL)))):bool,
            vol.Required(CONF_APP_CATALOGS,default=opts.get(CONF_APP_CATALOGS,True)):bool,
            vol.Required(CONF_LAN,default=opts.get(CONF_LAN,False)):bool,
            vol.Required(CONF_MQTT,default=opts.get(CONF_MQTT,False)):bool,
            vol.Required(CONF_PRIVATE_MUTATIONS,default=opts.get(CONF_PRIVATE_MUTATIONS,False)):bool,
            vol.Required(CONF_BLUETOOTH, default=opts.get(CONF_BLUETOOTH,False)):bool,
            vol.Required(CONF_PREFER_BLUETOOTH, default=opts.get(CONF_PREFER_BLUETOOTH,False)):bool,
            vol.Required(CONF_H6199_PROFILE, default=opts.get(CONF_H6199_PROFILE,"classic")):selector.SelectSelector(
                selector.SelectSelectorConfig(options=[{"value":"classic","label":"Original H6199 firmware"},
                                                       {"value":"updated","label":"Updated H6199 firmware"}])),
            vol.Optional("configure_bluetooth", default=False):bool,
            vol.Optional("configure_segments", default=False):bool,
        }))

    async def _finish_options(self):
        if getattr(self, "_configure_segments", False):
            self._configure_segments = False
            return await self.async_step_segments()
        return self.async_create_entry(title="", data=self._pending_options)

    async def async_step_segments(self, user_input=None):
        from .capabilities import segment_indices, SEGMENT_MODES
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        devices = [d for d in coordinator.devices.values() if segment_indices(d)] if coordinator else []
        if not devices:
            return self.async_abort(reason="no_segment_devices")
        errors = {}
        if user_input is not None:
            selected = user_input.get("device_ids", [])
            mode = user_input.get("mode")
            if not selected or set(selected) - {d.device_id for d in devices} or mode not in SEGMENT_MODES:
                errors["base"] = "invalid_segment_selection"
            else:
                modes = {**self._pending_options.get("segment_mode_by_device", {}), **dict.fromkeys(selected, mode)}
                self._pending_options["segment_mode_by_device"] = modes
                return await self._finish_options()
        return self.async_show_form(step_id="segments", errors=errors, data_schema=vol.Schema({
            vol.Required("device_ids"): selector.SelectSelector(selector.SelectSelectorConfig(multiple=True,
                options=[{"value": d.device_id, "label": f"{d.name} ({len(segment_indices(d))} segments)"} for d in devices])),
            vol.Required("mode", default="individual"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": mode, "label": {"individual": "Individual segments", "grouped": "All segments together",
                    "both": "Individual and grouped", "disabled": "Hide segment entities"}[mode]} for mode in SEGMENT_MODES])),
        }))

    async def async_step_bluetooth(self, user_input=None):
        from .bluetooth import valid_address
        errors = {}
        c = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        from .native_protocol import BLE_MODELS
        devices = [d for d in c.devices.values() if d.sku.upper() in BLE_MODELS] if c else []
        if not devices:
            return self.async_abort(reason="no_bluetooth_devices")
        if user_input is not None:
            device_id = user_input.get("device_id")
            address = str(user_input.get("address", "")).strip().upper()
            if device_id not in {d.device_id for d in devices} or not valid_address(address):
                errors["base"] = "invalid_bluetooth_address"
            else:
                addresses = {**self._pending_options.get(CONF_BLUETOOTH_ADDRESSES, {}), device_id: address}
                self._pending_options[CONF_BLUETOOTH_ADDRESSES] = addresses
                self._pending_options[CONF_BLUETOOTH] = True
                return await self._finish_options()
        return self.async_show_form(step_id="bluetooth", errors=errors, data_schema=vol.Schema({
            vol.Required("device_id"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": d.device_id, "label": d.name} for d in devices])),
            vol.Required("address"): str,
        }))
