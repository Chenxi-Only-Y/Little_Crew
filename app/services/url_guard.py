"""URL 安全校验：用于防御 SSRF（base_url / 参考图 URL 指向内网）与敏感外发。

- assert_public_url: 仅允许 http(s)，并拒绝解析出任何私有/回环/链路本地/保留地址的主机。
- 允许 localhost/127.0.0.1（自托管代理/本地 ComfyUI）；**调用方若不需要访问本机，
  应额外拒绝它们**（见 `/api/settings/test`）。
- 例外：`198.18.0.0/15` 视为可放行（见下方说明）。
"""

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

#: `198.18.0.0/15` —— IANA 保留的"网络基准测试"网段。
#:
#: 为什么必须单独放行：Clash / Surge / sing-box 等代理的 **fake-ip 模式**会把
#: **所有域名**解析到这个段里的假地址。实测本机：
#:     api.deepseek.com       → 198.18.0.16
#:     dashscope.aliyuncs.com → 198.18.0.74
#:     example.com            → 198.18.0.62
#: 而 `ipaddress` 把它判为 `is_private`，于是本函数会拒绝**每一个**域名——
#: 表现为"挂了代理的机器上 DeepSeek / 百炼 / MiniMax 全部连不上"，
#: 报错还写着"指向内网/保留地址"，完全指不到真正原因。
#:
#: 放行它的代价可接受：该网段**不可路由**（IANA 保留用于基准测试），
#: 真实内网目标（10/172.16/192.168/127/169.254）仍照常拒绝。
#: 即这条例外不会把"真能连上的内网地址"放进来，只是不再误伤代理。
_PROXY_FAKE_IP_NETS = (ipaddress.ip_network("198.18.0.0/15"),)


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
    # 直接给 IP 时不必解析（也让 `[::1]` 这类字面量走同一条判定）
    try:
        literal = ipaddress.ip_address(host)
        infos: list = [(None, None, None, None, (str(literal), 0))]
    except ValueError:
        try:
            infos = list(socket.getaddrinfo(host, None))
        except OSError:
            raise ValueError(f"{context} 无法解析主机：{host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in net for net in _PROXY_FAKE_IP_NETS):
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise ValueError(f"{context} 指向内网/保留地址，已拒绝：{host}")
    return url
