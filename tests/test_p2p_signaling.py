"""P2P 直传 P1 阶段（信令骨架 + 房间鉴权）的仓库内单元测试。

覆盖四层纯逻辑，全部脱离网络，可直接 `python -m unittest` 跑：
- tokens.py  ：发布令牌哈希、coturn REST 临时凭据、ICE 配置组装
- config.py  ：站点配置归一化、下发给前端的配置不含静态密钥
- rooms.py   ：进程内房间生命周期、上限与回收、信令转发决策
- migrations_007.py：增量迁移的可重复执行性

真实 WebSocket 端到端与浏览器 WebRTC 验收脚本见 tests/manual/，
那些需要起真实服务，不纳入本文件。
"""

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import time
import unittest

from tortoise import Tortoise

from apps.base.p2p import config as p2p_config
from apps.base.p2p import tokens
from apps.base.p2p.rooms import (
    MAX_CONNECTIONS_PER_IP,
    ROLE_DOWNLOADER,
    ROLE_PUBLISHER,
    TARGET_DOWNLOADERS,
    TARGET_PUBLISHER,
    PeerConnection,
    P2PRoom,
    P2PRoomManager,
    RoomFull,
    SignalingRouter,
    TooManyConnections,
    room_manager,
)
from apps.base.p2p.views import P2P_ALLOWED_EXPIRE_STYLES, _seconds_since
from core.settings import DEFAULT_CONFIG, settings


class _FakeWebSocket:
    """只用于占位比较 identity，rooms 层不调用任何 WS 方法。"""

    def __init__(self, tag: str = ""):
        self.tag = tag

    def __repr__(self) -> str:  # 便于失败时定位
        return f"<FakeWebSocket {self.tag}>"


# --------------------------------------------------------------------------- #
# tokens.py
# --------------------------------------------------------------------------- #


class PublishTokenTests(unittest.TestCase):
    def test_generated_tokens_are_unique_and_long_enough(self):
        sample = {tokens.generate_publish_token() for _ in range(50)}
        self.assertEqual(len(sample), 50)
        self.assertTrue(all(len(item) >= 32 for item in sample))

    def test_hash_is_sha256_hex(self):
        token = "abc123"
        expected = hashlib.sha256(b"abc123").hexdigest()
        self.assertEqual(tokens.hash_publish_token(token), expected)
        self.assertEqual(len(tokens.hash_publish_token(token)), 64)

    def test_verify_accepts_correct_and_rejects_wrong_token(self):
        token = tokens.generate_publish_token()
        token_hash = tokens.hash_publish_token(token)
        self.assertTrue(tokens.verify_publish_token(token, token_hash))
        self.assertFalse(tokens.verify_publish_token("other", token_hash))
        self.assertFalse(tokens.verify_publish_token(token, "deadbeef"))

    def test_verify_rejects_missing_inputs(self):
        token = tokens.generate_publish_token()
        self.assertFalse(tokens.verify_publish_token(token, None))
        self.assertFalse(tokens.verify_publish_token("", tokens.hash_publish_token(token)))
        self.assertFalse(tokens.verify_publish_token(None, tokens.hash_publish_token(token)))

    def test_token_hash_does_not_leak_plaintext(self):
        token = tokens.generate_publish_token()
        self.assertNotIn(token, tokens.hash_publish_token(token))


class TurnCredentialTests(unittest.TestCase):
    def test_turn_user_id_is_stable_per_ip_and_differs_across_ips(self):
        self.assertEqual(tokens.turn_user_id("10.0.0.1"), tokens.turn_user_id("10.0.0.1"))
        self.assertNotEqual(tokens.turn_user_id("10.0.0.1"), tokens.turn_user_id("10.0.0.2"))
        self.assertEqual(len(tokens.turn_user_id("10.0.0.1")), 16)

    def test_build_turn_credential_matches_coturn_rest_scheme(self):
        secret = "static-auth-secret"
        now = 1_700_000_000.0
        username, credential, expiry_ts = tokens.build_turn_credential(
            secret, "userid", 3600, now=now
        )

        # username 形如 "{expiry_ts}:{user_id}"
        self.assertEqual(expiry_ts, 1_700_003_600)
        self.assertEqual(username, "1700003600:userid")

        # credential 必须是 base64(HMAC-SHA1(secret, username))
        expected = base64.b64encode(
            hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
        ).decode("ascii")
        self.assertEqual(credential, expected)

    def test_turn_ttl_is_clamped_to_at_least_one_second(self):
        _, _, expiry_ts = tokens.build_turn_credential("s", "u", 0, now=1000.0)
        self.assertEqual(expiry_ts, 1001)


class BuildIceServersTests(unittest.TestCase):
    def test_without_turn_config_only_stun_is_returned(self):
        result = tokens.build_ice_servers(
            stun_urls=["stun:stun.example.com:3478"],
            turn_urls=[],
            turn_secret="",
            user_id="uid",
            turn_ttl=7200,
        )
        self.assertEqual(len(result["ice_servers"]), 1)
        self.assertEqual(result["ice_servers"][0]["urls"], ["stun:stun.example.com:3478"])
        self.assertFalse(result["turn_enabled"])
        self.assertIsNone(result["turn_expires_at"])
        self.assertEqual(result["expires_in"], 0)
        self.assertNotIn("username", result["ice_servers"][0])

    def test_turn_urls_without_secret_are_not_advertised_as_available(self):
        """只填了 TURN 地址但漏配 secret 时，不能伪装成可用。"""
        result = tokens.build_ice_servers(
            stun_urls=["stun:s"],
            turn_urls=["turn:turn.example.com:3478"],
            turn_secret="",
            user_id="uid",
            turn_ttl=7200,
        )
        self.assertFalse(result["turn_enabled"])
        self.assertEqual(len(result["ice_servers"]), 1)

    def test_turn_enabled_returns_credential_and_never_the_secret(self):
        secret = "super-secret-value"
        result = tokens.build_ice_servers(
            stun_urls=["stun:s"],
            turn_urls=["turn:turn.example.com:3478"],
            turn_secret=secret,
            user_id="uid",
            turn_ttl=7200,
        )
        self.assertTrue(result["turn_enabled"])
        self.assertEqual(result["expires_in"], 7200)
        self.assertIsInstance(result["turn_expires_at"], int)

        turn_entry = result["ice_servers"][1]
        self.assertEqual(turn_entry["urls"], ["turn:turn.example.com:3478"])
        self.assertTrue(turn_entry["username"].endswith(":uid"))
        self.assertTrue(turn_entry["credential"])

        # 静态密钥绝不能出现在下发载荷里
        self.assertNotIn(secret, json.dumps(result))

    def test_empty_stun_list_yields_no_stun_entry(self):
        result = tokens.build_ice_servers(
            stun_urls=[], turn_urls=[], turn_secret="", user_id="uid", turn_ttl=7200
        )
        self.assertEqual(result["ice_servers"], [])


# --------------------------------------------------------------------------- #
# config.py
# --------------------------------------------------------------------------- #


class ConfigNormalizationTests(unittest.TestCase):
    def test_as_bool_treats_string_zero_as_false(self):
        """管理面板回写的是字符串，"0" 不能被当成真值。"""
        self.assertFalse(p2p_config._as_bool("0"))
        self.assertFalse(p2p_config._as_bool("false"))
        self.assertFalse(p2p_config._as_bool(0))
        self.assertFalse(p2p_config._as_bool(None))
        self.assertTrue(p2p_config._as_bool("1"))
        self.assertTrue(p2p_config._as_bool("true"))
        self.assertTrue(p2p_config._as_bool("on"))
        self.assertTrue(p2p_config._as_bool("yes"))
        self.assertTrue(p2p_config._as_bool(1))

    def test_as_int_falls_back_on_garbage(self):
        self.assertEqual(p2p_config._as_int("12", 5), 12)
        self.assertEqual(p2p_config._as_int("abc", 5), 5)
        self.assertEqual(p2p_config._as_int(None, 5), 5)

    def test_as_url_list_accepts_both_string_and_sequence(self):
        self.assertEqual(
            p2p_config._as_url_list("stun:a, turn:b ,, "), ["stun:a", "turn:b"]
        )
        self.assertEqual(p2p_config._as_url_list(["stun:a", "", "turn:b"]), ["stun:a", "turn:b"])
        self.assertEqual(p2p_config._as_url_list(None), [])


class PublicP2PConfigTests(unittest.TestCase):
    def test_public_config_never_contains_turn_secret(self):
        original = getattr(settings, "p2pTurnSecret", "")
        settings.p2pTurnSecret = "do-not-leak-me"
        try:
            payload = p2p_config.build_public_p2p_config()
        finally:
            settings.p2pTurnSecret = original

        self.assertNotIn("p2pTurnSecret", payload)
        self.assertNotIn("do-not-leak-me", json.dumps(payload))
        self.assertFalse(any("secret" in key.lower() for key in payload))

    def test_public_config_exposes_expected_keys(self):
        payload = p2p_config.build_public_p2p_config()
        for key in (
            "enableP2P",
            "p2pDefaultChecked",
            "p2pMaxSize",
            "p2pRelayEnabled",
            "p2pMaxPeers",
            "p2pHeartbeatTimeout",
        ):
            self.assertIn(key, payload)

    def test_default_config_ships_p2p_switches_on(self):
        self.assertEqual(DEFAULT_CONFIG["enableP2P"], 1)
        self.assertEqual(DEFAULT_CONFIG["p2pDefaultChecked"], 1)
        self.assertEqual(DEFAULT_CONFIG["p2pTurnSecret"], "")
        self.assertEqual(DEFAULT_CONFIG["p2pTurnUrls"], [])

    def test_default_checked_is_true_by_default(self):
        original = getattr(settings, "p2pDefaultChecked", 1)
        try:
            settings.p2pDefaultChecked = 1
            self.assertTrue(p2p_config.p2p_default_checked())
            settings.p2pDefaultChecked = 0
            self.assertFalse(p2p_config.p2p_default_checked())
        finally:
            settings.p2pDefaultChecked = original


class RoomManagerConfigTests(unittest.TestCase):
    def test_apply_room_manager_config_pushes_values_to_singleton(self):
        snapshot = (
            room_manager.heartbeat_timeout,
            room_manager.room_ttl,
            room_manager.max_peers,
        )
        originals = {
            "p2pHeartbeatTimeout": getattr(settings, "p2pHeartbeatTimeout", 30),
            "p2pRoomTtl": getattr(settings, "p2pRoomTtl", 900),
            "p2pMaxPeers": getattr(settings, "p2pMaxPeers", 3),
        }
        try:
            settings.p2pHeartbeatTimeout = 45
            settings.p2pRoomTtl = 600
            settings.p2pMaxPeers = 5
            p2p_config.apply_room_manager_config()

            self.assertEqual(room_manager.heartbeat_timeout, 45)
            self.assertEqual(room_manager.room_ttl, 600)
            self.assertEqual(room_manager.max_peers, 5)
        finally:
            for key, value in originals.items():
                setattr(settings, key, value)
            p2p_config.apply_room_manager_config()
            self.assertEqual(
                (
                    room_manager.heartbeat_timeout,
                    room_manager.room_ttl,
                    room_manager.max_peers,
                ),
                snapshot,
            )


# --------------------------------------------------------------------------- #
# rooms.py — 房间生命周期
# --------------------------------------------------------------------------- #


class RoomManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = P2PRoomManager(heartbeat_timeout=30, room_ttl=900, max_peers=3)

    def test_register_publisher_then_downloader(self):
        async def scenario():
            room, pub, replaced = await self.manager.register_connection(
                "abc", ROLE_PUBLISHER, _FakeWebSocket("pub"), "1.1.1.1"
            )
            self.assertIsNone(replaced)
            self.assertEqual(room.publisher.peer_id, pub.peer_id)

            _, dl, _ = await self.manager.register_connection(
                "abc", ROLE_DOWNLOADER, _FakeWebSocket("dl"), "2.2.2.2"
            )
            self.assertEqual(len(room.downloaders), 1)
            self.assertIn(dl.peer_id, room.downloaders)
            self.assertEqual(room.peer_total, 2)

        asyncio.run(scenario())

    def test_code_is_normalized_before_lookup(self):
        async def scenario():
            await self.manager.register_connection(
                "  spaced  ", ROLE_PUBLISHER, _FakeWebSocket("pub"), "1.1.1.1"
            )
            self.assertIsNotNone(self.manager.get("spaced"))
            self.assertIsNotNone(self.manager.get("  spaced  "))

        asyncio.run(scenario())

    def test_downloader_beyond_max_peers_is_rejected(self):
        async def scenario():
            await self.manager.register_connection(
                "abc", ROLE_PUBLISHER, _FakeWebSocket("pub"), "1.1.1.1"
            )
            for index in range(3):
                await self.manager.register_connection(
                    "abc", ROLE_DOWNLOADER, _FakeWebSocket(f"dl{index}"), f"10.0.0.{index}"
                )
            with self.assertRaises(RoomFull):
                await self.manager.register_connection(
                    "abc", ROLE_DOWNLOADER, _FakeWebSocket("overflow"), "10.0.0.99"
                )

        asyncio.run(scenario())

    def test_single_ip_connection_cap_is_enforced(self):
        async def scenario():
            for index in range(MAX_CONNECTIONS_PER_IP):
                await self.manager.register_connection(
                    f"code{index}", ROLE_PUBLISHER, _FakeWebSocket(f"p{index}"), "9.9.9.9"
                )
            with self.assertRaises(TooManyConnections):
                await self.manager.register_connection(
                    "code-overflow", ROLE_PUBLISHER, _FakeWebSocket("x"), "9.9.9.9"
                )

        asyncio.run(scenario())

    def test_publisher_reconnect_replaces_previous_connection(self):
        async def scenario():
            old_ws = _FakeWebSocket("old")
            _, old_peer, _ = await self.manager.register_connection(
                "abc", ROLE_PUBLISHER, old_ws, "1.1.1.1"
            )
            new_ws = _FakeWebSocket("new")
            room, new_peer, replaced = await self.manager.register_connection(
                "abc", ROLE_PUBLISHER, new_ws, "1.1.1.1"
            )

            self.assertIsNotNone(replaced)
            self.assertEqual(replaced.peer_id, old_peer.peer_id)
            self.assertEqual(room.publisher.peer_id, new_peer.peer_id)
            # 同一 IP 只应计一条连接（旧连接额度已释放）
            self.assertEqual(self.manager._connections_by_ip.get("1.1.1.1"), 1)

        asyncio.run(scenario())

    def test_unregister_releases_ip_slot(self):
        async def scenario():
            room, peer, _ = await self.manager.register_connection(
                "abc", ROLE_PUBLISHER, _FakeWebSocket("pub"), "1.1.1.1"
            )
            await self.manager.unregister_connection("abc", peer.peer_id)

            self.assertIsNone(room.publisher)
            self.assertNotIn("1.1.1.1", self.manager._connections_by_ip)

        asyncio.run(scenario())

    def test_drop_room_releases_all_ip_slots(self):
        async def scenario():
            await self.manager.register_connection(
                "abc", ROLE_PUBLISHER, _FakeWebSocket("pub"), "1.1.1.1"
            )
            await self.manager.register_connection(
                "abc", ROLE_DOWNLOADER, _FakeWebSocket("dl"), "2.2.2.2"
            )
            dropped = await self.manager.drop_room("abc")

            self.assertIsNotNone(dropped)
            self.assertIsNone(self.manager.get("abc"))
            self.assertEqual(self.manager._connections_by_ip, {})

        asyncio.run(scenario())

    def test_heartbeat_marks_publisher_online_and_timeout_marks_offline(self):
        async def scenario():
            room, peer, _ = await self.manager.register_connection(
                "abc", ROLE_PUBLISHER, _FakeWebSocket("pub"), "1.1.1.1"
            )
            self.assertTrue(self.manager.is_publisher_online(room))

            # 心跳超时后应判离线
            room.publisher_last_seen = time.time() - self.manager.heartbeat_timeout - 1
            self.assertFalse(self.manager.is_publisher_online(room))

            await self.manager.heartbeat("abc", peer.peer_id)
            self.assertTrue(self.manager.is_publisher_online(room))

        asyncio.run(scenario())

    def test_heartbeat_from_downloader_does_not_mark_publisher_online(self):
        async def scenario():
            room, pub, _ = await self.manager.register_connection(
                "abc", ROLE_PUBLISHER, _FakeWebSocket("pub"), "1.1.1.1"
            )
            _, dl, _ = await self.manager.register_connection(
                "abc", ROLE_DOWNLOADER, _FakeWebSocket("dl"), "2.2.2.2"
            )
            room.publisher_last_seen = time.time() - 999

            await self.manager.heartbeat("abc", dl.peer_id)
            self.assertFalse(self.manager.is_publisher_online(room))

        asyncio.run(scenario())

    def test_record_transfer_accumulates_and_filters_transport(self):
        async def scenario():
            await self.manager.register_connection(
                "abc", ROLE_PUBLISHER, _FakeWebSocket("pub"), "1.1.1.1"
            )
            room = await self.manager.record_transfer("abc", byte_count=1024, transport="direct")
            room = await self.manager.record_transfer("abc", byte_count=2048, transport="relay")

            self.assertEqual(room.served_count, 2)
            self.assertEqual(room.bytes_sent, 3072)
            self.assertEqual(room.last_transport, "relay")

            # 未知 transport 不覆盖已有值，负数字节被夹到 0
            room = await self.manager.record_transfer("abc", byte_count=-5, transport="bogus")
            self.assertEqual(room.last_transport, "relay")
            self.assertEqual(room.bytes_sent, 3072)

        asyncio.run(scenario())

    def test_record_transfer_on_missing_room_returns_none(self):
        async def scenario():
            self.assertIsNone(await self.manager.record_transfer("nope", 10, "direct"))

        asyncio.run(scenario())

    def test_reap_idle_rooms_only_removes_empty_and_stale(self):
        async def scenario():
            # 空且超时 → 回收
            empty_room = P2PRoom(code="empty")
            self.manager._rooms["empty"] = empty_room
            empty_room.created_at = time.time() - self.manager.room_ttl - 10

            # 空但刚建 → 保留
            self.manager._rooms["fresh"] = P2PRoom(code="fresh")

            # 有连接但超时 → 保留（不能踢掉正在服务的房间）
            busy_room = P2PRoom(code="busy")
            busy_room.publisher = PeerConnection(
                peer_id="x", role=ROLE_PUBLISHER, websocket=_FakeWebSocket("busy")
            )
            busy_room.created_at = time.time() - self.manager.room_ttl - 10
            self.manager._rooms["busy"] = busy_room

            reaped = await self.manager.reap_idle_rooms()

            self.assertEqual(reaped, ["empty"])
            self.assertIsNone(self.manager.get("empty"))
            self.assertIsNotNone(self.manager.get("fresh"))
            self.assertIsNotNone(self.manager.get("busy"))

        asyncio.run(scenario())

    def test_status_shape_when_room_is_missing(self):
        snapshot = self.manager.status("ghost")
        self.assertEqual(
            set(snapshot),
            {
                "online",
                "downloaders",
                "served_count",
                "bytes_sent",
                "last_seen",
                "last_transport",
                "max_peers",
            },
        )
        self.assertFalse(snapshot["online"])
        self.assertEqual(snapshot["downloaders"], 0)

    def test_status_reports_online_downloaders_and_max_peers(self):
        async def scenario():
            await self.manager.register_connection(
                "abc", ROLE_PUBLISHER, _FakeWebSocket("pub"), "1.1.1.1"
            )
            await self.manager.register_connection(
                "abc", ROLE_DOWNLOADER, _FakeWebSocket("dl"), "2.2.2.2"
            )
            snapshot = self.manager.status("abc")

            self.assertTrue(snapshot["online"])
            self.assertEqual(snapshot["downloaders"], 1)
            self.assertEqual(snapshot["max_peers"], 3)
            self.assertIsNotNone(snapshot["last_seen"])

        asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# rooms.py — 信令转发决策
# --------------------------------------------------------------------------- #


class SignalingRouterTests(unittest.TestCase):
    def setUp(self):
        self.manager = P2PRoomManager(heartbeat_timeout=30, room_ttl=900, max_peers=3)
        self.router = SignalingRouter(self.manager)

    def _build_room(self):
        """构造 1 发布者 + 1 下载者 的房间，返回 (room, publisher, downloader)。"""
        publisher = PeerConnection(
            peer_id="pub000000000", role=ROLE_PUBLISHER, websocket=_FakeWebSocket("pub")
        )
        downloader = PeerConnection(
            peer_id="dl0000000000", role=ROLE_DOWNLOADER, websocket=_FakeWebSocket("dl")
        )
        room = P2PRoom(code="abc", publisher=publisher)
        room.publisher_last_seen = time.time()
        room.downloaders[downloader.peer_id] = downloader
        self.manager._rooms["abc"] = room
        return room, publisher, downloader

    def _handle(self, room, peer, payload):
        return asyncio.run(self.router.handle(room, peer, payload))

    def test_ping_returns_pong_to_sender(self):
        room, pub, _ = self._build_room()
        deliveries = self._handle(room, pub, {"t": "ping"})

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].target, pub.peer_id)
        self.assertEqual(deliveries[0].message["t"], "pong")

    def test_hello_returns_room_snapshot(self):
        room, _, dl = self._build_room()
        deliveries = self._handle(room, dl, {"t": "hello"})

        self.assertEqual(len(deliveries), 1)
        snapshot = deliveries[0].message
        self.assertEqual(snapshot["t"], "room")
        self.assertEqual(snapshot["code"], "abc")
        self.assertEqual(snapshot["role"], ROLE_DOWNLOADER)
        self.assertEqual(snapshot["peer"], dl.peer_id)
        self.assertTrue(snapshot["publisher_online"])
        self.assertEqual(snapshot["max_peers"], 3)

    def test_publisher_offer_is_forwarded_to_targeted_downloader(self):
        room, pub, dl = self._build_room()
        deliveries = self._handle(
            room,
            pub,
            {
                "t": "offer",
                "peer": dl.peer_id,
                "sdp": {"type": "offer", "sdp": "v=0"},
                "token": "should-be-stripped",
            },
        )

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].target, dl.peer_id)
        message = deliveries[0].message
        self.assertEqual(message["t"], "offer")
        self.assertEqual(message["from"], pub.peer_id)
        self.assertEqual(message["sdp"], {"type": "offer", "sdp": "v=0"})
        # 转发时不得把发布令牌带出去
        self.assertNotIn("token", message)

    def test_publisher_forward_without_peer_field_is_rejected(self):
        room, pub, _ = self._build_room()
        deliveries = self._handle(room, pub, {"t": "offer", "sdp": {}})

        self.assertEqual(deliveries[0].message["t"], "error")
        self.assertEqual(deliveries[0].target, pub.peer_id)

    def test_publisher_forward_to_unknown_peer_is_rejected(self):
        room, pub, _ = self._build_room()
        deliveries = self._handle(room, pub, {"t": "offer", "peer": "ghost", "sdp": {}})

        self.assertEqual(deliveries[0].message["t"], "error")
        self.assertIn("ghost", deliveries[0].message["message"])

    def test_downloader_answer_goes_to_publisher_without_peer_field(self):
        room, pub, dl = self._build_room()
        deliveries = self._handle(
            room, dl, {"t": "answer", "sdp": {"type": "answer", "sdp": "v=0"}}
        )

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].target, TARGET_PUBLISHER)
        self.assertEqual(deliveries[0].message["from"], dl.peer_id)

    def test_downloader_forward_is_rejected_when_publisher_offline(self):
        room, _, dl = self._build_room()
        room.publisher = None
        deliveries = self._handle(room, dl, {"t": "ice", "candidate": {}})

        self.assertEqual(deliveries[0].message["t"], "error")
        self.assertIn("发布者", deliveries[0].message["message"])

    def test_ice_is_forwarded_both_directions(self):
        room, pub, dl = self._build_room()

        pub_to_dl = self._handle(
            room, pub, {"t": "ice", "peer": dl.peer_id, "candidate": {"candidate": "c1"}}
        )
        self.assertEqual(pub_to_dl[0].target, dl.peer_id)
        self.assertEqual(pub_to_dl[0].message["t"], "ice")

        dl_to_pub = self._handle(room, dl, {"t": "ice", "candidate": {"candidate": "c2"}})
        self.assertEqual(dl_to_pub[0].target, TARGET_PUBLISHER)
        self.assertEqual(dl_to_pub[0].message["from"], dl.peer_id)

    def test_done_from_publisher_updates_stats_and_notifies_both_sides(self):
        room, pub, dl = self._build_room()
        deliveries = self._handle(
            room,
            pub,
            {"t": "done", "peer": dl.peer_id, "bytes": 1048576, "mode": "direct"},
        )

        self.assertEqual(len(deliveries), 2)
        ack = next(item for item in deliveries if item.message["t"] == "done-ack")
        peer_done = next(item for item in deliveries if item.message["t"] == "peer-done")

        self.assertEqual(ack.target, pub.peer_id)
        self.assertEqual(ack.message["served_count"], 1)
        self.assertEqual(ack.message["bytes_sent"], 1048576)

        self.assertEqual(peer_done.target, TARGET_DOWNLOADERS)
        self.assertEqual(peer_done.message["bytes"], 1048576)
        self.assertEqual(peer_done.message["mode"], "direct")

        self.assertEqual(room.served_count, 1)
        self.assertEqual(room.bytes_sent, 1048576)
        self.assertEqual(room.last_transport, "direct")

    def test_done_from_downloader_is_rejected(self):
        room, _, dl = self._build_room()
        deliveries = self._handle(room, dl, {"t": "done", "bytes": 10})

        self.assertEqual(deliveries[0].message["t"], "error")
        self.assertIn("发布者", deliveries[0].message["message"])
        self.assertEqual(room.served_count, 0)

    def test_done_with_garbage_bytes_is_coerced_to_zero(self):
        room, pub, dl = self._build_room()
        deliveries = self._handle(room, pub, {"t": "done", "peer": dl.peer_id, "bytes": "NaN"})

        ack = next(item for item in deliveries if item.message["t"] == "done-ack")
        self.assertEqual(ack.message["bytes_sent"], 0)

    def test_unknown_type_returns_error(self):
        room, pub, _ = self._build_room()
        deliveries = self._handle(room, pub, {"t": "nonsense"})

        self.assertEqual(deliveries[0].message["t"], "error")

    def test_non_dict_payload_returns_error(self):
        room, pub, _ = self._build_room()
        deliveries = self._handle(room, pub, ["not", "a", "dict"])

        self.assertEqual(deliveries[0].message["t"], "error")

    def test_missing_type_field_returns_error(self):
        room, pub, _ = self._build_room()
        deliveries = self._handle(room, pub, {"peer": "x"})

        self.assertEqual(deliveries[0].message["t"], "error")

    def test_type_alias_and_case_are_accepted(self):
        room, pub, _ = self._build_room()
        # 兼容 type 字段与大小写
        deliveries = self._handle(room, pub, {"type": "PING"})

        self.assertEqual(deliveries[0].message["t"], "pong")

    def test_relay_control_messages_are_forwardable(self):
        """P3 才会用到，但路由表应提前放行，避免前端先接协议后改路由。"""
        room, pub, dl = self._build_room()
        for message_type in ("mode", "relay-start", "relay-ready", "pause", "resume"):
            deliveries = self._handle(
                room, pub, {"t": message_type, "peer": dl.peer_id, "value": 1}
            )
            self.assertEqual(deliveries[0].target, dl.peer_id)
            self.assertEqual(deliveries[0].message["t"], message_type)

    def test_peer_join_and_left_messages_have_expected_shape(self):
        room, _, dl = self._build_room()
        joined = self.router.peer_joined_message(dl)
        self.assertEqual(joined["t"], "peer-join")
        self.assertEqual(joined["peer"], dl.peer_id)

        left = self.router.peer_left_message(dl.peer_id)
        self.assertEqual(left, {"t": "peer-left", "peer": dl.peer_id})


# --------------------------------------------------------------------------- #
# views.py — 过期方式约束与时间比较
# --------------------------------------------------------------------------- #


class PublishGuardTests(unittest.TestCase):
    def test_p2p_rejects_count_based_expiration(self):
        """P2P 数据一旦交给下载者就收不回，因此不支持按次数过期。"""
        self.assertNotIn("count", P2P_ALLOWED_EXPIRE_STYLES)
        self.assertIn("day", P2P_ALLOWED_EXPIRE_STYLES)
        self.assertIn("forever", P2P_ALLOWED_EXPIRE_STYLES)

    def test_seconds_since_handles_naive_and_aware_datetimes(self):
        now = datetime.datetime(2026, 9, 14, 12, 0, 0)
        aware = datetime.datetime(2026, 9, 14, 11, 59, 0, tzinfo=datetime.timezone.utc)

        self.assertEqual(_seconds_since(aware, now), 60.0)
        self.assertEqual(_seconds_since(now, now), 0.0)
        self.assertEqual(_seconds_since(None, now), None)

    def test_seconds_since_never_returns_negative(self):
        now = datetime.datetime(2026, 9, 14, 12, 0, 0)
        future = datetime.datetime(2026, 9, 14, 13, 0, 0)
        self.assertEqual(_seconds_since(future, now), 0.0)


# --------------------------------------------------------------------------- #
# migrations_007.py — 增量迁移可重复执行
# --------------------------------------------------------------------------- #


class P2PMigrationTests(unittest.TestCase):
    NEW_COLUMNS = {
        "is_p2p",
        "p2p_token_hash",
        "p2p_status",
        "p2p_last_seen",
        "p2p_served_count",
        "p2p_bytes_sent",
        "p2p_last_transport",
    }

    def _column_names(self, rows):
        return {str(row.get("name") or "") for row in rows}

    def test_migration_adds_columns_once_and_is_idempotent(self):
        async def scenario():
            await Tortoise.init(
                config={
                    "connections": {
                        "default": {
                            "engine": "tortoise.backends.sqlite",
                            "credentials": {"file_path": ":memory:"},
                        }
                    },
                    "apps": {
                        "models": {
                            "models": ["apps.base.models"],
                            "default_connection": "default",
                        }
                    },
                    "use_tz": False,
                    "timezone": "Asia/Shanghai",
                }
            )
            from tortoise import connections

            conn = connections.get("default")
            try:
                # 模拟老库：filecodes 表还没有任何 P2P 列
                await conn.execute_script("DROP TABLE IF EXISTS filecodes;")
                await conn.execute_script(
                    "CREATE TABLE filecodes (id INTEGER PRIMARY KEY AUTOINCREMENT, code VARCHAR(32));"
                )

                from apps.base.migrations import migrations_007

                await migrations_007.migrate()
                first = self._column_names(
                    await conn.execute_query_dict("PRAGMA table_info(filecodes)")
                )
                self.assertTrue(self.NEW_COLUMNS.issubset(first))

                # 重复执行不应报错，也不应重复加列
                await migrations_007.migrate()
                second = self._column_names(
                    await conn.execute_query_dict("PRAGMA table_info(filecodes)")
                )
                self.assertEqual(first, second)

                # 索引应存在
                indexes = await conn.execute_query_dict(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='idx_filecodes_is_p2p'"
                )
                self.assertEqual(len(indexes), 1)
            finally:
                await Tortoise.close_connections()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()