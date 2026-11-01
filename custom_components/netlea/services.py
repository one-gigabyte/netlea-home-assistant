"""Services for the Netlea integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .coordinator import NetleaCoordinator
from .protocol import build_frame, from_hex, to_hex

_LOGGER = logging.getLogger(__name__)

ATTR_REAL_ADDRESS = "real_address"
ATTR_FRAME = "frame"
ATTR_DEVICE_TYPE = "device_type"
ATTR_CMD = "cmd"
ATTR_BODY = "body"
ATTR_PROTOCOL = "protocol"
ATTR_CMD_TYPE = "cmd_type"
ATTR_IS_SET = "is_set"

SERVICE_SEND_FRAME = "send_frame"
SERVICE_BUILD_FRAME = "build_frame"

SEND_FRAME_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_REAL_ADDRESS): str,
        vol.Exclusive(ATTR_FRAME, "payload"): str,
        vol.Exclusive(ATTR_CMD, "payload"): vol.Coerce(int),
        vol.Optional(ATTR_DEVICE_TYPE): vol.Coerce(int),
        vol.Optional(ATTR_BODY, default=""): str,
        vol.Optional(ATTR_PROTOCOL, default=0x5A): vol.Coerce(int),
        vol.Optional(ATTR_CMD_TYPE, default=0): vol.Coerce(int),
    }
)

BUILD_FRAME_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_TYPE): vol.Coerce(int),
        vol.Required(ATTR_CMD): vol.Coerce(int),
        vol.Optional(ATTR_BODY, default=""): str,
        vol.Optional(ATTR_PROTOCOL, default=0x5A): vol.Coerce(int),
        vol.Optional(ATTR_CMD_TYPE, default=0): vol.Coerce(int),
    }
)


def _first_coordinator(hass: HomeAssistant) -> NetleaCoordinator:
    for coordinator in hass.data.get(DOMAIN, {}).values():
        return coordinator
    raise HomeAssistantError("Netlea 集成未配置")


async def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SEND_FRAME):
        return

    async def _send_frame(call: ServiceCall) -> None:
        coordinator = _first_coordinator(hass)
        address = call.data[ATTR_REAL_ADDRESS]
        device = coordinator.devices.get(address)
        if device is None:
            raise HomeAssistantError(f"未知设备 {address}")

        if ATTR_FRAME in call.data:
            frame_hex = call.data[ATTR_FRAME].strip().lower()
        else:
            device_type = call.data.get(ATTR_DEVICE_TYPE) or device.device_type
            if device_type is None:
                raise HomeAssistantError("需要 device_type")
            body = from_hex(call.data.get(ATTR_BODY) or "")
            frame = build_frame(
                int(device_type),
                int(call.data[ATTR_CMD]),
                body,
                protocol=int(call.data.get(ATTR_PROTOCOL, device.protocol)),
                cmd_type=int(call.data.get(ATTR_CMD_TYPE, 0)),
            )
            frame_hex = to_hex(frame)

        _LOGGER.debug("service send_frame -> %s %s", address, frame_hex)
        if not await coordinator.async_send_raw(device, frame_hex):
            raise HomeAssistantError("WebSocket 未连接，指令未发送")

    async def _build_frame(call: ServiceCall) -> dict[str, str]:
        body = from_hex(call.data.get(ATTR_BODY) or "")
        frame = build_frame(
            int(call.data[ATTR_DEVICE_TYPE]),
            int(call.data[ATTR_CMD]),
            body,
            protocol=int(call.data.get(ATTR_PROTOCOL, 0x5A)),
            cmd_type=int(call.data.get(ATTR_CMD_TYPE, 0)),
        )
        hex_frame = to_hex(frame)
        _LOGGER.debug("built frame %s", hex_frame)
        return {"frame": hex_frame}

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_FRAME, _send_frame, schema=SEND_FRAME_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BUILD_FRAME,
        _build_frame,
        schema=BUILD_FRAME_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
