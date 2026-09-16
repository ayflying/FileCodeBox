"""内置 STUN 与 STUN 地址派生逻辑的单元测试。

覆盖两件事：
1. `apps/base/p2p/stun.py` 的报文编解码是否符合 RFC 5389（用独立解码逻辑反向校验，
   避免"编码和解码同一个 bug"互相掩盖）。
2. `apps/base/p2p/config.py` 的 STUN 地址派生：不再回退第三方 STUN。
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.base.p2p import stun  # noqa: E402
from apps.base.p2p import config as p2p_config  # noqa: E402


def make_binding_request(transaction_id: bytes = b"\x01" * 12) -> bytes:
    return struct.pack(">HHI", stun.BINDING_REQUEST, 0, stun.MAGIC_COOKIE) + transaction_id


def decode_attributes(payload: bytes) -> dict:
    """独立解码属性（不复用被测代码的编码路径）。"""
    attrs = {}
    pos = stun.HEADER_LENGTH
    while pos + 4 <= len(payload):
        atype, alen = struct.unpack(">HH", payload[pos:pos + 4])
        attrs[atype] = payload[pos + 4:pos + 4 + alen]
        pos += 4 + alen + ((4 - alen % 4) % 4)
    return attrs


def decode_xor_mapped(value: bytes, transaction_id: bytes):
    family = value[1]
    port = struct.unpack(">H", value[2:4])[0] ^ (stun.MAGIC_COOKIE >> 16)
    if family == 0x01:
        mask = struct.pack(">I", stun.MAGIC_COOKIE)
        raw = bytes(a ^ b for a, b in zip(value[4:8], mask))
        import socket as _socket

        return _socket.inet_ntoa(raw), port
    mask = struct.pack(">I", stun.MAGIC_COOKIE) + transaction_id
    raw = bytes(a ^ b for a, b in zip(value[4:20], mask))
    import socket as _socket

    return _socket.inet_ntop(_socket.AF_INET6, raw), port


class StunPacketTests(unittest.TestCase):
    def test_binding_request_gets_success_with_correct_mapped_address(self):
        transaction_id = b"\xab" * 12
        request = make_binding_request(transaction_id)

        response = stun.handle_packet(request, ("203.0.113.7", 54321))

        self.assertIsNotNone(response)
        msg_type, length, magic = struct.unpack(">HHI", response[:8])
        self.assertEqual(msg_type, stun.BINDING_SUCCESS)
        self.assertEqual(magic, stun.MAGIC_COOKIE)
        self.assertEqual(response[8:20], transaction_id)
        # 声明长度必须与真实负载一致，否则客户端会截断解析
        self.assertEqual(length, len(response) - stun.HEADER_LENGTH)

        attrs = decode_attributes(response)
        self.assertIn(stun.ATTR_XOR_MAPPED_ADDRESS, attrs)
        self.assertEqual(
            decode_xor_mapped(attrs[stun.ATTR_XOR_MAPPED_ADDRESS], transaction_id),
            ("203.0.113.7", 54321),
        )

    def test_ipv6_mapped_address_round_trips(self):
        transaction_id = b"\x5a" * 12
        response = stun.handle_packet(
            make_binding_request(transaction_id), ("2001:db8::1", 40000)
        )

        attrs = decode_attributes(response)
        self.assertEqual(
            decode_xor_mapped(attrs[stun.ATTR_XOR_MAPPED_ADDRESS], transaction_id),
            ("2001:db8::1", 40000),
        )

    def test_software_attribute_is_advertised(self):
        response = stun.handle_packet(make_binding_request(), ("10.0.0.1", 1234))
        attrs = decode_attributes(response)
        self.assertEqual(attrs[stun.ATTR_SOFTWARE], stun.SOFTWARE_NAME)

    def test_non_stun_payload_is_ignored(self):
        """STUN 端口暴露在公网，任意垃圾报文都不能触发回包（避免放大）。"""
        self.assertIsNone(stun.handle_packet(b"hello world", ("10.0.0.1", 1)))
        self.assertIsNone(stun.handle_packet(b"", ("10.0.0.1", 1)))
        self.assertIsNone(stun.handle_packet(b"\x00" * 8, ("10.0.0.1", 1)))

    def test_wrong_magic_cookie_is_ignored(self):
        """老式 RFC 3489 报文（无 magic cookie）不做兼容。"""
        packet = struct.pack(">HHI", stun.BINDING_REQUEST, 0, 0xDEADBEEF) + b"\x01" * 12
        self.assertIsNone(stun.handle_packet(packet, ("10.0.0.1", 1)))

    def test_non_binding_method_is_ignored(self):
        """TURN Allocate（0x0003）不属于本服务职责，必须静默丢弃。"""
        packet = struct.pack(">HHI", 0x0003, 0, stun.MAGIC_COOKIE) + b"\x01" * 12
        self.assertIsNone(stun.handle_packet(packet, ("10.0.0.1", 1)))

    def test_error_response_shape(self):
        transaction_id = b"\x11" * 12
        response = stun.build_binding_error(transaction_id, 420, "Unknown Attribute")
        msg_type, _length, _magic = struct.unpack(">HHI", response[:8])
        self.assertEqual(msg_type, stun.BINDING_ERROR)
        self.assertEqual(response[8:20], transaction_id)
        attrs = decode_attributes(response)
        error_value = attrs[stun.ATTR_ERROR_CODE]
        # 高位保留字节为 0，类别 4、编号 20
        self.assertEqual(error_value[0], 0)
        self.assertEqual(error_value[2], 4)
        self.assertEqual(error_value[3], 20)


class HostOnlyTests(unittest.TestCase):
    def test_strips_port(self):
        self.assertEqual(p2p_config._host_only("100.66.1.2:32345"), "100.66.1.2")
        self.assertEqual(p2p_config._host_only("example.com:8080"), "example.com")

    def test_keeps_bare_host(self):
        self.assertEqual(p2p_config._host_only("example.com"), "example.com")
        self.assertEqual(p2p_config._host_only("192.168.50.243"), "192.168.50.243")

    def test_handles_ipv6_literal(self):
        """IPv6 字面量的冒号不能被误当作端口分隔符。"""
        self.assertEqual(p2p_config._host_only("[::1]:8080"), "::1")
        self.assertEqual(p2p_config._host_only("[2001:db8::1]"), "2001:db8::1")

    def test_empty_input(self):
        self.assertEqual(p2p_config._host_only(""), "")
        self.assertEqual(p2p_config._host_only(None), "")


class StunUrlDerivationTests(unittest.TestCase):
    """STUN 地址派生：显式配置优先，否则跟随访问入口，且绝不回退第三方。"""

    def setUp(self):
        from core.settings import settings

        self.settings = settings
        self._saved_user_config = dict(settings.user_config)
        settings.user_config.clear()

    def tearDown(self):
        self.settings.user_config.clear()
        self.settings.user_config.update(self._saved_user_config)

    def test_derives_from_request_host(self):
        urls = p2p_config.p2p_stun_urls(request_host="100.66.1.2:32345")
        self.assertEqual(urls, [f"stun:100.66.1.2:{p2p_config.p2p_stun_port()}"])

    def test_explicit_config_wins(self):
        self.settings.user_config["p2pStunUrls"] = ["stun:stun.example.com:3478"]
        self.assertEqual(
            p2p_config.p2p_stun_urls(request_host="100.66.1.2:32345"),
            ["stun:stun.example.com:3478"],
        )

    def test_no_third_party_fallback_when_host_missing(self):
        """拿不到 Host 时返回空列表，而不是偷偷回退到 Google STUN。"""
        self.assertEqual(p2p_config.p2p_stun_urls(), [])
        self.assertEqual(p2p_config.p2p_stun_urls(request_host=""), [])

    def test_disabled_builtin_yields_no_stun(self):
        self.settings.user_config["p2pStunEnabled"] = 0
        self.assertEqual(p2p_config.p2p_stun_urls(request_host="1.2.3.4"), [])

    def test_custom_port_is_respected(self):
        self.settings.user_config["p2pStunPort"] = 40000
        self.assertEqual(
            p2p_config.p2p_stun_urls(request_host="1.2.3.4"), ["stun:1.2.3.4:40000"]
        )

    def test_invalid_port_falls_back_to_default(self):
        self.settings.user_config["p2pStunPort"] = 0
        self.assertEqual(
            p2p_config.p2p_stun_urls(request_host="1.2.3.4"),
            [f"stun:1.2.3.4:{p2p_config.DEFAULT_STUN_PORT}"],
        )

    def test_default_config_has_no_google_stun(self):
        """默认配置里不得再出现第三方 STUN。"""
        from core.settings import DEFAULT_CONFIG

        self.assertEqual(DEFAULT_CONFIG["p2pStunUrls"], [])


if __name__ == "__main__":
    unittest.main()