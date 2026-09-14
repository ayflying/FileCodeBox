"""P2P 房间状态到 FileCodes 的持久化。

房间运行态只在内存，但以下三件事必须落库，否则管理端与 `/p2p/status`
（可能落在别的请求上下文中）看不到真实状态：
- 发布端在线状态（`p2p_status` / `p2p_last_seen`）
- 累计传输统计（`p2p_served_count` / `p2p_bytes_sent` / `p2p_last_transport`）
- 主动下线（终态 expired）

写库做了节流，避免每个心跳都打一次 SQLite。
"""

from __future__ import annotations

import time
from typing import Optional

from apps.base.models import FileCodes
from apps.base.p2p.rooms import P2PRoom
from core.logger import logger
from core.utils import get_now

# 心跳落库节流间隔（秒）
LAST_SEEN_SYNC_INTERVAL = 15

STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"
STATUS_EXPIRED = "expired"


async def fetch_p2p_record(code: str) -> Optional[FileCodes]:
    normalized = str(code or "").strip()
    if not normalized:
        return None
    return await FileCodes.filter(code=normalized, is_p2p=True).first()


async def set_publisher_online(code: str, online: bool) -> None:
    normalized = str(code or "").strip()
    if not normalized:
        return
    try:
        await FileCodes.filter(code=normalized, is_p2p=True).update(
            p2p_status=STATUS_ONLINE if online else STATUS_OFFLINE,
            p2p_last_seen=await get_now(),
        )
    except Exception as exc:  # pragma: no cover - 落库失败不应影响信令
        logger.error(f"[P2P] 更新发布端在线状态失败 code={normalized}: {exc}")


async def sync_last_seen(room: P2PRoom) -> None:
    """节流刷新 p2p_last_seen，供管理端展示「最后活跃时间」。"""
    now = time.time()
    if now - room.last_db_sync < LAST_SEEN_SYNC_INTERVAL:
        return
    room.last_db_sync = now
    await set_publisher_online(room.code, True)


async def persist_transfer_stats(room: P2PRoom) -> None:
    """传输完成后落一次累计统计。"""
    try:
        await FileCodes.filter(code=room.code, is_p2p=True).update(
            p2p_served_count=room.served_count,
            p2p_bytes_sent=room.bytes_sent,
            p2p_last_transport=room.last_transport,
            p2p_last_seen=await get_now(),
            p2p_status=STATUS_ONLINE,
        )
        room.last_db_sync = time.time()
    except Exception as exc:  # pragma: no cover
        logger.error(f"[P2P] 写入传输统计失败 code={room.code}: {exc}")


async def expire_p2p_record(code: str) -> bool:
    """发布者主动下线：置为终态，后续任何人取件都会看到已失效。"""
    normalized = str(code or "").strip()
    if not normalized:
        return False
    updated = await FileCodes.filter(code=normalized, is_p2p=True).update(
        p2p_status=STATUS_EXPIRED,
        expired_at=await get_now(),
        p2p_last_seen=await get_now(),
    )
    return bool(updated)


async def attach_room_to_record(code: str, room: P2PRoom) -> None:
    """把库中已有的累计统计并入内存房间，避免重连后计数被清零。"""
    record = await fetch_p2p_record(code)
    if record is None:
        return
    room.served_count = max(room.served_count, int(record.p2p_served_count or 0))
    room.bytes_sent = max(room.bytes_sent, int(record.p2p_bytes_sent or 0))
    if record.p2p_last_transport:
        room.last_transport = str(record.p2p_last_transport)
