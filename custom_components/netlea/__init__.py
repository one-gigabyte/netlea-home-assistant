"""Set up the Netlea (尼特利) integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import NetleaApi
from .const import (
    CONF_LAMP_MODE,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_USER_ID,
    DEFAULT_LAMP_MODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import NetleaCoordinator
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BUTTON,
]

NetleaConfigEntry = ConfigEntry[NetleaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: NetleaConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    stored_user_id = entry.options.get(CONF_USER_ID) or entry.data.get(CONF_USER_ID) or None
    api = NetleaApi(session, entry.data[CONF_TOKEN], stored_user_id)

    coordinator = NetleaCoordinator(
        hass,
        session,
        api,
        scan_interval=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        lamp_mode=entry.options.get(CONF_LAMP_MODE, DEFAULT_LAMP_MODE),
    )

    # Never abort setup because of a transient cloud error: the websocket and
    # the periodic refresh keep running, entities are created as soon as the
    # device inventory becomes available (see entity.async_setup_dynamic_entities).
    try:
        await coordinator.async_refresh()
    except Exception as err:  # noqa: BLE001 - keep the entry alive
        _LOGGER.warning("Netlea 首次同步失败，稍后自动重试: %s", err)

    # Self heal: remember the account id learned from the cloud so that device
    # lookups work without the user having to re-add the integration.
    if api.user_id and not stored_user_id:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_USER_ID: str(api.user_id)}
        )
        _LOGGER.info("Netlea 已记录账号 userId=%s", api.user_id)

    await coordinator.async_start()
    await async_register_services(hass)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NetleaConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: NetleaCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
