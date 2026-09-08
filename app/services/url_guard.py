"""URL 安全校验：用于防御 SSRF（base_url / 参考图 URL 指向内网）与敏感外发。

- assert_public_url: 仅允许 http(s)，并拒绝解析出任何私有/回环/链路本地/保留地址的主机。
  允许 localhost/127.0.0.1（自托管代理/本地 ComfyUI）。
"""

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def assert_public_url(url: str, *, context: str = "URL") -> str:
    """校验 url 为公网 http(s)（或 localhost），否则抛 ValueError。返回原 url。"""
    s = urlparse(url)
    if s.scheme not in ("http", "https"):
        raise ValueError(f"{context} 仅支持 http/https：{url[:60]}")
    host = (s.hostname or "").lower()
    if not host:
        raise ValueError(f"{context} 缺少主机名：{url[:60]}")
    if host in _ALLOWED_LOCAL_HOSTS:
        return url
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ValueError(f"{context} 无法解析主机：{host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise ValueError(f"{context} 指向内网/保留地址，已拒绝：{host}")
    return url
