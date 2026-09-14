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


def p2p_stun_urls() -> List[str]:
    return _as_url_list(getattr(settings, "p2pStunUrls", [])) or [
        "stun:stun.l.google.com:19302"
    ]


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
