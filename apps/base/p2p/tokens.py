"""P2P 令牌与 TURN 临时凭据（详见 docs/p2p-design.md §7.1 / §7.2）。

两条硬性约束：
1. `publish_token` 原文只在发布者前端持有，库中只存哈希（与现有密码哈希策略一致）。
2. TURN 静态密钥**绝不**下发前端，一律按 coturn REST 模式签发短时凭据。
"""

import base64
import hashlib
import hmac
import secrets
import time
from typing import Optional, Sequence

PUBLISH_TOKEN_BYTES = 32
# coturn REST 模式下 username 形如 "{expiry_ts}:{user_id}"
TURN_USERNAME_SEPARATOR = ":"


def generate_publish_token() -> str:
    """生成发布端令牌原文，仅在发布响应中返回一次。"""
    return secrets.token_urlsafe(PUBLISH_TOKEN_BYTES)


def hash_publish_token(token: str) -> str:
    """令牌入库前的单向哈希。"""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def verify_publish_token(token: str, token_hash: Optional[str]) -> bool:
    """常数时间比较，避免时序侧信道。"""
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_publish_token(token), str(token_hash))


def turn_user_id(client_ip: str) -> str:
    """按客户端 IP 派生稳定的 TURN 用户名片段，便于 coturn 侧按用户计量。"""
    digest = hashlib.sha256(f"p2p-turn:{client_ip or 'unknown'}".encode("utf-8"))
    return digest.hexdigest()[:16]


def build_turn_credential(
    secret: str, user_id: str, ttl: int, now: Optional[float] = None
) -> tuple[str, str, int]:
    """按 coturn REST 模式签发临时凭据，返回 (username, credential, expiry_ts)。"""
    ttl_seconds = max(1, int(ttl))
    expiry_ts = int(now if now is not None else time.time()) + ttl_seconds
    username = f"{expiry_ts}{TURN_USERNAME_SEPARATOR}{user_id}"
    digest = hmac.new(
        str(secret).encode("utf-8"), username.encode("utf-8"), hashlib.sha1
    ).digest()
    credential = base64.b64encode(digest).decode("ascii")
    return username, credential, expiry_ts


def build_ice_servers(
    stun_urls: Sequence[str],
    turn_urls: Sequence[str],
    turn_secret: str,
    user_id: str,
    turn_ttl: int,
) -> dict:
    """组装下发给前端的 ICE servers。

    未配置 TURN（或缺少 static-auth-secret）时只返回 STUN，
    且**不返回**任何 TURN 条目，避免把不可用配置伪装成可用。
    """
    ice_servers: list[dict] = []
    stun_list = [str(url) for url in (stun_urls or []) if str(url).strip()]
    if stun_list:
        ice_servers.append({"urls": stun_list})

    turn_list = [str(url) for url in (turn_urls or []) if str(url).strip()]
    secret = str(turn_secret or "").strip()
    turn_enabled = bool(turn_list) and bool(secret)
    if turn_enabled:
        username, credential, expiry_ts = build_turn_credential(
            secret, user_id, turn_ttl
        )
        ice_servers.append(
            {
                "urls": turn_list,
                "username": username,
                "credential": credential,
            }
        )
    else:
        expiry_ts = None

    return {
        "ice_servers": ice_servers,
        "turn_enabled": turn_enabled,
        "turn_expires_at": expiry_ts,
        "expires_in": max(1, int(turn_ttl)) if turn_enabled else 0,
    }
