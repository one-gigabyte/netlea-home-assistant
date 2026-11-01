"""Light platform: WRGB colour + brightness for the aquarium lamp (cmd 16)."""

from __future__ import annotations

import logging

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGBW_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PRODUCT_LAMP
from .coordinator import NetleaCoordinator
from .entity import NetleaEntity, async_setup_dynamic_entities

_LOGGER = logging.getLogger(__name__)

# device channel levels are 0-100 %, HA colour channels are 0-255
_CH_SCALE = 255 / 100
# device `grade` is the spectrum preset index 1-10, HA brightness is 0-255
_GRADE_SCALE = 255 / 10


def _to_ha(value: int | None) -> int:
    return max(0, min(255, round((value or 0) * _CH_SCALE)))


def _from_ha(value: int) -> int:
    return max(0, min(100, round(value / _CH_SCALE)))


def _grade_to_ha(value: int | None) -> int:
    return max(0, min(255, round((value or 0) * _GRADE_SCALE)))


def _grade_from_ha(value: int) -> int:
    return max(0, min(10, round(value / _GRADE_SCALE)))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NetleaCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _build(device):
        if device.product_id == PRODUCT_LAMP:
            return [NetleaLampLight(coordinator, device)]
        return []

    async_setup_dynamic_entities(coordinator, async_add_entities, _build)


class NetleaLampLight(NetleaEntity, LightEntity):
    """WRGB water-plant lamp.

    The device keeps four independent spectrum channels (W/R/G/B, plus an
    optional F).  HA colour is mapped straight onto them, and the overall
    spectrum level (`grade`) becomes HA brightness.
    """

    _attr_name = "WRGB 灯光"
    _attr_icon = "mdi:lightbulb-on"
    _attr_color_mode = ColorMode.RGBW
    _attr_supported_color_modes = {ColorMode.RGBW}

    def __init__(self, coordinator: NetleaCoordinator, device) -> None:
        super().__init__(coordinator, device, "light")

    # -- state ---------------------------------------------------------- #
    @property
    def is_on(self) -> bool | None:
        return self.device.is_on

    @property
    def brightness(self) -> int | None:
        grade = self.device.grade
        return None if grade is None else _grade_to_ha(grade)

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        channels = self.device.channel_map()
        if not channels:
            return None
        return (
            _to_ha(channels.get("R")),
            _to_ha(channels.get("G")),
            _to_ha(channels.get("B")),
            _to_ha(channels.get("W")),
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attrs = dict(super().extra_state_attributes)
        channels = self.device.channel_map()
        attrs["channels"] = channels
        attrs["grade"] = self.device.grade
        attrs["lamp_mode"] = self.device.lamp_mode
        attrs["mode_id"] = self.device.merged_state().get("modeId")
        return attrs

    # -- commands ------------------------------------------------------- #
    async def async_turn_on(self, **kwargs) -> None:
        grade = kwargs.get(ATTR_BRIGHTNESS)
        rgbw = kwargs.get(ATTR_RGBW_COLOR)

        channels: dict[str, int] | None = None
        if rgbw is not None:
            red, green, blue, white = rgbw
            channels = {
                "R": _from_ha(red),
                "G": _from_ha(green),
                "B": _from_ha(blue),
                "W": _from_ha(white),
            }

        await self.coordinator.async_set_lamp(
            self.device,
            power=True,
            grade=None if grade is None else _grade_from_ha(grade),
            channels=channels,
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_lamp(self.device, power=False)
