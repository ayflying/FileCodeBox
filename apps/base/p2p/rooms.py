"""P2P 信令房间的进程内状态与转发决策（详见 docs/p2p-design.md §4.2 / §6.3）。

设计要点：
- 房间状态**只存进程内存**，不入库；因此信令必须落在同一进程（见决策 D12，
  `WORKERS=1`）。调大多 worker 前需先上 Redis pub/sub，否则发布者与下载者
  可能落在不同进程，房间互不可见，信令永远握不上手。
- 本模块**不直接操作 WebSocket**，只产出「投递决策」（Delivery），
  由 signaling.py 执行。这样信令逻辑可以脱离 WS 做单元测试。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from core.logger import logger

ROLE_PUBLISHER = "publisher"
ROLE_DOWNLOADER = "downloader"
VALID_ROLES = (ROLE_PUBLISHER, ROLE_DOWNLOADER)

# 二进制中继帧用 1 字节存放 peer_id 长度，故 peer_id 上限 255 字节
PEER_ID_BYTES = 12
MAX_PEER_ID_BYTES = 255

# 单 IP 并发信令连接上限，防房间枚举探测（见 §7.3）
MAX_CONNECTIONS_PER_IP = 8

TARGET_PUBLISHER = "@publisher"
TARGET_DOWNLOADERS = "@downloaders"


def new_peer_id() -> str:
    return secrets.token_hex(PEER_ID_BYTES // 2)


class P2PError(Exception):
    """P2P 信令层错误基类。"""

    code = "p2p_error"

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class P2PDisabled(P2PError):
    code = "p2p_disabled"


class RoomNotFound(P2PError):
    code = "room_not_found"


class RoomFull(P2PError):
    code = "room_full"


class TooManyConnections(P2PError):
    code = "too_many_connections"


class PeerNotAllowed(P2PError):
    code = "peer_not_allowed"


@dataclass
class PeerConnection:
    peer_id: str
    role: str
    websocket: Any
    client_ip: str = "unknown"
    joined_at: float = field(default_factory=time.time)
    # 二进制中继帧暂不可用时的告警去重标记
    binary_warned: bool = False

    @property
    def is_publisher(self) -> bool:
        return self.role == ROLE_PUBLISHER

    def summary(self) -> dict:
        return {
            "peer": self.peer_id,
            "peer_id": self.peer_id,
            "role": self.role,
            "joined_at": self.joined_at,
        }


@dataclass
class P2PRoom:
    code: str
    created_at: float = field(default_factory=time.time)
    publisher: Optional[PeerConnection] = None
    downloaders: Dict[str, PeerConnection] = field(default_factory=dict)
    publisher_last_seen: float = 0.0
    served_count: int = 0
    bytes_sent: int = 0
    last_transport: Optional[str] = None
    # 节流写库：避免每个心跳都落一次 SQLite
    last_db_sync: float = 0.0

    def downloader_summaries(self) -> List[dict]:
        return [peer.summary() for peer in self.downloaders.values()]

    @property
    def peer_total(self) -> int:
        return len(self.downloaders) + (1 if self.publisher else 0)

    def iter_peers(self) -> Iterable[PeerConnection]:
        if self.publisher is not None:
            yield self.publisher
        yield from self.downloaders.values()


class P2PRoomManager:
    """进程内房间表。所有结构变更都在同一把锁内完成。"""

    def __init__(
        self,
        heartbeat_timeout: int = 30,
        room_ttl: int = 900,
        max_peers: int = 3,
    ):
        self._rooms: Dict[str, P2PRoom] = {}
        self._connections_by_ip: Dict[str, int] = {}
        self._lock = asyncio.Lock()
        self.heartbeat_timeout = int(heartbeat_timeout)
        self.room_ttl = int(room_ttl)
        self.max_peers = int(max_peers)

    def configure(
        self,
        heartbeat_timeout: Optional[int] = None,
        room_ttl: Optional[int] = None,
        max_peers: Optional[int] = None,
    ) -> None:
        """从站点配置同步运行参数（每次 refresh_settings 后可调用）。"""
        if heartbeat_timeout is not None:
            self.heartbeat_timeout = max(1, int(heartbeat_timeout))
        if room_ttl is not None:
            self.room_ttl = max(1, int(room_ttl))
        if max_peers is not None:
            self.max_peers = max(1, int(max_peers))

    # ---- 查询 ----

    def get(self, code: str) -> Optional[P2PRoom]:
        return self._rooms.get(str(code or "").strip())

    def is_publisher_online(self, room: Optional[P2PRoom]) -> bool:
        if room is None or room.publisher is None:
            return False
        return (time.time() - room.publisher_last_seen) <= self.heartbeat_timeout

    def status(self, code: str) -> dict:
        """给 /p2p/status 用的房间快照（房间不存在时也返回结构完整的默认值）。"""
        room = self.get(code)
        if room is None:
            return {
                "online": False,
                "downloaders": 0,
                "served_count": 0,
                "bytes_sent": 0,
                "last_seen": None,
                "last_transport": None,
                "max_peers": self.max_peers,
            }
        online = self.is_publisher_online(room)
        last_seen = room.publisher_last_seen or None
        return {
            "online": online,
            "downloaders": len(room.downloaders),
            "served_count": room.served_count,
            "bytes_sent": room.bytes_sent,
            "last_seen": last_seen,
            "last_transport": room.last_transport,
            "max_peers": self.max_peers,
        }

    def active_room_count(self) -> int:
        return len(self._rooms)

    # ---- 连接生命周期 ----

    async def register_connection(
        self,
        code: str,
        role: str,
        websocket: Any,
        client_ip: str = "unknown",
    ) -> tuple[P2PRoom, PeerConnection, Optional[PeerConnection]]:
        """登记一条信令连接。

        返回 (room, peer, replaced_publisher)。`replaced_publisher` 非空表示
        发布者刷新页面后重连，旧连接已被顶替，调用方需关闭旧连接。
        """
        normalized_code = str(code or "").strip()
        if not normalized_code:
            raise RoomNotFound("取件码不能为空")
        if role not in VALID_ROLES:
            raise PeerNotAllowed("role 参数不合法")

        async with self._lock:
            if self._connections_by_ip.get(client_ip, 0) >= MAX_CONNECTIONS_PER_IP:
                raise TooManyConnections("同一 IP 的并发信令连接过多")

            room = self._rooms.get(normalized_code)
            if room is None:
                room = P2PRoom(code=normalized_code)
                self._rooms[normalized_code] = room

            replaced: Optional[PeerConnection] = None
            if role == ROLE_PUBLISHER:
                previous = room.publisher
                if previous is not None and previous.websocket is not websocket:
                    replaced = previous
                    self._release_ip(previous.client_ip)
                peer = PeerConnection(
                    peer_id=new_peer_id(),
                    role=role,
                    websocket=websocket,
                    client_ip=client_ip,
                )
                room.publisher = peer
                room.publisher_last_seen = time.time()
            else:
                if len(room.downloaders) >= self.max_peers:
                    raise RoomFull(f"并发下载者已达上限（{self.max_peers}）")
                peer = PeerConnection(
                    peer_id=new_peer_id(),
                    role=role,
                    websocket=websocket,
                    client_ip=client_ip,
                )
                room.downloaders[peer.peer_id] = peer

            self._acquire_ip(client_ip)
            logger.info(
                f"[P2P] 连接加入 code={normalized_code} role={role} "
                f"peer={peer.peer_id} ip={client_ip} "
                f"downloaders={len(room.downloaders)}"
            )
            return room, peer, replaced

    async def unregister_connection(
        self, code: str, peer_id: str
    ) -> tuple[Optional[P2PRoom], Optional[PeerConnection]]:
        """注销连接，返回 (剩余房间, 被移除的连接)。房间空置时保留占位，由回收任务清理。"""
        normalized_code = str(code or "").strip()
        async with self._lock:
            room = self._rooms.get(normalized_code)
            if room is None:
                return None, None

            removed: Optional[PeerConnection] = None
            if room.publisher is not None and room.publisher.peer_id == peer_id:
                removed = room.publisher
                room.publisher = None
                room.publisher_last_seen = 0.0
            else:
                removed = room.downloaders.pop(peer_id, None)

            if removed is None:
                return room, None

            self._release_ip(removed.client_ip)
            logger.info(
                f"[P2P] 连接离开 code={normalized_code} role={removed.role} "
                f"peer={peer_id} ip={removed.client_ip} "
                f"downloaders={len(room.downloaders)}"
            )
            return room, removed

    async def heartbeat(self, code: str, peer_id: str) -> None:
        async with self._lock:
            room = self._rooms.get(str(code or "").strip())
            if room is None:
                return
            if room.publisher is not None and room.publisher.peer_id == peer_id:
                room.publisher_last_seen = time.time()

    async def record_transfer(
        self,
        code: str,
        byte_count: int = 0,
        transport: Optional[str] = None,
    ) -> Optional[P2PRoom]:
        normalized_code = str(code or "").strip()
        async with self._lock:
            room = self._rooms.get(normalized_code)
            if room is None:
                return None
            room.served_count += 1
            room.bytes_sent += max(0, int(byte_count))
            if transport in {"direct", "relay"}:
                room.last_transport = transport
            return room

    async def drop_room(self, code: str) -> Optional[P2PRoom]:
        async with self._lock:
            room = self._rooms.pop(str(code or "").strip(), None)
            if room is None:
                return None
            for peer in room.iter_peers():
                self._release_ip(peer.client_ip)
            return room

    async def reap_idle_rooms(self) -> List[str]:
        """回收长时间无连接的空房间，避免内存无界增长。"""
        now = time.time()
        async with self._lock:
            stale = [
                code
                for code, room in self._rooms.items()
                if room.peer_total == 0 and (now - room.created_at) > self.room_ttl
            ]
            for code in stale:
                self._rooms.pop(code, None)
        if stale:
            logger.info(f"[P2P] 回收空闲房间 {len(stale)} 个")
        return stale

    async def reset(self) -> None:
        """仅供测试使用。"""
        async with self._lock:
            self._rooms.clear()
            self._connections_by_ip.clear()

    # ---- 内部 ----

    def _acquire_ip(self, client_ip: str) -> None:
        self._connections_by_ip[client_ip] = (
            self._connections_by_ip.get(client_ip, 0) + 1
        )

    def _release_ip(self, client_ip: str) -> None:
        remaining = self._connections_by_ip.get(client_ip, 0) - 1
        if remaining > 0:
            self._connections_by_ip[client_ip] = remaining
        else:
            self._connections_by_ip.pop(client_ip, None)


@dataclass
class Delivery:
    """一条投递决策：把 message 发给 target。

    target 取值：TARGET_PUBLISHER / TARGET_DOWNLOADERS / 具体 peer_id。
    """

    target: str
    message: Dict[str, Any]


class SignalingRouter:
    """把收到的信令消息翻译成投递决策。不碰 WebSocket，便于单测。"""

    FORWARD_TYPES = {
        "offer",
        "answer",
        "ice",
        "mode",
        "relay-start",
        "relay-ready",
        "pause",
        "resume",
    }

    def __init__(self, manager: P2PRoomManager):
        self.manager = manager

    async def handle(
        self, room: P2PRoom, peer: PeerConnection, payload: Any
    ) -> List[Delivery]:
        if not isinstance(payload, dict):
            return [self._error_to(peer.peer_id, "信令载荷必须是 JSON 对象")]

        message_type = str(payload.get("t") or payload.get("type") or "").strip().lower()
        if not message_type:
            return [self._error_to(peer.peer_id, "缺少信令类型字段 t")]

        if message_type == "ping":
            await self.manager.heartbeat(room.code, peer.peer_id)
            return [Delivery(peer.peer_id, {"t": "pong", "ts": time.time()})]

        if message_type == "hello":
            await self.manager.heartbeat(room.code, peer.peer_id)
            return [Delivery(peer.peer_id, self.room_snapshot(room, peer))]

        if message_type in self.FORWARD_TYPES:
            return self._forward(room, peer, payload, message_type)

        if message_type == "done":
            return await self._on_done(room, peer, payload)

        return [self._error_to(peer.peer_id, f"不支持的信令类型: {message_type}")]

    def room_snapshot(self, room: P2PRoom, peer: PeerConnection) -> dict:
        return {
            "t": "room",
            "code": room.code,
            "role": peer.role,
            "peer": peer.peer_id,
            "peers": room.downloader_summaries(),
            "downloaders": len(room.downloaders),
            "publisher_online": self.manager.is_publisher_online(room),
            "max_peers": self.manager.max_peers,
        }

    def peer_left_message(self, peer_id: str) -> dict:
        return {"t": "peer-left", "peer": peer_id}

    def peer_joined_message(self, peer: PeerConnection) -> dict:
        return {"t": "peer-join", "peer": peer.peer_id, **peer.summary()}

    # ---- 内部 ----

    def _forward(
        self,
        room: P2PRoom,
        peer: PeerConnection,
        payload: dict,
        message_type: str,
    ) -> List[Delivery]:
        forwarded = {
            key: value for key, value in payload.items() if key not in {"token"}
        }
        forwarded["t"] = message_type
        forwarded["from"] = peer.peer_id

        if peer.is_publisher:
            target_id = str(payload.get("peer") or payload.get("peer_id") or "").strip()
            if not target_id:
                return [self._error_to(peer.peer_id, "缺少 peer 字段")]
            if target_id not in room.downloaders:
                return [self._error_to(peer.peer_id, f"下载者不存在: {target_id}")]
            return [Delivery(target_id, forwarded)]

        if room.publisher is None:
            return [self._error_to(peer.peer_id, "发布者已离线")]
        # 下载者不发 peer 字段时默认发给发布者
        return [Delivery(TARGET_PUBLISHER, forwarded)]

    async def _on_done(
        self, room: P2PRoom, peer: PeerConnection, payload: dict
    ) -> List[Delivery]:
        if not peer.is_publisher:
            return [self._error_to(peer.peer_id, "只有发布者可以上报传输完成")]

        try:
            byte_count = int(payload.get("bytes") or 0)
        except (TypeError, ValueError):
            byte_count = 0

        transport = str(
            payload.get("mode") or payload.get("transport") or ""
        ).strip() or None

        updated = await self.manager.record_transfer(
            room.code, byte_count=byte_count, transport=transport
        )
        if updated is None:
            return [self._error_to(peer.peer_id, "房间已失效")]

        return [
            Delivery(
                peer.peer_id,
                {
                    "t": "done-ack",
                    "peer": str(payload.get("peer") or "").strip(),
                    "served_count": updated.served_count,
                    "bytes_sent": updated.bytes_sent,
                },
            ),
            Delivery(
                TARGET_DOWNLOADERS,
                {
                    "t": "peer-done",
                    "peer": str(payload.get("peer") or "").strip(),
                    "bytes": max(0, byte_count),
                    "mode": transport,
                },
            ),
        ]

    def _error_to(self, target: str, message: str, code: str = "p2p_error") -> Delivery:
        return Delivery(target, {"t": "error", "code": code, "message": message})


# 进程级单例：信令状态必须与 worker 进程绑定（见 D12）
room_manager = P2PRoomManager()
signaling_router = SignalingRouter(room_manager)
