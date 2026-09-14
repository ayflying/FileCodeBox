from ipaddress import ip_address, ip_network
from typing import Dict, Iterable, Union
from datetime import datetime, timedelta
from fastapi import HTTPException, Request

from core.settings import settings


def _iter_trusted_proxies() -> Iterable[str]:
    trusted_proxies = getattr(settings, "trustedProxies", [])
    if isinstance(trusted_proxies, str):
        trusted_proxies = [item.strip() for item in trusted_proxies.split(",")]
    return [item for item in trusted_proxies if item]


def _is_trusted_proxy(host: str) -> bool:
    try:
        remote_addr = ip_address(host)
    except ValueError:
        return False

    for proxy in _iter_trusted_proxies():
        try:
            if remote_addr in ip_network(proxy, strict=False):
                return True
        except ValueError:
            continue
    return False


def _get_forwarded_for_ip(header_value: str, fallback_ip: str) -> str:
    forwarded_chain = [
        item.strip() for item in header_value.split(",") if item.strip()
    ]
    if not forwarded_chain:
        return fallback_ip

    for candidate in reversed(forwarded_chain):
        try:
            ip_address(candidate)
        except ValueError:
            return fallback_ip
        if not _is_trusted_proxy(candidate):
            return candidate
    return forwarded_chain[0]


def resolve_client_ip(
    client_host: str,
    forwarded_for: Union[str, None] = None,
    real_ip: Union[str, None] = None,
) -> str:
    """按可信代理白名单解析真实客户端 IP。

    HTTP 与 WebSocket 共用：只有直连来源是可信代理时，才采信转发头。
    """
    host = client_host or "unknown"
    if not _is_trusted_proxy(host):
        return host

    if forwarded_for:
        return _get_forwarded_for_ip(forwarded_for, host)

    if real_ip:
        try:
            ip_address(real_ip)
        except ValueError:
            return host
        return real_ip

    return host


def get_client_ip(request: Request) -> str:
    client_host = request.client.host if request.client else "unknown"
    return resolve_client_ip(
        client_host,
        request.headers.get("X-Forwarded-For"),
        request.headers.get("X-Real-IP"),
    )


class IPRateLimit:
    def __init__(self, count: int, minutes: int):
        self.ips: Dict[str, Dict[str, Union[int, datetime]]] = {}
        self.count = count
        self.minutes = minutes

    def check_ip(self, ip: str) -> bool:
        if ip in self.ips:
            ip_info = self.ips[ip]
            if ip_info["count"] >= self.count:
                if ip_info["time"] + timedelta(minutes=self.minutes) > datetime.now():
                    return False
                self.ips.pop(ip)
        return True

    def add_ip(self, ip: str) -> int:
        ip_info = self.ips.get(ip, {"count": 0, "time": datetime.now()})
        ip_info["count"] += 1
        ip_info["time"] = datetime.now()
        self.ips[ip] = ip_info
        return ip_info["count"]

    async def remove_expired_ip(self) -> None:
        now = datetime.now()
        expiration = timedelta(minutes=self.minutes)
        self.ips = {
            ip: info
            for ip, info in self.ips.items()
            if info["time"] + expiration >= now
        }

    def __call__(self, request: Request) -> str:
        ip = get_client_ip(request)
        if not self.check_ip(ip):
            raise HTTPException(status_code=423, detail="请求次数过多，请稍后再试")
        return ip
