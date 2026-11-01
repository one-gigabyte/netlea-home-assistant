#!/usr/bin/env python3
"""Standalone Netlea cloud probe — no Home Assistant needed.

    python3 tools/netlea_probe.py login      # 手机号 + 短信验证码，token 存到 ~/.netlea_token
    python3 tools/netlea_probe.py config     # getSysConfig（免鉴权）
    python3 tools/netlea_probe.py devices    # 设备清单 + statusLog
    python3 tools/netlea_probe.py frame 1 27 00          # 构造帧并打印 hex
    python3 tools/netlea_probe.py frame 1 16 <body-hex>

只依赖标准库，用来在部署前确认账号、设备地址和帧内容。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://v3api.netlea.com/prod-api"
TOKEN_FILE = os.path.expanduser("~/.netlea_token")


def call(method: str, path: str, params: dict | None = None, body: dict | None = None, token: str | None = None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        return {"code": err.code, "msg": err.read().decode()[:300]}


def save_token(token: str) -> None:
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        fh.write(token)
    print(f"token 已保存到 {TOKEN_FILE}")


def load_token() -> str | None:
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE, encoding="utf-8").read().strip()
    return None


def cmd_login() -> None:
    phone = input("手机号: ").strip()
    r = call("GET", "/V3/Api/captchaMsg", params={"captchaMsgFlag": "3", "phonenumber": phone})
    print("验证码请求:", r)
    uuid = r.get("uuid")
    if not uuid:
        print("未拿到 uuid，退出")
        return
    code = input("短信验证码: ").strip()
    r = call(
        "POST",
        "/V3/Api/appuser/loginBycode",
        body={
            "code": code,
            "username": phone,
            "uuid": uuid,
            "appVer": "v3(android-14:probe)",
        },
    )
    if r.get("token"):
        save_token(str(r["token"]))
        print("登录成功:", {k: r.get(k) for k in ("userId", "userName", "nickName")})
    else:
        print("登录失败:", r)


def cmd_config() -> None:
    print(json.dumps(call("GET", "/V3/Api/getSysConfig"), ensure_ascii=False, indent=2))


def cmd_devices() -> None:
    token = load_token()
    if not token:
        print("先运行 login")
        return
    user_id = os.environ.get("NETLEA_USER_ID") or (sys.argv[2] if len(sys.argv) > 2 else None)

    print("=== 1) POST /V3/Api/batchSyncEquipmentUser （App 首页用的就是这个）===")
    body = {"client": 0, "equipmentUserEntityList": []}
    if user_id:
        body["userId"] = user_id
    r = call("POST", "/V3/Api/batchSyncEquipmentUser", body=body, token=token)
    print(json.dumps(r, ensure_ascii=False, indent=2)[:4000])
    rows = r.get("rows") or []

    if not rows:
        print("\n=== 2) GET /V3/Api/getWifiCollabUserDeviceList （备用）===")
        r2 = call(
            "GET",
            "/V3/Api/getWifiCollabUserDeviceList",
            params={"pageNum": 1, "pageSize": 100, "userId": user_id},
            token=token,
        )
        print(json.dumps(r2, ensure_ascii=False, indent=2)[:4000])
        rows = r2.get("rows") or []

    if rows:
        print(f"\n=== 摘要：{len(rows)} 台设备 ===")
        for row in rows:
            status = row.get("statusLog")
            if isinstance(status, str):
                try:
                    status = json.loads(status)
                except ValueError:
                    status = {}
            status = status or {}
            product = row.get("equipmentProductId") or (row.get("equipmentType") or {}).get(
                "equipmentProductId"
            )
            print(
                f"- {row.get('name')} | realAddress={row.get('realAddress')} "
                f"| product={product} | protocol={status.get('protocolType')} "
                f"| fw={status.get('firmwareVersion')} | userId={row.get('userId')}"
            )


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def cmd_frame(device_type: int, cmd: int, body_hex: str = "", protocol: int = 0x5A, seq: int = 1, cmd_type: int = 0) -> None:
    body = bytes.fromhex(body_hex) if body_hex else b""
    if protocol == 0x6A:
        frame = bytes(
            [
                0x6A,
                (3 + 7 + len(body) + 1) & 0xFF,
                device_type & 0xFF,
                0x07,
                (seq >> 8) & 0xFF,
                seq & 0xFF,
                cmd & 0xFF,
                cmd_type & 0xFF,
                0,
                0,
            ]
        ) + body
    else:
        frame = bytes([0x5A, (6 + len(body) + 1) & 0xFF, device_type & 0xFF, cmd & 0xFF, cmd_type, 0]) + body
    frame += bytes([checksum(frame)])
    print(f"cmd={cmd} len={len(frame)} hex={frame.hex()}")


def main() -> None:
    argv = sys.argv[1:] or ["config"]
    action = argv[0]
    if action == "login":
        cmd_login()
    elif action == "config":
        cmd_config()
    elif action == "devices":
        cmd_devices()
    elif action == "frame":
        device_type = int(argv[1], 0) if len(argv) > 1 else 1
        cmd = int(argv[2], 0) if len(argv) > 2 else 27
        body = argv[3] if len(argv) > 3 else ""
        protocol = int(argv[4], 0) if len(argv) > 4 else 0x5A
        cmd_type = int(argv[5], 0) if len(argv) > 5 else 0
        cmd_frame(device_type, cmd, body, protocol, cmd_type=cmd_type)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
