"""Config flow for the Netlea (尼特利) integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import NetleaApi, NetleaApiError, NetleaAuthError
from .const import (
    CONF_CODE,
    CONF_LAMP_MODE,
    CONF_PHONE,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_USER_ID,
    DEFAULT_LAMP_MODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LAMP_MODE_OPTIONS,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER = vol.Schema(
    {
        vol.Optional(CONF_PHONE, default=""): str,
        vol.Optional(CONF_TOKEN, default=""): str,
        vol.Optional(CONF_USER_ID, default=""): str,
    }
)

STEP_CODE = vol.Schema({vol.Required(CONF_CODE): str})


class NetleaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Phone + SMS code login, or paste an existing token."""

    VERSION = 1

    def __init__(self) -> None:
        self._phone: str = ""
        self._uuid: str = ""
        self._token: str | None = None
        self._user_id: Any = None
        self._user_name: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._phone = str(user_input.get(CONF_PHONE) or "").strip()
            token = str(user_input.get(CONF_TOKEN) or "").strip()
            manual_uid = str(user_input.get(CONF_USER_ID) or "").strip()
            session = async_get_clientsession(self.hass)

            if token:
                api = NetleaApi(session, token, manual_uid or None)
                try:
                    await api.validate_token()
                except NetleaAuthError:
                    errors["base"] = "invalid_auth"
                except NetleaApiError as err:
                    _LOGGER.warning("Token validation failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    self._token = token
                    self._user_id = manual_uid or None
                    return await self._create_entry()
            elif self._phone:
                api = NetleaApi(session)
                try:
                    self._uuid = await api.request_sms_code(self._phone)
                except NetleaApiError as err:
                    _LOGGER.warning("SMS request failed: %s", err)
                    errors["base"] = "sms_failed"
                else:
                    return await self.async_step_code()
            else:
                errors["base"] = "no_credentials"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER,
            errors=errors,
        )

    async def async_step_code(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            api = NetleaApi(async_get_clientsession(self.hass))
            try:
                payload = await api.login_with_code(
                    self._phone, str(user_input[CONF_CODE]).strip(), self._uuid
                )
            except NetleaAuthError as err:
                _LOGGER.warning("Login rejected: %s", err)
                errors["base"] = "invalid_auth"
            except NetleaApiError as err:
                _LOGGER.warning("Login failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._token = api.token
                self._user_id = payload.get("userId")
                self._user_name = payload.get("userName") or payload.get("nickName")
                return await self._create_entry()

        return self.async_show_form(
            step_id="code",
            data_schema=STEP_CODE,
            errors=errors,
            description_placeholders={"phone": self._phone},
        )

    async def _create_entry(self):
        unique = str(self._user_id or self._phone or (self._token or "")[-12:])
        await self.async_set_unique_id(unique, raise_on_progress=False)
        self._abort_if_unique_id_configured(updates={CONF_TOKEN: self._token})
        title = f"尼特利 {self._user_name or self._phone or ''}".strip()
        data = {
            CONF_TOKEN: self._token,
            CONF_PHONE: self._phone,
            CONF_USER_ID: str(self._user_id or ""),
            CONF_NAME: title,
        }
        return self.async_create_entry(title=title, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return NetleaOptionsFlow()


class NetleaOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        current_uid = self.config_entry.options.get(
            CONF_USER_ID, self.config_entry.data.get(CONF_USER_ID, "")
        )
        current_mode = self.config_entry.options.get(CONF_LAMP_MODE, DEFAULT_LAMP_MODE)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SCAN_INTERVAL, default=current): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=600,
                            step=5,
                            unit_of_measurement="秒",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(CONF_USER_ID, default=current_uid or ""): selector.TextSelector(),
                    vol.Optional(
                        CONF_LAMP_MODE, default=str(current_mode)
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=value, label=label)
                                for value, label in LAMP_MODE_OPTIONS
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )
