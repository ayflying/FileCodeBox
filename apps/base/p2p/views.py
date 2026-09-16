"""P2P REST 控制面（详见 docs/p2p-design.md §6.1 / §6.2 / §7.2）。

与既有上传链路的关键差异：`/p2p/publish` **不调用 `reserve_storage`**，
也不写 `file_path` / `uuid_file_name` —— 文件始终留在发布者浏览器里，
服务器只登记元数据（见决策 D4）。
"""

import os
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request

from apps.admin.dependencies import share_required_login
from apps.base.file_validation import validate_file_type
from apps.base.models import FileCodes
from apps.base.p2p import p2p_api, store
from apps.base.p2p.config import (
    p2p_enabled,
    p2p_heartbeat_timeout,
    p2p_max_peers,
    p2p_max_size,
    p2p_relay_enabled,
    p2p_stun_urls,
    p2p_turn_secret,
    p2p_turn_ttl,
    p2p_turn_urls,
)
from apps.base.p2p.rooms import room_manager
from apps.base.p2p.tokens import (
    build_ice_servers,
    generate_publish_token,
    hash_publish_token,
    turn_user_id,
    verify_publish_token,
)
from apps.base.schemas import P2PPublishModel, P2PUnpublishModel
from apps.base.utils import get_expire_info, ip_limit, validate_expire_style
from core.response import APIResponse
from core.utils import get_now

STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"
STATUS_EXPIRED = "expired"

# P2P 不支持按取件次数过期：数据一旦交给下载者就无法收回（见 §3.1）
P2P_ALLOWED_EXPIRE_STYLES = {"day", "hour", "minute", "forever"}


def _seconds_since(moment, now) -> Optional[float]:
    """计算距今秒数。

    Tortoise 在 use_tz=False 下回读的时间可能带/不带 tzinfo，
    这里统一按本地墙上时间比较，避免 naive/aware 混用报错。
    """
    if moment is None:
        return None
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return max(0.0, (now - moment).total_seconds())


@p2p_api.post("/publish", dependencies=[Depends(share_required_login)])
async def p2p_publish(data: P2PPublishModel, ip: str = Depends(ip_limit["upload"])):
    """登记一个 P2P 分享，立即返回取件码与发布令牌（零等待）。"""
    if not p2p_enabled():
        raise HTTPException(status_code=403, detail="站点未启用 P2P 直传")

    file_name = os.path.basename(str(data.file_name or "").strip())
    if not file_name:
        raise HTTPException(status_code=400, detail="缺少文件名")

    try:
        file_size = int(data.file_size)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="文件大小不合法")
    if file_size <= 0:
        raise HTTPException(status_code=400, detail="文件大小不合法")

    max_size = p2p_max_size()
    if max_size > 0 and file_size > max_size:
        raise HTTPException(
            status_code=403,
            detail=f"P2P 单文件上限为 {max_size / 1024 ** 3:.2f} GB",
        )

    expire_style = str(data.expire_style or "").strip()
    validate_expire_style(expire_style)
    if expire_style not in P2P_ALLOWED_EXPIRE_STYLES:
        raise HTTPException(
            status_code=400,
            detail="P2P 分享不支持按取件次数过期，请改用按时间过期",
        )

    # 服务端不持有文件内容，此处只能校验文件名
    validate_file_type(file_name)

    try:
        expire_value = max(1, int(data.expire_value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="过期时间不合法")

    expired_at, expired_count, used_count, code = await get_expire_info(
        expire_value, expire_style
    )
    publish_token = generate_publish_token()
    prefix, suffix = os.path.splitext(file_name)

    await FileCodes.create(
        code=code,
        prefix=prefix,
        suffix=suffix,
        uuid_file_name=None,
        file_path=None,
        size=file_size,
        text=None,
        expired_at=expired_at,
        expired_count=expired_count,
        used_count=used_count,
        is_p2p=True,
        p2p_token_hash=hash_publish_token(publish_token),
        p2p_status=STATUS_OFFLINE,
        p2p_served_count=0,
        p2p_bytes_sent=0,
    )

    ip_limit["upload"].add_ip(ip)
    return APIResponse(
        detail={
            "code": code,
            "publish_token": publish_token,
            "name": file_name,
            "size": file_size,
            "expires_at": expired_at,
            "max_size": max_size,
            "relay_enabled": p2p_relay_enabled(),
            "heartbeat_timeout": p2p_heartbeat_timeout(),
            "max_peers": p2p_max_peers(),
        }
    )


@p2p_api.get("/status/{code}")
async def p2p_status(code: str, ip: str = Depends(ip_limit["metadata"])):
    """取件页用的状态查询：在线/离线 + 最后活跃时间 + 已服务次数。

    按决策 D7 只给「事实」，不给「预估在线时长」。
    """
    normalized = str(code or "").strip()
    record = await FileCodes.filter(code=normalized).first()
    if record is None:
        ip_limit["metadata"].add_ip(ip)
        return APIResponse(code=404, detail="文件不存在")
    if not record.is_p2p:
        ip_limit["metadata"].add_ip(ip)
        return APIResponse(code=400, detail="该取件码不是 P2P 分享")

    ip_limit["metadata"].add_ip(ip)
    now = await get_now()
    expired = await record.is_expired()
    room = room_manager.get(normalized)
    online = room_manager.is_publisher_online(room) and not expired

    served_count = int(record.p2p_served_count or 0)
    bytes_sent = int(record.p2p_bytes_sent or 0)
    transport = record.p2p_last_transport
    if room is not None:
        served_count = max(served_count, room.served_count)
        bytes_sent = max(bytes_sent, room.bytes_sent)
        transport = room.last_transport or transport

    if room is not None and room.publisher_last_seen:
        last_seen_ago = max(0.0, time.time() - room.publisher_last_seen)
    else:
        last_seen_ago = _seconds_since(record.p2p_last_seen, now)

    if expired:
        status = STATUS_EXPIRED
    elif online:
        status = STATUS_ONLINE
    else:
        status = STATUS_OFFLINE

    return APIResponse(
        detail={
            "code": record.code,
            "name": record.prefix + record.suffix,
            "size": record.size,
            "type": "file",
            "is_text": False,
            "is_p2p": True,
            "online": online,
            "expired": expired,
            "status": status,
            "expired_at": record.expired_at,
            "last_seen_ago": last_seen_ago,
            "served_count": served_count,
            "bytes_sent": bytes_sent,
            "transport": transport,
            "downloaders": len(room.downloaders) if room is not None else 0,
            "max_peers": p2p_max_peers(),
            "relay_enabled": p2p_relay_enabled(),
        }
    )


@p2p_api.post("/unpublish")
async def p2p_unpublish(data: P2PUnpublishModel):
    """发布者主动下线，进入 expired 终态并释放房间。"""
    normalized = str(data.code or "").strip()
    record = await FileCodes.filter(code=normalized, is_p2p=True).first()
    if record is None:
        return APIResponse(code=404, detail="文件不存在")
    if not verify_publish_token(data.publish_token, record.p2p_token_hash):
        raise HTTPException(status_code=403, detail="发布令牌无效")

    await room_manager.drop_room(normalized)
    await store.expire_p2p_record(normalized)
    return APIResponse(detail={"code": normalized, "status": STATUS_EXPIRED})


@p2p_api.post("/ice")
async def p2p_ice(request: Request, ip: str = Depends(ip_limit["metadata"])):
    """签发 ICE 配置。

    STUN 默认按当前访问入口派生，指向同机自建 coturn（见 config.p2p_stun_urls），
    因此不再依赖任何第三方 STUN。
    TURN 走 coturn REST 临时凭据，静态密钥永不下发前端（见 §7.2）。
    未配置 TURN 时只返回 STUN，且不伪装成可用。
    """
    if not p2p_enabled():
        raise HTTPException(status_code=403, detail="站点未启用 P2P 直传")
    ip_limit["metadata"].add_ip(ip)
    return APIResponse(
        detail=build_ice_servers(
            stun_urls=p2p_stun_urls(request_host=request.headers.get("host")),
            turn_urls=p2p_turn_urls(),
            turn_secret=p2p_turn_secret(),
            user_id=turn_user_id(ip),
            turn_ttl=p2p_turn_ttl(),
        )
    )
