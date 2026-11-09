"""Regressions for credential isolation, cached TLS and reauthentication errors."""
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch
import ssl

import pytest
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.util.ssl import client_context

import custom_components.govee_native as integration
from custom_components.govee_native.app_api import AppAPIError, AppAuthenticationError, AppVerificationRequired, AppSession, GoveeAppAPI
from custom_components.govee_native.config_flow import GoveeNativeFlow, GoveeNativeOptionsFlow
from custom_components.govee_native import mqtt_events


@pytest.mark.parametrize("own_key", [None, "native-test-key"])
async def test_setup_never_reads_another_integrations_credentials(coordinator, own_key):
    hass = coordinator.hass
    hass.config_entries = ConfigEntries(hass, {})
    data = {"email": "test@example.invalid", "app_token": "test", "app_account_id": "test", "app_account_topic": "GA/test"}
    if own_key:
        data["api_key"] = own_key
    entry = ConfigEntry(domain="govee_native", version=2, minor_version=1, source="user", title="Test",
        data=data, options={"use_legacy_api_key": True, "app_mqtt": False, "app_catalogs": False, "poll_seconds": 90},
        unique_id="review-test", subentries_data=[], discovery_keys=MappingProxyType({}),
        state=ConfigEntryState.SETUP_IN_PROGRESS)
    with patch.object(hass.config_entries, "async_setup", AsyncMock(return_value=True)):
        await hass.config_entries.async_add(entry)
    original_entries = hass.config_entries.async_entries

    def owned_entries_only(domain=None, **kwargs):
        assert domain != "govee", "Another integration's credentials must never be inspected"
        return original_entries(domain, **kwargs)

    session = object()
    with (
        patch.object(hass.config_entries, "async_entries", side_effect=owned_entries_only),
        patch("homeassistant.helpers.entity_registry.async_get", return_value=SimpleNamespace(entities=SimpleNamespace(get_entries_for_config_entry_id=lambda _: []))),
        patch.object(integration, "async_get_clientsession", return_value=session),
        patch.object(GoveeAppAPI, "devices", AsyncMock(return_value=[{"device": "id", "sku": "H6199", "topic": "GD/test"}])),
        patch.object(integration.GoveeAPI, "devices", AsyncMock(return_value=[])),
        patch.object(integration, "GoveeAPI", wraps=integration.GoveeAPI) as api_factory,
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)),
    ):
        try:
            assert await integration.async_setup_entry(hass, entry)
            if own_key:
                api_factory.assert_called_once_with(session, own_key)
            else:
                api_factory.assert_not_called()
            assert "use_legacy_api_key" not in entry.options
            assert entry.options["poll_seconds"] == 90
        finally:
            if entry.entry_id in hass.data.get("govee_native", {}):
                await integration.async_unload_entry(hass, entry)


async def test_removed_credential_option_is_not_offered_or_resaved(coordinator):
    flow = GoveeNativeOptionsFlow()
    flow.hass = coordinator.hass
    entry = SimpleNamespace(entry_id="test", data={}, options={"use_legacy_api_key": True,
        "bluetooth_addresses": {"owned-device": "AA:BB:CC:DD:EE:FF"}, "private_mutations": True})
    with patch.object(GoveeNativeOptionsFlow, "config_entry", property(lambda self: entry)):
        form = await flow.async_step_init()
        assert "use_legacy_api_key" not in {key.schema for key in form["data_schema"].schema}
        result = await flow.async_step_init({"poll_seconds": 90})
    assert "use_legacy_api_key" not in result["data"]
    assert result["data"]["bluetooth_addresses"] == entry.options["bluetooth_addresses"]
    assert result["data"]["private_mutations"] is True


async def test_event_mqtt_uses_cached_verified_context_without_loading_certs(coordinator):
    cached = client_context()
    with patch.object(mqtt_events.mqtt.Client, "tls_set", side_effect=AssertionError("Blocking certificate loading on event loop")):
        events = mqtt_events.GoveeEvents(coordinator.hass, coordinator, "native-test-key")
    assert events._client._ssl_context is cached
    assert cached.verify_mode == ssl.CERT_REQUIRED
    assert cached.check_hostname


@pytest.mark.parametrize("error_type", [AppAPIError, AppAuthenticationError])
async def test_reauth_email_request_failure_shows_error_and_allows_retry(coordinator, error_type):
    flow = GoveeNativeFlow()
    flow.hass = coordinator.hass
    entry = SimpleNamespace(data={"email": "test@example.invalid", "password": "old-password", "app_token": "old-token"})
    account = SimpleNamespace(login=AsyncMock(side_effect=AppVerificationRequired()),
        request_email_code=AsyncMock(side_effect=[error_type("rejected"), None]))
    with (
        patch.object(flow, "_get_reauth_entry", return_value=entry),
        patch("custom_components.govee_native.config_flow.async_get_clientsession", return_value=object()),
        patch("custom_components.govee_native.config_flow.GoveeAppAPI", return_value=account),
        patch.object(flow, "async_update_reload_and_abort") as update,
    ):
        result = await flow.async_step_reauth_confirm({"password": "new-password"})
        assert result["type"] == "form" and result["step_id"] == "reauth_confirm"
        assert result["errors"] == {"base": "cannot_send_code"}
        update.assert_not_called()
        assert entry.data["app_token"] == "old-token"
        result = await flow.async_step_reauth_confirm({"password": "new-password"})
        assert result["step_id"] == "reauth_verify"
        account.login.side_effect = None
        account.login.return_value = AppSession("new-token", "account", "GA/test")
        await flow.async_step_reauth_verify({"code": "123456"})
        update.assert_called_once()
        assert update.call_args.kwargs["data_updates"]["app_token"] == "new-token"
