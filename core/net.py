"""Client IP resolution behind reverse proxies and CIDR allowlists (PRD 7.3)."""
from __future__ import annotations

import ipaddress
from functools import lru_cache


def client_ip(request) -> str | None:
    """
    The right-most *untrusted* ``X-Forwarded-For`` hop.

    Each of the ``TRUSTED_PROXY_COUNT`` proxies in front of the app appends the address it received the
    connection from, so with N trusted proxies the client is the N-th entry from the right. Anything to
    the left of it was supplied by the client and cannot be trusted. With a count of 0 (local dev) or a
    header shorter than the count, ``REMOTE_ADDR`` is used.
    """
    if request is None:
        return None
    from django.conf import settings

    hops = int(getattr(settings, "TRUSTED_PROXY_COUNT", getattr(settings, "TRUSTED_PROXY_HOPS", 0)) or 0)
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if hops > 0 and xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if len(parts) >= hops:
            candidate = parts[-hops]
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                pass
    return request.META.get("REMOTE_ADDR")


@lru_cache(maxsize=16)
def _networks(raw: tuple[str, ...]):
    nets = []
    for item in raw:
        nets.append(ipaddress.ip_network(item.strip(), strict=False))
    return tuple(nets)


def ip_allowed(ip: str | None, allowlist) -> bool:
    if not allowlist:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return any(addr in net for net in _networks(tuple(allowlist)) if addr.version == net.version)
