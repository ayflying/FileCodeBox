"""P2P WebSocket 信令端点（详见 docs/p2p-design.md §6.3）。

职责边界：本模块只做「连接生命周期 + 消息搬运」，
信令语义全在 `rooms.SignalingRouter`（可脱离 WebSocket 单测）。

鉴权（P1）：
- 站点级 `enableP2P` 必须开启；
- `role=publisher` 必须持有效 `publish_token`；
- `role=downloader` 必须持有效且未过期的 code；
- 失败按 IP 计入 `ip_limit["p2p"]`，防房间枚举探测。

失败时先 `accept()` 再回 `error` 帧并关闭，这样前端能拿到具体原因，
而不是只看到一个没有上下文的握手失败。
"""

import json
from typing import Any, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from apps.base.dependencies import resolve_client_ip
from apps.base.models import FileCodes
from apps.base.p2p import p2p_api, store
from apps.base.p2p.config import apply_room_manager_config, p2p_enabled, p2p_relay_enabled
from apps.base.p2p.rooms import (
    ROLE_DOWNLOADER,
    ROLE_PUBLISHER,
    TARGET_DOWNLOADERS,
    TARGET_PUBLISHER,
    Delivery,
    P2PError,
    PeerConnection,
    P2PRoom,
    RoomFull,
    TooManyConnections,
    room_manager,
    signaling_router,
)
from apps.base.p2p.tokens import verify_publish_token
from apps.base.utils import ip_limit
from core.logger import logger

# 应用级关闭码（RFC 6455 允许 4000-4999 自定义）
CLOSE_BAD_ROLE = 4400
CLOSE_TOKEN_INVALID = 4401
CLOSE_P2P_DISABLED = 4503
CLOSE_ROOM_NOT_FOUND = 4404
CLOSE_ROOM_FULL = 4409
CLOSE_RATE_LIMITED = 4429
CLOSE_ROOM_EXPIRED = 4410
CLOSE_REPLACED = 4412
CLOSE_NORMAL = 1000


def get_ws_client_ip(websocket: WebSocket) -> str:
    client = websocket.client
    return resolve_client_ip(
        client.host if client else "unknown",
        websocket.headers.get("x-forwarded-for"),
        websocket.headers.get("x-real-ip"),
    )


def _failure(close_code: int, error_code: str, message: str) -> dict:
    return {"close_code": close_code, "code": error_code, "message": message}


async def _send(websocket: WebSocket, message: dict) -> bool:
    """安全发送 JSON 文本帧，连接已断时静默跳过。"""
    if websocket is None:
        return False
    if websocket.client_state is not WebSocketState.CONNECTED:
        return False
    try:
        await websocket.send_text(json.dumps(message, ensure_ascii=False))
        return True
    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.debug(f"[P2P] 发送信令失败：{exc}")
        return False


async def _safe_close(websocket: WebSocket, code: int, reason: str = "") -> None:
    if websocket is None:
        return
    if websocket.client_state is WebSocketState.DISCONNECTED:
        return
    try:
        await websocket.close(code=code, reason=reason)
    except (WebSocketDisconnect, RuntimeError):
        pass


async def _reject(
    websocket: WebSocket, close_code: int, error_code: str, message: str
) -> None:
    """先握手、再回错误帧、随即关闭，让前端能拿到原因。"""
    try:
        await websocket.accept()
    except RuntimeError:
        return
    await _send(
        websocket, {"t": "error", "code": error_code, "message": message}
    )
    await _safe_close(websocket, close_code, message)


async def _authorize(
    code: str, role: str, token: str
) -> tuple[Optional[FileCodes], Optional[dict]]:
    if not p2p_enabled():
        return None, _failure(CLOSE_P2P_DISABLED, "p2p_disabled", "站点未启用 P2P 直传")

    normalized = str(code or "").strip()
    if not normalized:
        return None, _failure(CLOSE_ROOM_NOT_FOUND, "room_not_found", "取件码不能为空")

    record = await FileCodes.filter(code=normalized).first()
    if record is None or not record.is_p2p:
        return None, _failure(CLOSE_ROOM_NOT_FOUND, "room_not_found", "文件不存在")
    if await record.is_expired():
        return None, _failure(CLOSE_ROOM_EXPIRED, "room_expired", "分享已失效")

    if role == ROLE_PUBLISHER:
        if not verify_publish_token(token, record.p2p_token_hash):
            return None, _failure(CLOSE_TOKEN_INVALID, "token_invalid", "发布令牌无效")

    return record, None


async def _dispatch(
    room: P2PRoom, peer: PeerConnection, deliveries: list
) -> None:
    for delivery in deliveries:
        if delivery.target == TARGET_DOWNLOADERS:
            targets = list(room.downloaders.values())
        elif delivery.target == TARGET_PUBLISHER:
            targets = [room.publisher] if room.publisher is not None else []
        elif delivery.target == peer.peer_id:
            targets = [peer]
        else:
            targets = []
            target = room.downloaders.get(delivery.target)
            if target is None and room.publisher is not None:
                if room.publisher.peer_id == delivery.target:
                    target = room.publisher
            if target is not None:
                targets.append(target)

        for item in targets:
            await _send(item.websocket, delivery.message)


async def _handle_text(room: P2PRoom, peer: PeerConnection, raw: str) -> None:
    try:
        payload: Any = json.loads(raw)
    except (TypeError, ValueError):
        await _send(
            peer.websocket,
            {"t": "error", "code": "bad_json", "message": "信令必须是合法 JSON"},
        )
        return

    deliveries = await signaling_router.handle(room, peer, payload)
    await _dispatch(room, peer, deliveries)

    # 传输完成后落一次累计统计
    if isinstance(payload, dict) and str(payload.get("t") or "").lower() == "done":
        await store.persist_transfer_stats(room)

    # 心跳节流写库，供管理端展示「最后活跃时间」
    if peer.is_publisher:
        await store.sync_last_seen(room)


async def _handle_binary(room: P2PRoom, peer: PeerConnection, data: bytes) -> None:
    """二进制帧是中继数据面，P1 阶段尚未实现（见 docs 阶段 P3）。"""
    if not peer.binary_warned:
        peer.binary_warned = True
        await _send(
            peer.websocket,
            {
                "t": "error",
                "code": "relay_unavailable",
                "message": "流式中转数据面尚未启用",
            },
        )
    logger.debug(
        f"[P2P] 收到二进制帧但中继未实现 code={room.code} peer={peer.peer_id} "
        f"bytes={len(data)} relay_enabled={p2p_relay_enabled()}"
    )


@p2p_api.websocket("/signal/{code}")
async def p2p_signal(
    websocket: WebSocket,
    code: str,
    role: str = ROLE_DOWNLOADER,
    token: str = "",
):
    client_ip = get_ws_client_ip(websocket)
    normalized_role = str(role or "").strip().lower()

    if normalized_role not in (ROLE_PUBLISHER, ROLE_DOWNLOADER):
        await _reject(websocket, CLOSE_BAD_ROLE, "role_invalid", "role 参数不合法")
        return

    limiter = ip_limit["p2p"]
    if not limiter.check_ip(client_ip):
        await _reject(
            websocket, CLOSE_RATE_LIMITED, "rate_limited", "请求次数过多，请稍后再试"
        )
        return

    apply_room_manager_config()

    record, failure = await _authorize(code, normalized_role, token)
    if failure is not None:
        limiter.add_ip(client_ip)
        await _reject(
            websocket, failure["close_code"], failure["code"], failure["message"]
        )
        return

    normalized_code = record.code
    await websocket.accept()

    try:
        room, peer, replaced = await room_manager.register_connection(
            normalized_code, normalized_role, websocket, client_ip
        )
    except RoomFull as exc:
        await _send(websocket, {"t": "error", "code": exc.code, "message": exc.message})
        await _safe_close(websocket, CLOSE_ROOM_FULL, exc.message)
        return
    except TooManyConnections as exc:
        await _send(websocket, {"t": "error", "code": exc.code, "message": exc.message})
        await _safe_close(websocket, CLOSE_RATE_LIMITED, exc.message)
        return
    except P2PError as exc:
        await _send(websocket, {"t": "error", "code": exc.code, "message": exc.message})
        await _safe_close(websocket, CLOSE_BAD_ROLE, exc.message)
        return

    if replaced is not None:
        await _send(
            replaced.websocket,
            {
                "t": "error",
                "code": "replaced",
                "message": "发布端已在新的连接恢复服务，本连接关闭",
            },
        )
        await _safe_close(replaced.websocket, CLOSE_REPLACED, "发布端重连")

    try:
        if peer.is_publisher:
            await store.attach_room_to_record(normalized_code, room)
            await store.set_publisher_online(normalized_code, True)
            # 发布者（重）上线时通知已在场的下载者
            await _dispatch(
                room,
                peer,
                [Delivery(TARGET_DOWNLOADERS, {"t": "publisher-online"})],
            )
        else:
            if room.publisher is not None:
                await _send(
                    room.publisher.websocket,
                    signaling_router.peer_joined_message(peer),
                )

        await _send(websocket, signaling_router.room_snapshot(room, peer))

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if text is not None:
                await _handle_text(room, peer, text)
                continue
            data = message.get("bytes")
            if data is not None:
                await _handle_binary(room, peer, data)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover - 兜底，避免单连接异常影响全局
        logger.error(f"[P2P] 信令连接异常 code={normalized_code}: {exc}")
    finally:
        _, removed = await room_manager.unregister_connection(
            normalized_code, peer.peer_id
        )
        if removed is not None:
            if removed.is_publisher:
                await store.set_publisher_online(normalized_code, False)
                await _dispatch(
                    room,
                    peer,
                    [Delivery(TARGET_DOWNLOADERS, {"t": "publisher-offline"})],
                )
            elif room.publisher is not None:
                await _send(
                    room.publisher.websocket,
                    signaling_router.peer_left_message(removed.peer_id),
                )
        await _safe_close(websocket, CLOSE_NORMAL)
