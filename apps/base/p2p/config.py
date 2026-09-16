"""P2P 配置的集中读取层。

站点配置可经管理面板改写，取值类型不保证（可能是字符串、可能是数字），
因此所有读取都做一次归一化，避免把 `"0"` 当成真值、或把 `"2"` 当字节数用。
"""

from typing import Any, List

from apps.base.p2p.rooms import room_manager
from core.settings import settings

DEFAULT_P2P_MAX_SIZE = 2 * 1024**3
DEFAULT_HEARTBEAT_TIMEOUT = 30
DEFAULT_ROOM_TTL = 900
DEFAULT_MAX_PEERS = 3
DEFAULT_TURN_TTL = 7200
DEFAULT_STUN_PORT = 3478


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    return bool(value)


def _as_url_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _host_only(value: Any) -> str:
    """从 Host 头取纯主机名。

    Host 可能是 `1.2.3.4:8080`、`example.com`、或 IPv6 字面量 `[::1]:8080`，
    这里统一剥离端口与方括号，避免拼出 `stun:host:8080:3478` 这类非法地址。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end > 0 else raw
    if raw.count(":") == 1:
        return raw.split(":", 1)[0]
    return raw


def p2p_enabled() -> bool:
    return _as_bool(getattr(settings, "enableP2P", 1), True)


def p2p_default_checked() -> bool:
    return _as_bool(getattr(settings, "p2pDefaultChecked", 1), True)


def p2p_max_size() -> int:
    return max(0, _as_int(getattr(settings, "p2pMaxSize", DEFAULT_P2P_MAX_SIZE), DEFAULT_P2P_MAX_SIZE))


def p2p_relay_enabled() -> bool:
    return _as_bool(getattr(settings, "p2pRelayEnabled", 1), True)


def p2p_heartbeat_timeout() -> int:
    return max(
        1,
        _as_int(
            getattr(settings, "p2pHeartbeatTimeout", DEFAULT_HEARTBEAT_TIMEOUT),
            DEFAULT_HEARTBEAT_TIMEOUT,
        ),
    )


def p2p_room_ttl() -> int:
    return max(1, _as_int(getattr(settings, "p2pRoomTtl", DEFAULT_ROOM_TTL), DEFAULT_ROOM_TTL))


def p2p_max_peers() -> int:
    return max(1, _as_int(getattr(settings, "p2pMaxPeers", DEFAULT_MAX_PEERS), DEFAULT_MAX_PEERS))


def p2p_stun_enabled() -> bool:
    """内置 STUN 开关。"""
    return _as_bool(getattr(settings, "p2pStunEnabled", 1), True)


def p2p_stun_port() -> int:
    """内置 STUN 监听端口。"""
    port = _as_int(getattr(settings, "p2pStunPort", DEFAULT_STUN_PORT), DEFAULT_STUN_PORT)
    return port if 1 <= port <= 65535 else DEFAULT_STUN_PORT


def p2p_stun_urls(request_host: Any = None) -> List[str]:
    """下发给前端的 STUN 列表。

    取值为空时**不再回退到第三方 STUN**，而是按当前访问入口派生，
    指向站点内置的自建 STUN —— 这样「站点可达即 STUN 可达」，
    换访问地址（内网 IP / 虚拟网 IP / 域名）都不需要改配置。
    显式配置了列表则以配置为准，便于将来挂外部 STUN。
    """
    configured = _as_url_list(getattr(settings, "p2pStunUrls", []))
    if configured:
        return configured
    if not p2p_stun_enabled():
        return []
    host = _host_only(request_host)
    if not host:
        return []
    return [f"stun:{host}:{p2p_stun_port()}"]


def p2p_turn_urls() -> List[str]:
    return _as_url_list(getattr(settings, "p2pTurnUrls", []))


def p2p_turn_secret() -> str:
    """coturn static-auth-secret。绝不进入任何下发给前端的响应。"""
    return str(getattr(settings, "p2pTurnSecret", "") or "").strip()


def p2p_turn_ttl() -> int:
    return max(1, _as_int(getattr(settings, "p2pTurnTtl", DEFAULT_TURN_TTL), DEFAULT_TURN_TTL))


def apply_room_manager_config() -> None:
    """把站点配置同步到进程内房间管理器。"""
    room_manager.configure(
        heartbeat_timeout=p2p_heartbeat_timeout(),
        room_ttl=p2p_room_ttl(),
        max_peers=p2p_max_peers(),
    )


def build_public_p2p_config() -> dict:
    """下发给前端的 P2P 配置，**不含 p2pTurnSecret**。"""
    return {
        "enableP2P": p2p_enabled(),
        "p2pDefaultChecked": p2p_default_checked(),
        "p2pMaxSize": p2p_max_size(),
        "p2pRelayEnabled": p2p_relay_enabled(),
        "p2pMaxPeers": p2p_max_peers(),
        "p2pHeartbeatTimeout": p2p_heartbeat_timeout(),
    }
