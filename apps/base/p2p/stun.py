"""内置 STUN 服务（RFC 5389 Binding）。

站点自带的 STUN：**不依赖任何第三方 STUN，也不需要额外部署 coturn**。
以 asyncio UDP 协议随应用进程一起启停，因此「站点可达即 STUN 可达」，
换访问入口（内网 IP / 虚拟网 IP / 域名）都不需要改配置。

实现范围只覆盖 ICE 收集 srflx 候选所需的最小集合 —— Binding 请求/响应。
TURN 中继不在本模块职责内（中继需要 coturn，属独立议题，见
`docs/p2p-design.md` §9.3）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import struct
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101
BINDING_ERROR = 0x0111

ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_ERROR_CODE = 0x0009
ATTR_SOFTWARE = 0x8022

HEADER_LENGTH = 20
TRANSACTION_ID_LENGTH = 12
SOFTWARE_NAME = b"FileCodeBox-STUN"

# 只接受 Binding 方法；TURN Allocate / Refresh 等不属于本服务职责
SUPPORTED_MESSAGE_TYPE = BINDING_REQUEST

Address = Tuple[str, int]


def _pack_attribute(attr_type: int, value: bytes) -> bytes:
    """封装 TLV 属性，长度补齐到 4 字节边界（RFC 5389 §15）。"""
    padding = (4 - len(value) % 4) % 4
    return struct.pack(">HH", attr_type, len(value)) + value + b"\x00" * padding


def _build_xor_mapped_address(address: Address, transaction_id: bytes) -> bytes:
    """构造 XOR-MAPPED-ADDRESS 属性的值。

    IPv4 用 magic cookie 做掩码；IPv6 用 magic cookie + transaction id（RFC 5389 §15.2）。
    """
    ip = ipaddress.ip_address(address[0])
    xored_port = address[1] ^ (MAGIC_COOKIE >> 16)

    if ip.version == 4:
        mask = struct.pack(">I", MAGIC_COOKIE)
        family = 0x01
    else:
        mask = struct.pack(">I", MAGIC_COOKIE) + transaction_id
        family = 0x02

    xored_ip = bytes(a ^ b for a, b in zip(ip.packed, mask))
    return struct.pack(">BBH", 0, family, xored_port) + xored_ip


def build_binding_success(transaction_id: bytes, address: Address) -> bytes:
    """Binding 成功响应：告诉客户端「服务端看到的你的来源地址」。"""
    body = _pack_attribute(
        ATTR_XOR_MAPPED_ADDRESS,
        _build_xor_mapped_address(address, transaction_id),
    )
    body += _pack_attribute(ATTR_SOFTWARE, SOFTWARE_NAME)
    header = struct.pack(">HHI", BINDING_SUCCESS, len(body), MAGIC_COOKIE)
    return header + transaction_id + body


def build_binding_error(
    transaction_id: bytes, code: int = 400, reason: str = "Bad Request"
) -> bytes:
    """Binding 错误响应（RFC 5389 §15.6，ERROR-CODE 的高位字节保留为 0）。"""
    class_byte = (code // 100) & 0x07
    number_byte = code % 100
    value = struct.pack(">HBB", 0, class_byte, number_byte) + reason.encode("utf-8")
    body = _pack_attribute(ATTR_ERROR_CODE, value)
    header = struct.pack(">HHI", BINDING_ERROR, len(body), MAGIC_COOKIE)
    return header + transaction_id + body


def handle_packet(data: bytes, address: Address) -> Optional[bytes]:
    """处理一个 UDP 报文，返回需回送的字节；无需响应时返回 None。

    静默忽略是刻意的：STUN 端口暴露在公网，任何非法/无关报文都不应放大回包。
    """
    if len(data) < HEADER_LENGTH:
        return None

    try:
        message_type, _length, magic = struct.unpack(">HHI", data[:8])
    except struct.error:
        return None

    # 非 RFC 5389 报文（例如老式 RFC 3489、无 magic cookie）一律忽略
    if magic != MAGIC_COOKIE:
        return None

    transaction_id = data[8:HEADER_LENGTH]
    if message_type != SUPPORTED_MESSAGE_TYPE:
        return None

    return build_binding_success(transaction_id, address)


class StunProtocol(asyncio.DatagramProtocol):
    """UDP 协议实现，统计请求/响应数便于观测。"""

    def __init__(self) -> None:
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.requests = 0
        self.responses = 0

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self.requests += 1
        response = handle_packet(data, addr)
        if response is not None and self.transport is not None:
            self.transport.sendto(response, addr)
            self.responses += 1

    def error_received(self, exc: Exception) -> None:  # type: ignore[override]
        logger.debug("STUN 套接字错误：%s", exc)


async def start_stun_server(host: str = "0.0.0.0", port: int = 3478):
    """启动内置 STUN，返回 (transport, protocol)。端口被占用时由调用方处理。"""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        StunProtocol, local_addr=(host, port)
    )
    logger.info("内置 STUN 已启动：%s:%s/udp", host, port)
    return transport, protocol
