"""Netlea (尼特利) device protocol codec.

Layout observed on the vendor's cloud link (HTTPS + WebSocket) and
verified frame by frame against a live SG-X-PRO-80w lamp.

Two wire protocols are used by the same transport layer:

protocol 0x5A (90) -- classic frame
    +0  0x5A            magic
    +1  LEN             total length including checksum
    +2  DEVICE_TYPE
    +3  CMD             instruction / command id
    +4  BYTE4           request: cmdType (0 = query, 0x10 = set)
                        response: (request_state << 4) | code
    +5  BYTE5           flash_wifi flag (requests), reserved
    +6  ...BODY...
    +n  CHECKSUM        sum(all previous bytes) & 0xFF

protocol 0x6A (106) -- sequenced frame
    +0  0x6A
    +1  LEN
    +2  DEVICE_TYPE
    +3  0x07            header extension length
    +4  SEQ_HI
    +5  SEQ_LO
    +6  CMD
    +7  BYTE7           request: cmdType, response: (request_state << 4) | code
    +8  0x00
    +9  0x00
    +10 ...BODY...
    +n  CHECKSUM

Multi byte fields are little endian (app reads `slice -> reverse -> big endian`,
app writes `hex pad -> reverse`).

Reference implementation in the official client:
    function ml(e,r,o,n)  -> build 0x5A frame   (e=DEVICE_TYPE, r=CMD, o=body)
    function pl(e)        -> build 0x5A/0x6A frame with seq
    function pn(e)        -> checksum
    function cl(...)      -> field reader
    function sl/ll/nl     -> 1 byte / N byte (little endian) field writers
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROTO_5A = 0x5A
PROTO_6A = 0x6A

CMD_TYPE_QUERY = 0x00
CMD_TYPE_SET = 0x10
CMD_TYPE_RECOVER_SET = 0x40

# response codes (low nibble of byte4)
CODE_SUCCESS = 0
CODE_FAIL = 1
CODE_ALARM = 2
CODE_INVALID = 3
CODE_TOKEN_FAIL = 7

# request state (high nibble of byte4 in responses)
RS_QUERY = 0
RS_SET = 1
RS_REPORTING = 2
RS_RECOVER = 4

CODE_NAMES = {
    CODE_SUCCESS: "success",
    CODE_FAIL: "fail",
    CODE_ALARM: "alarm",
    CODE_INVALID: "invalid",
    CODE_TOKEN_FAIL: "token_fail",
}


# --------------------------------------------------------------------------- #
# checksum / byte helpers
# --------------------------------------------------------------------------- #
def checksum(data: bytes) -> int:
    """`pn()` in the app: plain sum modulo 256."""
    return sum(data) & 0xFF


def to_hex(data: bytes) -> str:
    """`ol()`: lowercase, two chars per byte."""
    return "".join(f"{b:02x}" for b in data)


def from_hex(text: str) -> bytes:
    text = "".join(c for c in text if c in "0123456789abcdefABCDEF")
    if len(text) % 2:
        text = "0" + text
    return bytes.fromhex(text)


def le(value: int, size: int) -> bytes:
    """`nl([value], size, True)` -> little endian, `size` bytes."""
    return int(value).to_bytes(size, "little", signed=False)


def read_field(body: bytes, offset: int, size: int, little_endian: bool = True) -> int:
    chunk = body[offset : offset + size]
    if not chunk:
        return 0
    return int.from_bytes(chunk, "little" if little_endian else "big")


# --------------------------------------------------------------------------- #
# frame builders
# --------------------------------------------------------------------------- #
def build_5a(device_type: int, cmd: int, body: bytes = b"", byte4: int = 0, byte5: int = 0) -> bytes:
    length = 6 + len(body) + 1
    frame = bytes([PROTO_5A, length & 0xFF, device_type & 0xFF, cmd & 0xFF, byte4 & 0xFF, byte5 & 0xFF]) + body
    return frame + bytes([checksum(frame)])


def build_6a(device_type: int, cmd: int, body: bytes = b"", seq: int = 1, cmd_type: int = 0) -> bytes:
    length = 3 + 7 + len(body) + 1
    seq &= 0xFFFF
    frame = bytes(
        [
            PROTO_6A,
            length & 0xFF,
            device_type & 0xFF,
            0x07,
            (seq >> 8) & 0xFF,
            seq & 0xFF,
            cmd & 0xFF,
            cmd_type & 0xFF,
            0x00,
            0x00,
        ]
    ) + body
    return frame + bytes([checksum(frame)])


def build_frame(
    device_type: int,
    cmd: int,
    body: bytes = b"",
    protocol: int = PROTO_5A,
    seq: int = 1,
    cmd_type: int = 0,
) -> bytes:
    """Build a request frame.

    `cmd_type` is byte 4 of a 0x5A frame / byte 7 of a 0x6A frame.  The lamp
    commands in the official client leave it at 0 (the command number itself implies the
    action); the UV / feeder / fish-tank commands set 0x10 (`sn` = CmdSetMode).
    """
    if protocol == PROTO_6A:
        return build_6a(device_type, cmd, body, seq=seq, cmd_type=cmd_type)
    return build_5a(device_type, cmd, body, byte4=cmd_type)


# --------------------------------------------------------------------------- #
# frame parser
# --------------------------------------------------------------------------- #
@dataclass
class Frame:
    raw: bytes
    protocol: int
    device_type: int
    cmd: int
    body: bytes
    code: int = CODE_SUCCESS
    request_state: int = RS_QUERY
    seq: int = 0
    checksum_ok: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_report(self) -> bool:
        return self.request_state == RS_REPORTING

    @property
    def code_name(self) -> str:
        return CODE_NAMES.get(self.code, f"code_{self.code}")

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"<Frame proto={self.protocol:#x} type={self.device_type} cmd={self.cmd} "
            f"state={self.request_state} code={self.code_name} body={self.body.hex()}>"
        )


def parse_frame(data: bytes) -> Frame | None:
    """Parse a device frame. Returns None when the buffer is not a known frame."""
    if len(data) < 7:
        return None
    ok = checksum(data[:-1]) == data[-1]
    head = data[0]
    if head == PROTO_5A:
        length = data[1] or len(data)
        raw = data[:length]
        if not ok:
            ok = checksum(raw[:-1]) == raw[-1]
        payload = raw[:-1]
        return Frame(
            raw=raw,
            protocol=PROTO_5A,
            device_type=payload[2],
            cmd=payload[3],
            body=payload[6:],
            code=payload[4] & 0x0F,
            request_state=(payload[4] & 0xF0) >> 4,
            checksum_ok=ok,
            extra={"byte5": payload[5]},
        )
    if head == PROTO_6A:
        ext = data[3] if len(data) > 3 else 7
        header_len = 3 + ext
        length = data[1] or len(data)
        raw = data[:length]
        flag = raw[7] if len(raw) > 7 else 0
        return Frame(
            raw=raw,
            protocol=PROTO_6A,
            device_type=raw[2],
            cmd=raw[6] if len(raw) > 6 else 0,
            body=raw[header_len:-1],
            code=flag & 0x0F,
            request_state=(flag & 0xF0) >> 4,
            seq=(raw[4] << 8) | raw[5] if len(raw) > 5 else 0,
            checksum_ok=checksum(raw[:-1]) == raw[-1],
        )
    return None


# --------------------------------------------------------------------------- #
# field decoding
# --------------------------------------------------------------------------- #
def decode_fields(spec: dict[str, tuple[int, int]], body: bytes) -> dict[str, Any]:
    """`cl()` equivalent.

    `spec` maps field name -> (offset, size).  Size 1 means a single byte,
    larger sizes are little endian integers.
    """
    out: dict[str, Any] = {}
    for name, (offset, size) in spec.items():
        out[name] = read_field(body, offset, size)
    return out


def seq_fields(names: list[str], start: int, size: int = 1) -> dict[str, tuple[int, int]]:
    """Convenience: consecutive fields of the same size."""
    return {name: (start + i * size, size) for i, name in enumerate(names)}


# Wire order of the master-light channel block: R, W, G, B, F.
# Confirmed against the live device (SG-X-PRO-80w): the cloud `statusLog`
# stores the last cmd-16 frame the app sent, and its five channel bytes
# `25 1f 37 22 00` decode to R=37 W=31 G=55 B=34, which is exactly the
# "水草/正白" spectrum #5 in that model's `zhudengConfig`.  The channel names
# also come from `mcustomConfig[0].custom` (custom1_text=R … custom5_text=F).
CHANNEL_ORDER: tuple[str, ...] = ("R", "W", "G", "B", "F")

# Channel levels are 0-101 % (custom1Max = 101 in the device config).
CHANNEL_MAX = 100
# `grade` is the spectrum preset index (档位) 1-10, not a percentage.
GRADE_MIN = 0
GRADE_MAX = 10

# `lampMode` values (constants `mode_light_*` in the client)
LAMP_MODE_MANUAL = 0      # 临时/手动，配合 restoreActTime 使用
LAMP_MODE_AUTOMATIC = 1   # 自动（跟随行程）—— App 首页灯卡的默认值
LAMP_MODE_TEMPORARY = 2


# deviceType 1 -- 水草灯 / 灯具  (client lamp module)
LAMP_DECODERS: dict[int, dict[str, tuple[int, int]]] = {
    20: {
        # Verified on a live SG-X-PRO-80w: bytes 6..10 are R, W, G, B, F.
        # (The client's own decoder labels them R,G,W,B,F, swapping W and G.)
        **seq_fields(
            [
                "stateType",
                "startH",
                "startM",
                "lampState",
                "lampMode",
                "pattern",
                "R",
                "W",
                "G",
                "B",
                "F",
                "reserve1",
                "reserve2",
            ],
            0,
        ),
        **{f"spot_{k}": (13 + i, 1) for i, k in enumerate(
            ["stateType", "startH", "startM", "lampState", "lampMode", "pattern", "moodLamp", "followType", "reserve1"]
        )},
        **{f"atm_{k}": (22 + i, 1) for i, k in enumerate(
            ["stateType", "startH", "startM", "lampState", "lampMode", "pattern", "moodLamp", "followType", "reserve1"]
        )},
    },
    26: {
        "on": (0, 2),
        "off": (2, 2),
        "R": (4, 1),
        "W": (5, 1),
        "G": (6, 1),
        "B": (7, 1),
        "F": (8, 1),
        "modeId": (9, 1),
        "grade": (10, 2),
    },
    28: {"on": (0, 4), "off": (4, 4), "moodLamp": (8, 1), "grade": (9, 1)},
    32: {"on": (0, 4), "off": (4, 4), "moodLamp": (8, 1), "grade": (9, 1)},
    30: {
        "lampState": (0, 1),
        "lampMode": (1, 1),
        "reserve1": (2, 1),
        "reserve2": (3, 1),
        "moodLamp": (4, 1),
        "restoreActTime": (5, 4),
        "grade": (9, 1),
        "reserve3": (10, 1),
    },
    16: {
        "lampState": (0, 1),
        "lampMode": (1, 1),
        "reserve1": (2, 1),
        "reserve2": (3, 1),
        "R": (4, 1),
        "W": (5, 1),
        "G": (6, 1),
        "B": (7, 1),
        "F": (8, 1),
        "restoreActTime": (9, 2),
        "modeId": (11, 1),
        "grade": (12, 2),
        "reserve3": (14, 1),
        "reserve4": (15, 1),
    },
    17: {
        "lampState": (0, 1),
        "lampMode": (1, 1),
        "reserve1": (2, 1),
        "reserve2": (3, 1),
        "moodLamp": (4, 1),
        "restoreActTime": (5, 4),
        "grade": (9, 1),
        "reserve3": (10, 1),
    },
    22: {"followType": (0, 1), "channel": (1, 1), "grade": (2, 1)},
    34: {"followType": (0, 1), "channel": (1, 1), "grade": (2, 1)},
}

# deviceType 7 -- UV 杀菌灯  (client UV module)
UV_DECODERS: dict[int, dict[str, tuple[int, int]]] = {
    48: {
        "runMode": (0, 1),
        "onOff": (1, 1),
        "automatic": (2, 1),
        "reserve1": (3, 1),
        "pwm": (4, 1),
        "lv": (5, 1),
        "restoreActTime": (6, 2),
        "reserve4": (8, 1),
    },
    50: {
        "controlMode": (0, 1),
        "runMode": (1, 1),
        "onOff": (2, 1),
        "automatic": (3, 1),
        "isAutomaticSet": (4, 1),
        "stateH": (5, 1),
        "stateM": (6, 1),
        "pwm": (7, 1),
        "lv": (8, 1),
        "restoreActTime": (9, 2),
        "TProtect": (11, 1),
    },
    51: {
        "onOff": (0, 1),
        "openMin": (1, 2),
        "closeMin": (3, 2),
        "pwm": (5, 1),
        "lv": (6, 1),
        "priorityOnOff": (7, 1),
        "second": (8, 2),
    },
    34: {
        "reserve1": (0, 1),
        "buzzMode": (1, 1),
        "lockMode": (2, 1),
        "reserve2": (3, 1),
    },
}

DECODERS: dict[int, dict[int, dict[str, tuple[int, int]]]] = {
    1: LAMP_DECODERS,
    7: UV_DECODERS,
}

# commands used to provoke a status report from the device
QUERY_FRAMES: dict[int, list[int]] = {
    1: [27, 29, 33],  # 主灯 / 射灯 / 氛围灯
    7: [49, 50],
}


def decode_frame(frame: Frame) -> dict[str, Any]:
    """Decode a frame body into a flat dict of named values."""
    table = DECODERS.get(frame.device_type, {})
    spec = table.get(frame.cmd)
    if not spec:
        return {}
    return decode_fields(spec, frame.body)


# --------------------------------------------------------------------------- #
# lamp command body (cmd 16, `lt()` in api/ble/LampDevice.js)
# --------------------------------------------------------------------------- #
def build_lamp_body(
    *,
    lamp_state: int = 1,
    lamp_mode: int = LAMP_MODE_AUTOMATIC,
    channels: dict[str, int] | None = None,
    restore_act_time: int = 1000,
    mode_id: int = 0,
    grade: int = 5,
) -> bytes:
    """Assemble the 16 byte cmd 16 payload (verified against a live device).

        lampState, lampMode, r1, r2, R, W, G, B, F,
        restoreActTime(2 LE), modeId, grade(2 LE), r3, r4

    A real frame captured from the vendor cloud:
        body = 0101 0000 251f372200 e803 00 0500 0000
               ^state ^mode  ^channels  ^1000 ^modeId ^grade=5
    """
    channels = channels or {}
    body = bytes([int(lamp_state) & 0xFF, int(lamp_mode) & 0xFF, 0, 0])
    body += bytes(int(channels.get(key, 0) or 0) & 0xFF for key in CHANNEL_ORDER)
    body += le(int(restore_act_time) & 0xFFFF, 2)
    body += bytes([int(mode_id) & 0xFF])
    body += le(int(grade) & 0xFFFF, 2)
    body += bytes([0, 0])
    return body
