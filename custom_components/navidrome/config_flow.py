"""Admin-only config, reauthentication and same-entry URL reconfiguration."""

from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    CannotConnect,
    InvalidAuth,
    NavidromeClient,
    NotAdmin,
    NotSupported,
    ProtocolError,
    normalize_url,
)
from .const import DOMAIN, NAME


def connection_schema(values: Mapping[str, Any], *, password_required: bool) -> probatio.Schema:
    """Never suggest a stored password back to the frontend."""
    password = probatio.Required if password_required else probatio.Optional
    return probatio.Schema(
        {
            probatio.Required(CONF_URL, default=values.get(CONF_URL, "")): str,
            probatio.Required(CONF_USERNAME, default=values.get(CONF_USERNAME, "")): str,
            password(probatio.Secret(CONF_PASSWORD)): str,
        }
    )


class NavidromeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure one connection, not an assumed immutable physical server ID."""

    VERSION = 1

    async def _validate(self, data: dict[str, Any]) -> dict[str, str]:
        try:
            data[CONF_URL] = normalize_url(data[CONF_URL])
        except ValueError:
            return {CONF_URL: "invalid_url"}
        try:
            api = NavidromeClient(
                async_get_clientsession(self.hass),
                data[CONF_URL],
                data[CONF_USERNAME],
                data[CONF_PASSWORD],
            )
            await api.validate()
        except InvalidAuth:
            return {"base": "invalid_auth"}
        except NotAdmin:
            return {"base": "not_admin"}
        except NotSupported:
            return {"base": "not_supported"}
        except CannotConnect:
            return {"base": "cannot_connect"}
        except ProtocolError:
            return {"base": "invalid_response"}
        return {}

    def _check_duplicate(self, url: str, entry_id: str | None = None) -> None:
        for entry in self._async_current_entries():
            if entry.entry_id != entry_id and normalize_url(entry.data[CONF_URL]) == url:
                from homeassistant.data_entry_flow import AbortFlow

                raise AbortFlow("already_configured")

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Explicitly disclose all-user monitoring and administrator credential power."""
        errors = {}
        if user_input is not None:
            data = dict(user_input)
            errors = await self._validate(data)
            if not errors:
                self._check_duplicate(data[CONF_URL])
                return self.async_create_entry(title=f"{NAME} ({data[CONF_URL]})", data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=connection_schema(user_input or {}, password_required=True),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Keep the original config entry and user entity identities."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data = dict(entry.data) | user_input
            errors = await self._validate(data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                    probatio.Required(probatio.Secret(CONF_PASSWORD)): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Changing the URL does not change entry_id or any entity unique ID."""
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            data = dict(entry.data) | user_input
            if not data.get(CONF_PASSWORD):
                data[CONF_PASSWORD] = entry.data[CONF_PASSWORD]
            errors = await self._validate(data)
            if not errors:
                self._check_duplicate(data[CONF_URL], entry.entry_id)
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=connection_schema(entry.data, password_required=False),
            errors=errors,
        )
