"""URL 安全校验：用于防御 SSRF（base_url / 参考图 URL 指向内网）与敏感外发。

判定原则：**白名单式**——只有"全局可路由（`is_global`）"的地址才放行，
其余一律拒绝。这样新出现的特殊网段（CGNAT、保留段……）默认是**被拒**的，
不会因为"忘了加进拒绝表"而漏过去。

- 允许 localhost/127.0.0.1（自托管代理/本地 ComfyUI）；**调用方若不需要访问本机，
  应传 `allow_local=False`**（下载类默认就是 False，见 `safe_get`）。
- 例外：`198.18.0.0/15`（代理 fake-ip 段）视为可放行，见下方说明。
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
#: 不放行的话本函数会拒绝**每一个**域名——表现为"挂了代理的机器上
#: DeepSeek / 百炼 / MiniMax 全部连不上"，报错还写着"指向内网/保留地址"，
#: 完全指不到真正原因。
#:
#: 放行它的代价可接受：该网段**不可路由**（IANA 保留用于基准测试，
#: 实测连接超时）。这是**唯一**的例外，且必须是**具体网段**——
#: 早先的写法是"用一系列 `is_private`/`is_loopback`… 去拒"，那种**黑名单**
#: 思路漏掉了 `100.64.0.0/10`（CGNAT，k8s Service CIDR / 云内网常用，
#: **是可路由的**），实测 `assert_public_url("http://100.64.0.1/x")` 会放行。
#: 现在改成白名单判定，这类网段默认被拒。
_PROXY_FAKE_IP_NETS = (ipaddress.ip_network("198.18.0.0/15"),)


def _is_allowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
                   allow_local: bool) -> bool:
    """该地址是否可放行。

    白名单语义：
      1. 代理 fake-ip 段 → 放行（唯一例外，见 `_PROXY_FAKE_IP_NETS`）；
      2. `allow_local` 且是回环 → 放行（用户显式配置的本地服务）；
      3. **只有 `is_global` 且是单播才放行**，其余（私有/CGNAT/保留/
         链路本地/组播/未指定/基准测试…）一律拒绝。

    第 3 条用 `is_global` 而不是逐项 `is_*` 取反，是为了让判定**随标准演进**：
    Python 更新特殊网段表时这里自动跟着收紧，不必手工维护拒绝清单。
    但**必须另外排除组播**：`is_global` 对 `224.0.0.1` 这类组播地址为 True
    （实测），只靠它会把组播放进来。
    """
    if any(ip in net for net in _PROXY_FAKE_IP_NETS):
        return True
    if allow_local and ip.is_loopback:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return False
    return bool(ip.is_global)


def assert_public_url(url: str, *, context: str = "URL",
                      allow_local: bool = True) -> str:
    """校验 url 为公网 http(s)，否则抛 ValueError。返回原 url。

    Args:
        allow_local: 是否允许本机地址（`localhost` / `127.0.0.1` / `::1`）。
            **默认 True 是给"用户显式配置的端点"用的**（本地 ComfyUI、
            local-gen 这类自托管服务确实在本机）。
            ⚠️ 但对**由远端内容决定的 URL**（下载地址、重定向目标）必须传
            `allow_local=False`——否则"公网服务返回 302 到 127.0.0.1"
            就能读到本机任意 HTTP 服务。见 `safe_get`。
    """
    s = urlparse(url)
    if s.scheme not in ("http", "https"):
        raise ValueError(f"{context} 仅支持 http/https：{url[:60]}")
    host = (s.hostname or "").lower()
    if not host:
        raise ValueError(f"{context} 缺少主机名：{url[:60]}")
    if allow_local and host in _ALLOWED_LOCAL_HOSTS:
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
        if not _is_allowed_ip(ip, allow_local):
            # 回环单独给一句更精确的提示（这是最常见的拒因，且解决方式不同）
            if ip.is_loopback and not allow_local:
                raise ValueError(f"{context} 指向本机地址，已拒绝：{host}")
            raise ValueError(f"{context} 指向内网/保留地址，已拒绝：{host}")
    return url


#: 跟随重定向的上限（防止无穷跳转）
_MAX_REDIRECTS = 5


def safe_get(url: str, *, context: str = "下载", allow_local: bool = False, **kwargs):
    """带 **逐跳 SSRF 校验**的 GET，返回 `httpx.Response`。

    为什么不能直接 `httpx.get(url, follow_redirects=True)`：
        入口校验只检查了**第一个** URL。攻击者让自己的公网服务返回
        `302 Location: http://127.0.0.1:8080/admin`，客户端就会照跳——
        校验形同虚设。这类"重定向跳板"是 SSRF 的经典绕过。

    这里手工跟跳，**每一跳**都重新过 `assert_public_url`。

    Args:
        allow_local: **默认 False**——下载类调用必须拒绝本机地址。
            这个默认值很关键：`assert_public_url` 默认是**允许**回环的
            （给用户显式配置的本地 ComfyUI / local-gen 用），若这里沿用那个
            默认，"302 → 127.0.0.1"就会一路放行——**逐跳校验形同虚设**，
            而这恰恰是本函数 docstring 举的那个例子（实测确认能读到本机响应体）。
            只有当你明确要访问本机服务时才传 True。

    用法：把 `with httpx.Client(...) as c: c.get(url)` 换成
        `resp = safe_get(url, context="...")`
    """
    import httpx

    opts = dict(kwargs)
    # 自己管重定向，所以强制关掉 httpx 的自动跟随
    opts["follow_redirects"] = False
    timeout = opts.pop("timeout", 60)

    current = assert_public_url(url, context=context, allow_local=allow_local)
    for _ in range(_MAX_REDIRECTS):
        resp = httpx.get(current, timeout=timeout, **opts)
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        location = resp.headers.get("location", "")
        if not location:
            return resp
        # 相对 Location 要先补成绝对地址，再逐跳校验。
        # 每一跳都**沿用调用方的 allow_local**——不能在这一跳放松。
        nxt = str(httpx.URL(current).join(location))
        current = assert_public_url(
            nxt, context=f"{context}（重定向后）", allow_local=allow_local
        )
    raise ValueError(f"{context} 重定向次数超过 {_MAX_REDIRECTS}，已中止")
