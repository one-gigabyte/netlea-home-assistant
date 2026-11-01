"""Cloud transport for the Netlea integration.

Two channels, both used by the official Android app:

* HTTPS  `https://v3api.netlea.com/prod-api`  -- login, inventory, snapshots.
  Auth header is the raw token (`Authorization: <token>`), every response body
  is JSON with a `code` field (200 == ok, 401/409 == session expired).

* WebSocket `ws://v3grpc.netlea.com:8088/websocket` -- device traffic.
  The handshake carries the same token in the `token` header.  Frames are JSON:
      {"msgType": "1001", "msgBody": null}                 -> online device list
      {"msgType": "1002", "msgBody": {...}}                -> send a device command
      {"msgType": "2001", "msgBody": {...}}                -> online state changed
      {"msgType": "ping"}                                  -> keepalive
  A control request body is
      {"time": <ms>, "all": "1", "topic": "App/Control/<realAddress>",
       "message": "<hex of the device frame>"}
  and the reply / spontaneous report arrives as msgType 1002 with
      {"code": 200, "msgBody": {"realAddress": "<addr>", "message": "<hex>"}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .const import (
    API_BASE,
    EP_CAPTCHA,
    EP_DEVICE_INFO,
    EP_DEVICE_LIST,
    EP_LOGIN_BY_CODE,
    EP_SYNC_DEVICES,
    EP_SYS_CONFIG,
    MSG_CONTROL,
    MSG_ONLINE_LIST,
    MSG_PING,
    WS_PING_INTERVAL,
    WS_TOPIC_PREFIX,
    WS_URL,
)

_LOGGER = logging.getLogger(__name__)


class NetleaAuthError(Exception):
    """Raised when the token is rejected."""


class NetleaApiError(Exception):
    """Raised for any other cloud failure."""


class NetleaApi:
    """Thin async wrapper around the vendor REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str | None = None,
        user_id: str | int | None = None,
    ) -> None:
        self._session = session
        self.token = token
        self.user_id: str | int | None = user_id
        self.user_name: str | None = None

    # -- helpers ---------------------------------------------------------- #
    def _headers(self, auth: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Language": "zh-Hans",
        }
        if auth and self.token:
            headers["Authorization"] = str(self.token)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        if params:
            url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                headers=self._headers(auth),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
        except asyncio.TimeoutError as err:
            raise NetleaApiError(f"timeout calling {path}") from err
        except aiohttp.ClientError as err:
            raise NetleaApiError(f"network error calling {path}: {err}") from err

        if resp.status != 200:
            raise NetleaApiError(f"HTTP {resp.status} from {path}: {text[:200]}")

        try:
            payload = json.loads(text)
        except ValueError as err:
            raise NetleaApiError(f"non-JSON reply from {path}: {text[:200]}") from err

        code = payload.get("code")
        if code in (401, 409):
            raise NetleaAuthError(payload.get("msg") or "登录状态已过期")
        if code not in (200, "200", None):
            raise NetleaApiError(payload.get("msg") or f"error code {code} from {path}")
        return payload

    # -- auth ------------------------------------------------------------- #
    async def request_sms_code(self, phone: str) -> str:
        """Trigger an SMS verification code; returns the uuid used for login.

        The app calls this with GET + query params (`Up()` in
        the app login screen); POST is answered with HTTP 500.
        """
        payload = await self._request(
            "GET",
            EP_CAPTCHA,
            params={"captchaMsgFlag": "3", "phonenumber": phone},
            auth=False,
        )
        body = _unwrap(payload)
        uuid = body.get("uuid") or payload.get("uuid")
        if not uuid:
            raise NetleaApiError(payload.get("msg") or "服务器未返回 uuid，验证码发送失败")
        return str(uuid)

    async def login_with_code(self, phone: str, code: str, uuid: str) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            EP_LOGIN_BY_CODE,
            json_body={
                "code": code,
                "username": phone,
                "uuid": uuid,
                "appVer": "v3(android-14:HomeAssistant)",
            },
            auth=False,
        )
        token = payload.get("token")
        if not token:
            raise NetleaAuthError(payload.get("msg") or "登录失败：未返回 token")
        self.token = str(token)
        self.user_id = payload.get("userId")
        self.user_name = payload.get("userName") or payload.get("nickName")
        return payload

    async def validate_token(self) -> dict[str, Any]:
        """Cheap authenticated call used to verify a pasted token."""
        payload = await self._request("GET", EP_SYS_CONFIG)
        return payload

    # -- data ------------------------------------------------------------- #
    async def get_sys_config(self) -> dict[str, Any]:
        return await self._request("GET", EP_SYS_CONFIG)

    async def get_devices(self, page_size: int = 100) -> list[dict[str, Any]]:
        """Device inventory with the last known `statusLog` snapshot.

        Two paths, tried in order:

        1. `POST /V3/Api/batchSyncEquipmentUser` with an empty entity list —
           this is what the app itself calls on every home-page show
           (`ln()` on the app home screen).  It returns the account's whole
           device table, which is why a fresh client can rebuild its inventory.
        2. `GET /V3/Api/getWifiCollabUserDeviceList` — the linkage picker
           endpoint.  Some accounts answer it with a generic 500
           (`{upload.image.exception}`), so it is only a fallback.
        """
        errors: list[str] = []

        for uid in ([self.user_id] if self.user_id is not None else []) + [None]:
            try:
                rows = await self._sync_devices(uid)
            except (NetleaApiError, NetleaAuthError) as err:
                errors.append(f"batchSync(userId={uid}): {err}")
                if isinstance(err, NetleaAuthError):
                    raise
                continue
            if rows:
                # learn the account id from the payload when it was unknown
                if self.user_id is None:
                    learned = rows[0].get("userId") if isinstance(rows[0], dict) else None
                    if learned:
                        self.user_id = learned
                return rows

        for uid in ([self.user_id] if self.user_id is not None else []) + [None]:
            try:
                rows = await self._list_devices(page_size, uid)
            except (NetleaApiError, NetleaAuthError) as err:
                errors.append(f"collabList(userId={uid}): {err}")
                if isinstance(err, NetleaAuthError):
                    raise
                continue
            if rows:
                return rows

        if errors:
            raise NetleaApiError("; ".join(errors))
        return []

    async def _sync_devices(self, user_id: Any = None) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "client": 0,
            "equipmentUserEntityList": [],
        }
        if user_id is not None:
            body["userId"] = user_id
        payload = await self._request("POST", EP_SYNC_DEVICES, json_body=body)
        rows = payload.get("rows")
        if rows is None:
            rows = _unwrap(payload).get("rows") or []
        return list(rows or [])

    async def _list_devices(self, page_size: int, user_id: Any = None) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []
        page = 1
        while page <= 10:
            payload = await self._request(
                "GET",
                EP_DEVICE_LIST,
                params={
                    "pageNum": page,
                    "pageSize": page_size,
                    "userId": user_id,
                },
            )
            rows = payload.get("rows") or _unwrap(payload).get("rows") or []
            if not rows:
                break
            devices.extend(rows)
            if len(rows) < page_size:
                break
            page += 1
        return devices

    async def get_device_info(self, real_address: str) -> dict[str, Any]:
        payload = await self._request(
            "GET", EP_DEVICE_INFO, params={"realAddress": real_address}
        )
        return _unwrap(payload)


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


# --------------------------------------------------------------------------- #
# WebSocket client
# --------------------------------------------------------------------------- #
class NetleaWebSocket:
    """Reconnecting websocket client for device traffic."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token_provider: Callable[[], str | None],
        on_frame: Callable[[str, dict[str, Any]], None],
        on_online_list: Callable[[list[dict[str, Any]]], None],
        on_connection: Callable[[bool], None] | None = None,
    ) -> None:
        self._session = session
        self._token_provider = token_provider
        self._on_frame = on_frame
        self._on_online_list = on_online_list
        self._on_connection = on_connection
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._closing = False
        self.connected = False

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> None:
        self._closing = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="netlea-ws")

    async def stop(self) -> None:
        self._closing = True
        for task in (self._ping_task, self._task):
            if task and not task.done():
                task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self.connected = False

    async def _run(self) -> None:
        delay = 2
        while not self._closing:
            token = self._token_provider()
            if not token:
                await asyncio.sleep(5)
                continue
            try:
                await self._connect(token)
                delay = 2
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                _LOGGER.warning("Netlea websocket error: %s", err)
            finally:
                self.connected = False
                if self._on_connection:
                    self._on_connection(False)
                if self._ping_task:
                    self._ping_task.cancel()
                    self._ping_task = None
            if self._closing:
                break
            _LOGGER.debug("Netlea websocket reconnecting in %ss", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _connect(self, token: str) -> None:
        _LOGGER.debug("Connecting Netlea websocket")
        self._ws = await self._session.ws_connect(
            WS_URL,
            headers={"token": str(token), "contentType": "application/json"},
            heartbeat=None,
            timeout=30,
            max_msg_size=0,
        )
        self.connected = True
        _LOGGER.info("Netlea websocket connected")
        if self._on_connection:
            self._on_connection(True)
        self._ping_task = asyncio.create_task(self._ping_loop(), name="netlea-ws-ping")
        await self.request_online_list()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type is aiohttp.WSMsgType.TEXT:
                self._handle_text(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        _LOGGER.debug("Netlea websocket closed")

    async def _ping_loop(self) -> None:
        try:
            while not self._closing and self.connected:
                await asyncio.sleep(WS_PING_INTERVAL)
                if self._ws and not self._ws.closed:
                    await self._ws.send_json({"msgType": MSG_PING})
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Netlea ping failed: %s", err)

    # -- inbound ---------------------------------------------------------- #
    def _handle_text(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Netlea ws non-JSON payload: %s", raw[:120])
            return
        msg_type = message.get("msgType")
        if msg_type in (MSG_PING, "pong"):
            return
        if msg_type == MSG_ONLINE_LIST:
            body = message.get("msgBody") or []
            if isinstance(body, list):
                self._on_online_list(body)
            return
        if msg_type == "2001":
            body = message.get("msgBody") or {}
            if isinstance(body, dict) and body.get("realAddress"):
                self._on_online_list([body])
            return
        if msg_type == MSG_CONTROL:
            body = message.get("msgBody") or {}
            if isinstance(body, dict) and body.get("message"):
                self._on_frame(str(body.get("realAddress") or ""), message)
            return
        _LOGGER.debug("Netlea ws unhandled message: %s", raw[:200])

    # -- outbound --------------------------------------------------------- #
    async def _send(self, payload: dict[str, Any]) -> bool:
        if not self._ws or self._ws.closed:
            _LOGGER.debug("Netlea ws not connected, dropping %s", payload.get("msgType"))
            return False
        try:
            await self._ws.send_json(payload)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Netlea ws send failed: %s", err)
            return False

    async def request_online_list(self) -> bool:
        return await self._send({"msgType": MSG_ONLINE_LIST, "msgBody": None})

    async def send_device_frame(self, real_address: str, frame_hex: str) -> bool:
        """Publish a raw device frame through the vendor MQTT bridge."""
        return await self._send(
            {
                "msgType": MSG_CONTROL,
                "msgBody": {
                    "time": int(time.time() * 1000),
                    "all": "1",
                    "topic": f"{WS_TOPIC_PREFIX}{real_address}",
                    "message": frame_hex,
                },
            }
        )
