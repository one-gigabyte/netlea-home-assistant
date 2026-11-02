"""Coordinator: keeps the device inventory, online state and live frame state."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NetleaApi, NetleaApiError, NetleaAuthError, NetleaWebSocket
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DEVTYPE_LAMP,
    DEVTYPE_UV,
    DOMAIN,
    PRODUCT_LAMP,
    PRODUCT_NAMES,
    PRODUCT_SMART_TANK,
    PRODUCT_UV,
)
from .protocol import (
    CHANNEL_MAX,
    CHANNEL_ORDER,
    GRADE_MAX,
    LAMP_MODE_AUTOMATIC,
    LAMP_MODE_MANUAL,
    PROTO_5A,
    PROTO_6A,
    Frame,
    build_frame,
    build_lamp_body,
    decode_frame,
    from_hex,
    le,
    parse_frame,
    to_hex,
)

_LOGGER = logging.getLogger(__name__)

# product id -> frame device type
PRODUCT_TO_DEVTYPE = {
    1: DEVTYPE_LAMP,
    7: DEVTYPE_UV,
    5: 5,
    6: 6,
    8: 8,
}

INVENTORY_REFRESH = timedelta(minutes=5)

# 指令下发后，若设备回报的开关状态与意图相反，先当作"在途的旧回报"忽略；
# 超过这个秒数仍不一致，则以设备为准。
INTENT_GRACE = 3.0


@dataclass
class NetleaDevice:
    """One physical device as exposed by the cloud account."""

    real_address: str
    name: str
    product_id: int | None = None
    model: str | None = None
    mac: str | None = None
    firmware: str | None = None
    online: bool = False
    protocol: int = PROTO_5A
    last_seen: float | None = None
    raw_status_log: dict[str, Any] = field(default_factory=dict)
    cloud_power: bool | None = None
    # Power the user asked for but the device has not confirmed yet.  Writes
    # that do not carry an explicit `power` (channel / grade setters) inherit
    # this instead of the stale `lampState` from the last device report --
    # otherwise a scene that restores several entities in a row would switch
    # the lamp back off with the value it had before the scene started.
    intent_power: bool | None = None
    intent_power_at: float = 0.0
    # Last cmd-16 body we actually sent, used to swallow the burst of
    # identical writes a scene restore produces.
    last_body: bytes | None = None
    state: dict[int, dict[str, Any]] = field(default_factory=dict)
    last_frame: str | None = None

    @property
    def device_type(self) -> int | None:
        if self.product_id in PRODUCT_TO_DEVTYPE:
            return PRODUCT_TO_DEVTYPE[self.product_id]
        return None

    @property
    def product_name(self) -> str:
        return PRODUCT_NAMES.get(self.product_id or -1, "设备")

    def merged_state(self) -> dict[str, Any]:
        """Flatten every decoded command payload, newest wins."""
        merged: dict[str, Any] = {}
        for payload in self.state.values():
            for key, value in payload.items():
                merged[key] = value
        return merged

    def channels(self) -> list[int]:
        """Master channel levels in the order `lt()` (cmd 16) expects.

        The cmd 20 report is authoritative; fall back to the cmd 26 / cmd 16
        echoes when it has not arrived yet.
        """
        # cmd 26 is the configured spectrum (what the lamp is aiming at);
        # cmd 20 is the live output and can be caught mid-transition, so it is
        # only a last resort.  cmd 16 is our own echo.
        for cmd in (26, 16, 20):
            payload = self.state.get(cmd) or {}
            if all(key in payload for key in CHANNEL_ORDER):
                return [int(payload[key]) for key in CHANNEL_ORDER]
        return [0, 0, 0, 0, 0]

    def channel_map(self) -> dict[str, int]:
        """Channel levels keyed by name ("R"/"G"/"W"/"B"/"F")."""
        return dict(zip(CHANNEL_ORDER, self.channels()))

    @property
    def lamp_mode(self) -> int | None:
        for cmd in (26, 16, 20):
            payload = self.state.get(cmd) or {}
            if "lampMode" in payload:
                return int(payload["lampMode"])
        return None

    @property
    def grade(self) -> int | None:
        for cmd in (26, 16, 20):
            payload = self.state.get(cmd) or {}
            if "grade" in payload:
                return int(payload["grade"])
        return None

    @property
    def is_on(self) -> bool | None:
        # An unconfirmed command wins until the device answers, so the UI does
        # not flick back to the previous state mid-way through a scene.
        if self.intent_power is not None:
            return self.intent_power
        merged = self.merged_state()
        for key in ("lampState", "onOff", "isSwitch", "totalSwitch"):
            if key in merged:
                return bool(merged[key])
        # 轮询拿到的 cmd 26/28/32 不含开关位，回落到云端快照里的 onoff
        return self.cloud_power

    @property
    def brightness(self) -> int | None:
        merged = self.merged_state()
        for key in ("grade", "lv", "pwm"):
            value = merged.get(key)
            if isinstance(value, int):
                return value
        return None


class NetleaCoordinator(DataUpdateCoordinator[dict[str, NetleaDevice]]):
    """Polls the cloud, listens on the websocket, keeps per-device state."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        api: NetleaApi,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        lamp_mode: int | str = "auto",
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.api = api
        self.session = session
        # "auto" = echo whatever the lamp reports (fallback: 跟随行程)
        self.lamp_mode_option: int | str = lamp_mode
        self.devices: dict[str, NetleaDevice] = {}
        self.ws = NetleaWebSocket(
            session,
            token_provider=lambda: self.api.token,
            on_frame=self._handle_frame_message,
            on_online_list=self._handle_online_list,
            on_connection=self._handle_connection,
        )
        self._last_inventory: float = 0.0
        self._seq: dict[str, int] = {}
        self._device_listeners: list[Callable[[], None]] = []
        self._inventory_error_logged = False

    def async_add_device_listener(self, callback: Callable[[], None]) -> None:
        """Register a sync callback fired when new devices appear.

        Entities are created lazily: the cloud inventory can be empty at setup
        time (server hiccup, brand new account) and fill in later.
        """
        self._device_listeners.append(callback)

    def _notify_device_listeners(self) -> None:
        for callback in list(self._device_listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001 - never break the update cycle
                _LOGGER.exception("Netlea device listener failed")

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def async_start(self) -> None:
        await self.ws.start()

    async def async_shutdown(self) -> None:
        await self.ws.stop()

    # ------------------------------------------------------------------ #
    # websocket callbacks
    # ------------------------------------------------------------------ #
    def _handle_connection(self, connected: bool) -> None:
        if connected:
            self.hass.async_create_task(self._async_kick_refresh())

    async def _async_kick_refresh(self) -> None:
        await self.async_request_refresh()

    def _handle_online_list(self, rows: list[dict[str, Any]]) -> None:
        changed = False
        for row in rows:
            address = str(row.get("realAddress") or "")
            if not address:
                continue
            online = bool(row.get("runState"))
            device = self.devices.get(address)
            if device is None:
                continue
            if device.online != online:
                device.online = online
                changed = True
        if changed:
            self.async_update_listeners()

    def _handle_frame_message(self, real_address: str, message: dict[str, Any]) -> None:
        body = message.get("msgBody") or {}
        hex_message = body.get("message") or ""
        address = real_address or str(body.get("realAddress") or "")
        device = self.devices.get(address)
        if device is None:
            _LOGGER.debug("Frame for unknown device %s: %s", address, hex_message)
            return
        try:
            raw = from_hex(hex_message)
        except ValueError:
            _LOGGER.debug("Bad hex from %s: %s", address, hex_message)
            return
        self._apply_frame(device, raw, message.get("code"))
        self.async_update_listeners()

    def _apply_frame(self, device: NetleaDevice, raw: bytes, ws_code: Any = None) -> None:
        frame: Frame | None = parse_frame(raw)
        device.last_frame = to_hex(raw)
        device.last_seen = self.hass.loop.time()
        device.online = True
        if frame is None or not frame.checksum_ok:
            _LOGGER.debug(
                "Unparsed frame from %s (ws code %s): %s", device.real_address, ws_code, raw.hex()
            )
            return
        decoded = decode_frame(frame)
        if decoded:
            device.state[frame.cmd] = decoded
            self._infer_product(device, frame, decoded)
            self._sync_intent(device, decoded)
            if frame.cmd == 20:
                # Full state report: whatever we sent last may be stale now
                # (the app could have changed something), so drop the cache.
                device.last_body = None
        _LOGGER.debug(
            "%s cmd=%s state=%s code=%s body=%s",
            device.real_address,
            frame.cmd,
            frame.request_state,
            frame.code_name,
            frame.body.hex(),
        )

    def _sync_intent(self, device: NetleaDevice, decoded: dict) -> None:
        """Drop the optimistic power flag once the device has answered.

        The device is authoritative, but a report that was already in flight
        when we sent the command must not undo it -- hence the short grace
        period for a contradicting report.
        """
        if device.intent_power is None or "lampState" not in decoded:
            return
        if bool(decoded["lampState"]) == device.intent_power:
            device.intent_power = None
        elif time.monotonic() - device.intent_power_at > INTENT_GRACE:
            _LOGGER.debug(
                "Netlea %s 未按指令切换（intent=%s, 设备回报=%s），以设备为准",
                device.real_address,
                device.intent_power,
                decoded["lampState"],
            )
            device.intent_power = None

    def _infer_product(self, device: NetleaDevice, frame: Frame, decoded: dict) -> None:
        """Fall back to the frame's device type when the cloud row is ambiguous."""
        if device.product_id is not None:
            return
        inferred: int | None = None
        if frame.device_type == DEVTYPE_LAMP or "lampState" in decoded or "lampMode" in decoded:
            inferred = PRODUCT_LAMP
        elif frame.device_type == DEVTYPE_UV:
            inferred = PRODUCT_UV
        if inferred is None:
            return
        device.product_id = inferred
        _LOGGER.info(
            "Netlea 由设备帧推断 %s 为 %s（product_id=%s）",
            device.real_address,
            PRODUCT_NAMES.get(inferred, inferred),
            inferred,
        )
        self._notify_device_listeners()

    # ------------------------------------------------------------------ #
    # polling
    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> dict[str, NetleaDevice]:
        if not self.ws.connected:
            await self.ws.start()
        now = self.hass.loop.time()
        if not self.devices or now - self._last_inventory > INVENTORY_REFRESH.total_seconds():
            await self._async_refresh_inventory()
            self._last_inventory = now
        else:
            await self.ws.request_online_list()
        await self._async_query_devices()
        return self.devices

    async def _async_refresh_inventory(self) -> None:
        try:
            rows = await self.api.get_devices()
        except NetleaAuthError as err:
            raise UpdateFailed(f"登录状态失效: {err}") from err
        except NetleaApiError as err:
            if not self._inventory_error_logged:
                _LOGGER.warning(
                    "Netlea 设备清单拉取失败（集成仍会按间隔自动重试）: %s | "
                    "userId=%s。若持续失败：删除集成后重新添加（短信登录会自动记录 userId），"
                    "或在集成「配置」里手动填写 userId。",
                    err,
                    self.api.user_id,
                )
                self._inventory_error_logged = True
            raise UpdateFailed(str(err)) from err
        self._inventory_error_logged = False

        seen: set[str] = set()
        added = False
        for row in rows:
            address = str(row.get("realAddress") or "")
            if not address:
                continue
            seen.add(address)
            device = self.devices.get(address)
            if device is None:
                device = NetleaDevice(real_address=address, name=str(row.get("name") or address))
                self.devices[address] = device
                added = True
                _LOGGER.info(
                    "Netlea 发现设备 %s (%s)", device.name, address
                )
            self._update_from_row(device, row)

        for address in list(self.devices):
            if address not in seen and rows:
                _LOGGER.debug("Device %s no longer present in the account", address)
                self.devices.pop(address, None)

        if added:
            self._notify_device_listeners()

    def _update_from_row(self, device: NetleaDevice, row: dict[str, Any]) -> None:
        status_log = row.get("statusLog") or {}
        if isinstance(status_log, str):
            try:
                status_log = json.loads(status_log)
            except ValueError:
                status_log = {}
        device.raw_status_log = status_log
        device.name = str(row.get("name") or status_log.get("showName") or device.name)
        equipment_type = row.get("equipmentType") or {}
        device.product_id = (
            row.get("equipmentProductId")
            or equipment_type.get("equipmentProductId")
            or row.get("equipmentTypeId")
            or equipment_type.get("equipmentTypeId")
            or device.product_id
        )
        if device.product_id is not None:
            try:
                device.product_id = int(device.product_id)
            except (TypeError, ValueError):
                pass
        device.mac = row.get("mac") or device.mac
        device.firmware = status_log.get("firmwareVersion") or device.firmware
        if "onoff" in status_log:
            device.cloud_power = bool(status_log.get("onoff"))
        protocol = status_log.get("protocolType")
        if protocol in (PROTO_5A, PROTO_6A, str(PROTO_5A), str(PROTO_6A)):
            device.protocol = int(protocol)

    async def _async_query_devices(self) -> None:
        """Ask every online device to report its current state."""
        if not self.ws.connected:
            return
        for device in list(self.devices.values()):
            if device.product_id == PRODUCT_LAMP:
                # gt() / _t() / Ft() in api/ble/LampDevice.js: cmd 27/29/33
                # carry a single 0x00 body byte.
                for cmd in (27, 29, 33):
                    await self._async_send(device, cmd, b"\x00")
                    await asyncio.sleep(0.15)
            elif device.product_id == PRODUCT_UV:
                # cmd 49 (schedule) + cmd 50 (state) with an empty body
                for cmd in (49, 50):
                    await self._async_send(device, cmd, b"")
                    await asyncio.sleep(0.15)
            elif device.product_id == PRODUCT_SMART_TANK:
                await self._async_send(device, 128, b"")

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    async def _async_send(
        self,
        device: NetleaDevice,
        cmd: int,
        body: bytes = b"",
        cmd_type: int = 0,
    ) -> bool:
        device_type = device.device_type
        if device_type is None:
            _LOGGER.warning("Unknown device type for %s", device.real_address)
            return False
        frame = build_frame(
            device_type,
            cmd,
            body,
            protocol=device.protocol,
            seq=self._next_seq(device),
            cmd_type=cmd_type,
        )
        hex_frame = to_hex(frame)
        _LOGGER.debug("-> %s %s", device.real_address, hex_frame)
        ok = await self.ws.send_device_frame(device.real_address, hex_frame)
        if not ok:
            _LOGGER.debug("Frame dropped, websocket offline")
        return ok

    def _next_seq(self, device: NetleaDevice) -> int:
        """Sequence number used by the 0x6A protocol frames."""
        seq = self._seq.get(device.real_address, 1) + 1
        if seq > 0xFFF0:
            seq = 1
        self._seq[device.real_address] = seq
        return seq

    # -- public command helpers ---------------------------------------- #
    async def async_set_power(self, device: NetleaDevice, turn_on: bool) -> None:
        if device.product_id == PRODUCT_LAMP:
            await self.async_set_lamp(device, power=turn_on)
        elif device.product_id == PRODUCT_UV:
            merged = device.merged_state()
            body = bytes(
                [
                    merged.get("runMode", 0),
                    1 if turn_on else 0,
                    merged.get("automatic", 0),
                    0,
                    merged.get("pwm", 0),
                    merged.get("lv", 1),
                    0,
                    0,
                    0,
                    0,
                ]
            )
            await self._async_send(device, 48, body, cmd_type=0x10)
        else:
            raise NotImplementedError(f"power control not mapped for {device.product_id}")

    async def async_set_lamp(
        self,
        device: NetleaDevice,
        *,
        power: bool | None = None,
        grade: int | None = None,
        channels: dict[str, int] | None = None,
        lamp_mode: int | None = None,
    ) -> None:
        """One entry point for every lamp write (cmd 16, `lt()` in the client).

        Whatever is not passed is echoed from the last device report so that
        only the intended field changes:

            lampState, lampMode, r1, r2, R, G, W, B, F,
            restoreActTime(4 LE), modeId, grade(4 LE), r3, r4

        `lampMode` decides whether the device accepts the new values right
        away: 0 = 临时/手动 (paired with restoreActTime), 1 = 跟随行程 (the value
        the app's home card sends).  Default comes from the `lamp_mode` option.
        """
        merged = device.merged_state()
        current = device.channel_map()
        target = dict(current)
        if channels:
            for key, value in channels.items():
                if key in target:
                    target[key] = max(0, min(CHANNEL_MAX, int(value)))

        if power is True and not any(target.values()):
            # No report yet (or everything dimmed to 0): use the model's own
            # default spectrum so the switch produces light, not a black lamp.
            target = {"R": 37, "W": 31, "G": 55, "B": 34, "F": 0}

        if power is not None:
            state = int(bool(power))
        elif device.intent_power is not None:
            # A pending switch-on must survive the following channel writes.
            state = int(bool(device.intent_power))
        else:
            state = merged.get("lampState", 1)
        if lamp_mode is None:
            option = self.lamp_mode_option
            if option in (None, "", "auto"):
                reported = merged.get("lampMode")
                lamp_mode = LAMP_MODE_AUTOMATIC if reported is None else int(reported)
            else:
                lamp_mode = int(option)
        # Echo whatever the lamp reports (custom spectra legitimately use
        # grade 0); only fall back to a sane default when we have no data yet.
        final_grade = max(
            0,
            min(GRADE_MAX, int(grade if grade is not None else merged.get("grade", 5))),
        )
        body = build_lamp_body(
            lamp_state=state,
            lamp_mode=lamp_mode,
            channels=target,
            restore_act_time=merged.get("restoreActTime", 1000),
            mode_id=merged.get("modeId", 0),
            grade=final_grade,
        )
        if body == device.last_body:
            # A scene restore touches every entity one after another; once the
            # body stops changing there is nothing left to tell the device.
            _LOGGER.debug(
                "set_lamp %s 内容与上一帧相同，跳过发送", device.real_address
            )
            device.intent_power = bool(state)
            device.intent_power_at = time.monotonic()
            return

        _LOGGER.debug(
            "set_lamp %s state=%s mode=%s grade=%s channels=%s",
            device.real_address,
            state,
            lamp_mode,
            final_grade,
            target,
        )
        ok = await self._async_send(device, 16, body)
        if ok:
            device.intent_power = bool(state)
            device.intent_power_at = time.monotonic()
            device.last_body = body

    async def async_set_channel(self, device: NetleaDevice, key: str, value: int) -> None:
        await self.async_set_lamp(device, channels={key: value})

    async def async_set_brightness(self, device: NetleaDevice, grade: int) -> None:
        if device.product_id == PRODUCT_LAMP:
            await self.async_set_lamp(device, grade=int(grade))
        elif device.product_id == PRODUCT_UV:
            merged = device.merged_state()
            body = bytes(
                [
                    merged.get("runMode", 0),
                    merged.get("onOff", 1),
                    merged.get("automatic", 0),
                    0,
                    max(0, min(255, int(grade))),
                    merged.get("lv", 1),
                    0,
                    0,
                    0,
                    0,
                ]
            )
            await self._async_send(device, 48, body, cmd_type=0x10)
        else:
            raise NotImplementedError

    async def async_set_color_rgbw(
        self, device: NetleaDevice, rgbw: tuple[int, int, int, int]
    ) -> None:
        """Map an HA RGBW colour (0-255 per channel) onto the lamp channels."""
        r, g, b, w = rgbw
        scale = CHANNEL_MAX / 255
        await self.async_set_lamp(
            device,
            power=True,
            channels={
                "R": round(r * scale),
                "W": round(w * scale),
                "G": round(g * scale),
                "B": round(b * scale),
            },
        )

    async def async_send_raw(self, device: NetleaDevice, frame_hex: str) -> bool:
        """Publish a hand crafted frame (advanced / debugging)."""
        return await self.ws.send_device_frame(device.real_address, frame_hex)
