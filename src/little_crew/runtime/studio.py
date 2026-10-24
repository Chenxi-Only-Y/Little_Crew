"""**工作室**：给导演台加"能在图上改、只重渲一镜"的本地服务。

## 为什么必须有服务

`file://` 打开的页面**不能发 POST** —— 浏览器的同源策略不允许。
所以"只读图"可以是一个静态文件，"能改"必须有一个本地 HTTP 服务。

这不是设计选择，是浏览器约束。

## 它做的事很少，因为底下的活早就有了

| 步 | 谁做 |
|---|---|
| 读这一镜的布局 | `previs/<shotId>/layout.json`（没有就从 `shot.json` 反推） |
| 改几个数 | 用户 |
| **校验** | `agent/previs_layout.validate_layout`（现成的） |
| **重渲这一镜** | `runtime/previs.render_layout`（现成的） |
| 其它 39 镜 | **不动**（缓存天然如此） |

**新写的只有"接收请求 + 调这两步 + 回结果"。**

## 一条刻意的宽松

**只拦 `blocker`，`major` 只提示不拦。**

理由：校验器里有 `no-reason` 这种 `major`（"没写空间理由"）。
它是**门禁**该管的事，不是**编辑**该管的事 ——
在这里因为"旧文件没存 reason"而拒绝重渲，只会让人绕开这个工具。

> **门禁负责"能不能交付"，工作室负责"能不能改"。两件事。**
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

__all__ = ["StudioServer", "load_layout", "patch_and_render"]


#: 回环地址（只有这些 host 才算「不对外」）
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def is_loopback_host(host: str) -> bool:
    """这个 host 是不是「只在本机」。"""
    return str(host or "").strip().lower() in _LOOPBACK


def safe_child(root: Path, name: str) -> Path | None:
    """把**用户给的路径片段**变成 `root` 之内的真实路径；越界返回 None。

    ## 为什么必须有（2026-10-23 审计 H-8）

    静态文件路由 `_static()` 有越界检查，而 `/api/shot/<sid>` 的两条路由
    （GET / POST）**直接把用户输入拼进文件系统路径**：

        sid = path[len("/api/shot/"):]          # 未校验
        d = srv.workdir / "previs" / sid        # 直接拼

    而 `path` 是 `unquote()` 过的，所以 `%2e%2e%2f` 会变成 `../` ——
    **workdir 之外的任意目录**都能被读（GET 返回它的 layout.json/shot.json），
    也能被 POST 重渲覆盖。同一份代码里两个路由一个有一个没有，正是
    「接线断了而不是实现错了」的典型。

    判据与 `_static` 一致（`resolve()` 后必须 `relative_to`），
    外加一条更早的拦截：镜头目录名里**不该**出现任何分隔符。
    """
    if not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name or "\x00" in name:
        return None
    try:
        base = Path(root).resolve()
        target = (base / name).resolve()
    except OSError:
        return None
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target

#: 允许通过工时改的字段（其余一律忽略 —— 别让请求体能改任意键）
EDITABLE = ("shot_size", "azimuth_deg", "angle_deg", "duration_sec",
            "camera_movement", "lens_mm")


def load_layout(shot_dir: Path) -> dict[str, Any]:
    """取这一镜的布局。**优先 `layout.json`**，没有就从 `shot.json` 反推。

    `shot.json` 是渲染用的 spec（有 outDir/resolution/form），
    反推时补回布局需要的 `reason: ""` —— 它会触发校验器的 `no-reason`（major），
    而工作室**不因 major 拒渲**（见模块 docstring）。
    """
    sid = shot_dir.name
    lay = shot_dir / "layout.json"
    if lay.is_file():
        try:
            d = json.loads(lay.read_text(encoding="utf-8"))
            d.setdefault("shotId", sid)
            return d
        except Exception:                                     # noqa: BLE001
            pass
    spec_p = shot_dir / "shot.json"
    if not spec_p.is_file():
        raise FileNotFoundError(f"{shot_dir} 里既没有 layout.json 也没有 shot.json")
    spec = json.loads(spec_p.read_text(encoding="utf-8"))
    shot = dict(spec.get("shot") or {})
    return {
        "shotId": sid,
        "shot": shot,
        "subjects": list(spec.get("subjects") or ()),
        "set": list(spec.get("set") or ()),
        "reason": "",
    }


def patch_and_render(layout: Mapping[str, Any], patch: Mapping[str, Any], *,
                     blender: str, out_root: Path,
                     resolution: tuple[int, int] | None = None,
                     fps: int | None = None,
                     render: bool = True) -> dict[str, Any]:
    """改几个数 → 校验 → （可选）只重渲这一镜。返回结构化结果。

    **校验只拦 blocker** —— 理由见模块 docstring。
    """
    from little_crew.agent.previs_layout import Verdict, validate_layout
    from little_crew.runtime.previs import layout_to_spec, render_layout

    lay = json.loads(json.dumps(dict(layout)))       # 深拷贝，不改调用方的
    shot = lay.setdefault("shot", {})
    applied: dict[str, Any] = {}
    for k in EDITABLE:
        if k not in patch:
            continue
        v = patch[k]
        if k == "shot_size":
            shot[k] = str(v)
        elif k == "camera_movement":
            shot[k] = str(v)
        else:
            try:
                shot[k] = float(v)
            except (TypeError, ValueError):
                return {"ok": False, "reason": f"{k} 不是数字：{v!r}", "applied": {}}
        applied[k] = shot[k]

    chk = validate_layout(lay)
    blockers = [i.as_dict() for i in chk.issues if i.severity == "blocker"]
    majors = [i.as_dict() for i in chk.issues if i.severity == "major"]
    # 旧运行没存 reason —— 那会让判定恒为 RETRY（`no-reason` 是 major）。
    # 明说出来，别让人以为"改了机位就变 RETRY"。
    reason_missing = not str(lay.get("reason") or "").strip()
    result: dict[str, Any] = {
        "ok": True, "applied": applied,
        "reasonMissing": reason_missing,
        "verdict": chk.verdict.value,
        "blockers": blockers, "majors": majors,
        "rendered": False,
    }
    if blockers:
        result["ok"] = False
        result["reason"] = "有 blocker，拒绝重渲"
        return result
    if not render:
        return result

    sid = str(lay.get("shotId") or "")
    out_dir = Path(out_root) / "previs" / sid

    # ---- 分辨率/帧率：**服务端自己从原 spec 取，不靠前端记** ----
    #
    # 2026-10-20 实测踩到：客户端没带 resolution 时传了 (None, None)，
    # `layout_to_spec` 里 `int(None)` 直接崩；
    # 就算不崩，退回默认 640×384 也会**把原来 852×480 的规格悄悄改掉** ——
    # 改个机位却顺带改了分辨率，是最难查的那类副作用。
    spec_old: dict[str, Any] = {}
    if (out_dir / "shot.json").is_file():
        try:
            spec_old = json.loads((out_dir / "shot.json").read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            spec_old = {}
    res_use: tuple[int, int] | None = None
    if resolution and all(x is not None for x in tuple(resolution)[:2]):
        res_use = (int(resolution[0]), int(resolution[1]))
    elif spec_old.get("resolution"):
        r0 = spec_old["resolution"]
        res_use = (int(r0.get("width") or 0) or None, int(r0.get("height") or 0) or None)
        res_use = res_use if all(res_use) else None
    fps_use = int(fps) if fps else (int(spec_old["fps"]) if spec_old.get("fps") else None)
    # 先把布局存下来 —— 下次再改就不用从 spec 反推（反推会丢 reason）
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "layout.json").write_text(
        json.dumps(lay, ensure_ascii=False, indent=2), encoding="utf-8")

    r = render_layout(lay, blender=blender, out_root=str(Path(out_root) / "previs"),
                      resolution=res_use, fps=fps_use)
    result["rendered"] = bool(r.get("ok"))
    result["render"] = {k: r.get(k) for k in
                        ("ok", "frames", "mp4", "first", "control", "reason")}
    result["warnings"] = list(r.get("warnings") or ())
    return result


class StudioServer:
    """一个只服务 `<workdir>` 的本地服务：静态页 + 两个 API。

    ⚠️ **只绑 127.0.0.1，不对外** —— 它能改文件、能起 Blender，
    绝不能监听 `0.0.0.0`。
    """

    def __init__(self, workdir: Path, *, blender: str = "",
                 host: str = "127.0.0.1", port: int = 0) -> None:
        self.workdir = Path(workdir).resolve()
        self.blender = blender
        self.host = host
        self._port = int(port)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.last_result: dict[str, Any] = {}
        # ⚠️ **「只绑 127.0.0.1」原来是注释，不是约束**（2026-10-23 审计 L-5）。
        # 注释拦不住 `StudioServer(host="0.0.0.0")` —— 而它能改文件、能起 Blender，
        # 一旦对外暴露，同网段任何人都能改你的镜头、跑任意 Blender 脚本。
        # 现在照 `api/__main__.py` 的做法：非回环一律**出声警告**。
        if not is_loopback_host(host):
            print(f"⚠️ Studio 绑定到 {host} 而不是回环地址 —— "
                  f"本服务**没有鉴权**，同网段任何人都能改你工作目录里的文件、"
                  f"并让服务起 Blender 渲任意路径。"
                  f"只在完全可信的隔离网络里这样做。", file=sys.stderr)

    # ---- 生命周期 ----
    def start(self) -> "StudioServer":
        if self._httpd is not None:
            return self
        srv = self

        class H(BaseHTTPRequestHandler):
            server_version = "LittleCrewStudio/1.0"

            def log_message(self, *a):                        # noqa: ANN002
                pass

            # ---- 路由 ----
            def do_GET(self):                                 # noqa: N802
                path = unquote(urlparse(self.path).path)
                if path in ("/", "/index.html"):
                    return self._file(srv._ensure_console(), "text/html")
                if path.startswith("/api/shot/"):
                    sid = path[len("/api/shot/"):]
                    d = safe_child(srv.workdir / "previs", sid)
                    if d is None:
                        # 越界（含 `..`、分隔符、绝对路径）—— 明确 403，不泄露存在性
                        return self._json({"ok": False, "reason": "越界路径"}, 403)
                    if not d.is_dir():
                        return self._json({"ok": False, "reason": "镜头不存在"}, 404)
                    try:
                        lay = load_layout(d)
                    except Exception as e:                    # noqa: BLE001
                        return self._json({"ok": False, "reason": str(e)}, 500)
                    spec = {}
                    if (d / "shot.json").is_file():
                        try:
                            spec = json.loads((d / "shot.json").read_text(encoding="utf-8"))
                        except Exception:                     # noqa: BLE001
                            spec = {}
                    return self._json({"ok": True, "layout": lay, "spec": spec})
                return self._static(path)

            def do_POST(self):                                # noqa: N802
                path = unquote(urlparse(self.path).path)
                if not path.startswith("/api/shot/"):
                    return self._json({"ok": False, "reason": "未知路径"}, 404)
                sid = path[len("/api/shot/"):]
                d = safe_child(srv.workdir / "previs", sid)
                if d is None:
                    return self._json({"ok": False, "reason": "越界路径"}, 403)
                if not d.is_dir():
                    return self._json({"ok": False, "reason": "镜头不存在"}, 404)
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception as e:                        # noqa: BLE001
                    return self._json({"ok": False, "reason": f"请求体不是 JSON：{e}"}, 400)
                if not srv.blender:
                    return self._json({"ok": False,
                                       "reason": "服务未配置 Blender，只能预览不能重渲"}, 400)
                try:
                    lay = load_layout(d)
                    res = patch_and_render(
                        lay, body, blender=srv.blender,
                        out_root=srv.workdir,
                        resolution=(tuple(body["resolution"])
                                    if body.get("resolution") else None),
                        fps=int(body["fps"]) if body.get("fps") else None)
                except Exception as e:                        # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    return self._json({"ok": False,
                                       "reason": f"{type(e).__name__}: {e}"}, 500)
                srv.last_result = res
                return self._json(res)

            # ---- 响应助手 ----
            def _file(self, p: Path, ctype: str):
                try:
                    raw = p.read_bytes()
                except Exception:                             # noqa: BLE001
                    return self._json({"ok": False, "reason": "文件不存在"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _json(self, obj: Mapping[str, Any], code: int = 200):
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _static(self, path: str):
                """只服务 workdir 里的文件 —— **路径穿越一律拒绝**。"""
                rel = path.lstrip("/")
                target = (srv.workdir / rel).resolve()
                try:
                    target.relative_to(srv.workdir)
                except ValueError:
                    return self._json({"ok": False, "reason": "越界路径"}, 403)
                if not target.is_file():
                    return self._json({"ok": False, "reason": "不存在"}, 404)
                ext = target.suffix.lower()
                ctype = {".png": "image/png", ".jpg": "image/jpeg",
                         ".jpeg": "image/jpeg", ".mp4": "video/mp4",
                         ".html": "text/html", ".json": "application/json",
                         ".webp": "image/webp"}.get(ext, "application/octet-stream")
                raw = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = ThreadingHTTPServer((self.host, self._port), H)
        self._port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("服务未启动")
        return f"http://{self.host}:{self._port}/"

    def __enter__(self) -> "StudioServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- 页面 ----
    def _ensure_console(self) -> Path:
        from little_crew.runtime.console import build_console

        return build_console(self.workdir, editable=True)
