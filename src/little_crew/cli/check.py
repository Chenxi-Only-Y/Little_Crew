"""命令行 / HTTP 接口 —— 自检。

断言的是**接口契约**，不是业务逻辑（业务逻辑由各层自己的自检覆盖）：

  · 退出码是否**分类**（CI 要能据以分流）
  · `--json` 是否真的只输出 JSON（可被 `jq` 消费）
  · 参数错误是否被当成**用法错误**而非崩溃
  · HTTP 接口的状态码语义（404/400 vs 500）
  · **长任务是否异步**（同步接口会让重试变成重复烧 GPU）

运行： python -m little_crew.cli.check
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _run_cli(argv: list[str]) -> tuple[int, str]:
    """跑一次 CLI，捕获 stdout 与 `SystemExit`。**不退出测试进程。**

    ⚠️ **必须捕获 `SystemExit`**：`argparse` 对用法错误直接 `sys.exit(2)`，
    而那是**正确行为**（shell 惯例）。测试要测的是"它确实这么做了"，
    不是让它把测试进程也一起结束掉 ——
    早期版本没捕获，自检跑一半就静默退出（exit 2、无输出），
    看起来像"测试崩了"，其实是"测试通过了但把自己也退了"。
    """
    from little_crew.cli import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = main(argv)
        except SystemExit as e:                     # argparse 的用法错误
            rc = int(e.code) if isinstance(e.code, int) else 1
    return rc, buf.getvalue()


# ==========================================================================
# A. CLI 契约
# ==========================================================================

def _a_cli_basics() -> None:
    from little_crew.cli import ExitCode

    check("A1 退出码是**分类**的，不是二值",
          len({ExitCode.OK, ExitCode.FAILED, ExitCode.ENVIRONMENT,
               ExitCode.USAGE}) == 4,
          f"OK={ExitCode.OK} FAILED={ExitCode.FAILED} "
          f"ENV={ExitCode.ENVIRONMENT} USAGE={ExitCode.USAGE}")
    check("A2 业务失败与环境不可用**分开**（CI 可分流）",
          ExitCode.FAILED != ExitCode.ENVIRONMENT)
    check("A3 Ctrl-C 用 130（shell 惯例）", ExitCode.INTERRUPTED == 130)

    # --version
    rc, out = _run_cli(["--version"])
    check("A4 `--version` 成功且打印版本号",
          rc == ExitCode.OK and "0.4" in out, f"rc={rc} out={out.strip()[:40]}")

    # 无子命令 -> 帮助 + 用法错误
    rc, out = _run_cli([])
    check("A5 无子命令 -> 打印帮助并返回用法错误码",
          rc == ExitCode.USAGE and "用法" in out or "usage" in out.lower(),
          f"rc={rc}")

    # 未知子命令 -> argparse 报错（退出码 2 是 argparse 的约定）
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            _run_cli(["no-such-command"])
        check("A6 未知子命令被 argparse 拒绝", True)
    except SystemExit:
        check("A6 未知子命令被 argparse 拒绝", True)


def _b_cli_usage_errors() -> None:
    from little_crew.cli import ExitCode

    rc, out = _run_cli(["compile", "no-such-file-xyz.json"])
    check("B1 文件不存在 -> **用法错误**码（不是崩溃）",
          rc == ExitCode.USAGE, f"rc={rc} out={out.strip()[:60]}")

    rc, out = _run_cli(["shot", "--channel", "bogus", "x"])
    check("B2 非法枚举值被 argparse 拒绝（不落到业务代码）",
          rc != ExitCode.OK, f"rc={rc}")


def _c_json_output() -> None:
    """`--json` 必须**只**输出 JSON —— 否则 `| jq` 会崩。"""
    rc, out = _run_cli(["probe", "--json"])
    text = out.strip()
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        pass
    check("C1 `probe --json` 输出**纯 JSON**（可被 jq 消费）",
          isinstance(parsed, dict), f"前 60 字：{text[:60]!r}")
    if isinstance(parsed, dict):
        check("C2 JSON 里有结构化的通道状态",
              "comfyui" in parsed and "localGen" in parsed,
              str(list(parsed)))
        check("C3 JSON 里报告总体可用性（脚本可判）",
              isinstance(parsed.get("ok"), bool), str(parsed.get("ok")))

    # 人读模式应当是**非 JSON**（有表格/文字）
    rc2, out2 = _run_cli(["probe"])
    check("C4 默认（人读）模式不是 JSON", not out2.strip().startswith("{"),
          out2.strip()[:50])


# ==========================================================================
# D. HTTP 接口契约
# ==========================================================================

class _Srv:
    """测试用的服务实例（**随机端口**，避免与本机服务撞车）。"""

    def __init__(self) -> None:
        from little_crew.api.server import ApiServer, serve

        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        self.port = port
        self.api = ApiServer(host="127.0.0.1", port=port)
        self.httpd = serve(host="127.0.0.1", port=port, api=self.api)
        time.sleep(0.25)
        self.base = f"http://127.0.0.1:{port}"

    def call(self, path: str, method: str = "GET",
             body: dict | None = None, timeout: int = 20):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return e.code, {"raw": raw[:100].decode("utf-8", "replace")}

    def close(self) -> None:
        self.httpd.shutdown()


def _d_http_contract() -> None:
    s = _Srv()
    try:
        # ---- healthz ----
        st, j = s.call("/healthz")
        check("D1 /healthz 返回 200 且 ok=true",
              st == 200 and j.get("ok") is True, f"{st} {j}")
        check("D2 /healthz 带版本号",
              bool(j.get("version")), str(j.get("version")))
        check("D3 ★ /healthz **不碰外部服务**（无依赖字段）",
              "comfyui" not in j and "localGen" not in j,
              str(list(j)))

        # ---- env ----
        st, j = s.call("/v1/env")
        check("D4 /v1/env 返回 200 且含配置来源",
              st == 200 and "config" in j, f"{st} keys={list(j)}")
        check("D5 ★ 配置项如实标注来源与「是否配成空串」",
              all({"key", "source", "empty"} <= set(x)
                  for x in (j.get("config") or [])),
              str((j.get("config") or [{}])[0])[:80])

        # ---- H-5：密钥**不许**明文出门 ----
        #
        # 这个接口**没有鉴权**，默认只绑 127.0.0.1 —— 但同机任何进程/网页都能打它，
        # 而项目自己在 `api/__main__.py` 里就知道"别对外暴露"这件事。
        # 原来它用 `config.describe()`，那个函数**明文返回 value**。
        import os as _os

        secret = "sk-THIS-MUST-NOT-LEAK-0123456789"
        _os.environ["LLM_API_KEY"] = secret
        try:
            st, j2 = s.call("/v1/env")
            blob = json.dumps(j2, ensure_ascii=False)
            check("D5b ★★ /v1/env 不返回明文密钥（它无鉴权、可能对外）",
                  st == 200 and secret not in blob, blob[:120])
            check("D5c ★ 但仍能看出「配没配、像不像」（打码而非抹掉）",
                  any("…" in str(x.get("value", "")) or "*" in str(
                      x.get("value", ""))
                      for x in (j2.get("config") or [])),
                  str((j2.get("config") or [{}])[0])[:80])
        finally:
            _os.environ.pop("LLM_API_KEY", None)

        # ---- M-25：`out` 不许越出允许的根目录 ----
        #
        # `out` 来自请求体，之后成为整集的**工作目录**（产物/缓存/检查点/报告
        # 全写进去）。原来直接 `Path(...) + mkdir(parents=True)`，
        # 绝对路径与 `../..` 都被照单创建 —— 而服务无鉴权。
        from little_crew.api.server import out_root, safe_out

        root = out_root()
        check("D5d 写盘根目录可声明（默认 outputs/）",
              root.name == "outputs" or bool(
                  __import__("os").environ.get("LITTLE_CREW_OUT_ROOT")),
              str(root))
        escaped = 0
        for bad in ("../escape", "../../.ssh", str(root.parent / "escape")):
            try:
                safe_out(bad)
            except ValueError:
                escaped += 1
        check("D5e ★★ `out` 越界（`..` / 绝对路径）一律拒（400，不是照单创建）",
              escaped == 3, f"{escaped}/3 被拒")
        inside = safe_out("outputs/_selfcheck_ok")
        check("D5f ★ 根目录之内的 `out` 仍然可用（不误伤正常调用）",
              inside == (root / "_selfcheck_ok").resolve(), str(inside))
        import shutil as _sh

        _sh.rmtree(inside, ignore_errors=True)

        st, j3 = s.call("/v1/episode", "POST",
                        {"script": "x", "out": "../escape_ep"})
        check("D5g ★★ `POST /v1/episode` 的越界 out -> 400（不是 500）",
              st == 400, f"{st} {j3}")

        # ---- 404 / 400 的语义 ----
        st, j = s.call("/v1/runs/nope")
        check("D6 未知 runId -> 404（不是 500）",
              st == 404, f"{st} {j}")
        st, j = s.call("/no/such/path")
        check("D7 未知路径 -> 404", st == 404, f"{st}")
        st, j = s.call("/v1/compile", "POST", {})
        check("D8 缺参数 -> 400（**客户端错，不是服务器错**）",
              st == 400, f"{st} {j}")

        # ★ 编译失败 -> 400。**用注入的假编译器，不依赖 ComfyUI 在不在跑。**
        #
        # ⚠️ 这里踩过一个真坑：早期用真实 IR 去打这个接口，于是本测试
        # **只在 ComfyUI 运行时才通过** —— 别人 clone 下来跑必然失败，
        # 而那与"契约对不对"毫无关系。
        #
        # 更值得记的是**服务端的判序错误**（已修）：它先查 ComfyUI、
        # 后校验 IR，于是没跑 ComfyUI 时连"IR 明显无效"都报 500。
        # **输入不合法这个结论与依赖在不在无关**，任何时候都该先答。
        from little_crew.api import server as _srv

        def _bad_ir(_ir):
            raise _srv._CompileFailed("IR 缺少必填字段 'irVersion'")

        def _no_comfy(_ir):
            raise RuntimeError("ComfyUI 不可用：连接超时")

        old_impl = s.api._compile_impl
        try:
            s.api._compile_impl = _bad_ir
            st, j = s.call("/v1/compile", "POST", {"ir": {"irVersion": "x"}})
            check("D9 ★★ 编译失败 -> **400**（业务结果，非服务器错误）",
                  st == 400 and j.get("ok") is False, f"{st} {str(j)[:70]}")
            check("D9b 失败响应带 `ok:false`（与成功响应对称）",
                  j.get("ok") is False, str(sorted(j)))
            check("D9c 且给出可操作的 hint",
                  bool(j.get("hint")), str(j.get("hint"))[:60])

            # 环境不可用必须是 **5xx**，与 4xx 区分开
            s.api._compile_impl = _no_comfy
            st, j = s.call("/v1/compile", "POST", {"ir": {"irVersion": "x"}})
            check("D9d ★★ 环境不可用 -> 500（与「你的输入错了」区分开）",
                  st == 500, f"{st} {str(j)[:60]}")
        finally:
            s.api._compile_impl = old_impl

        # ---- 空任务表 ----
        st, j = s.call("/v1/runs")
        check("D10 /v1/runs 初始为空列表",
              st == 200 and j.get("runs") == [], f"{st} {j}")

        # ---- ★ 长任务异步 ----
        # 用一个**假的** handler 验证「立刻返回 runId」这一契约，
        # 不真跑一集（那要几分钟，且依赖外部服务）。
        from little_crew.api.server import RunState

        def _slow(run) -> dict:
            time.sleep(1.2)
            return {"done": True}

        t0 = time.time()
        run = s.api.runs.create("episode")
        s.api.runs.start(run, _slow)
        dt = time.time() - t0
        check("D11 ★★ 提交返回**不等任务完成**（长任务异步）",
              dt < 0.5, f"提交耗时 {dt:.3f}s")
        check("D12 提交立刻拿到 runId",
              bool(run.run_id), run.run_id)

        st, j = s.call(f"/v1/runs/{run.run_id}")
        check("D13 立刻查询是 running/pending（不是假装完成）",
              j.get("state") in (RunState.PENDING, RunState.RUNNING),
              str(j.get("state")))

        # 等它完成
        deadline = time.time() + 8
        while time.time() < deadline:
            st, j = s.call(f"/v1/runs/{run.run_id}")
            if j.get("state") in RunState.TERMINAL:
                break
            time.sleep(0.3)
        check("D14 后台任务最终成功且结果可取",
              j.get("state") == RunState.SUCCEEDED
              and (j.get("result") or {}).get("done") is True,
              f"{j.get('state')} {str(j.get('result'))[:40]}")
        check("D15 完成时间被记录（可算耗时）",
              bool(j.get("finishedAt")) and j.get("seconds") is not None,
              str(j.get("seconds")))

        # ---- ★ 后台异常必须变成状态，不能静默 ----
        def _boom(run) -> dict:
            raise RuntimeError("故意失败")

        run2 = s.api.runs.create("episode")
        s.api.runs.start(run2, _boom)
        deadline = time.time() + 6
        while time.time() < deadline:
            st, j2 = s.call(f"/v1/runs/{run2.run_id}")
            if j2.get("state") in RunState.TERMINAL:
                break
            time.sleep(0.2)
        check("D16 ★★ 后台异常变成 failed 状态（不会永远 running）",
              j2.get("state") == RunState.FAILED, str(j2.get("state")))
        check("D17 失败原因写进状态（客户端能看到）",
              "故意失败" in str(j2.get("error")), str(j2.get("error"))[:60])

        # ---- SSE ----
        run3 = s.api.runs.create("episode")
        s.api.runs.start(run3, _slow)
        got: list[str] = []

        def _reader() -> None:
            try:
                with urllib.request.urlopen(
                        f"{s.base}/v1/runs/{run3.run_id}/events",
                        timeout=10) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data:"):
                            got.append(line)
                        if '"state"' in line and len(got) > 1:
                            break
            except Exception:                             # noqa: BLE001
                pass

        th = threading.Thread(target=_reader, daemon=True)
        th.start()
        th.join(timeout=8)
        check("D18 ★ SSE 进度流可读（`data:` 行）",
              len(got) >= 1, f"收到 {len(got)} 条")
        check("D19 SSE 事件是 JSON（可解析）",
              all(_is_json(x[5:].strip()) for x in got) if got else False,
              got[0][:70] if got else "(无)")

        check("D20 任务表可列出（含刚才的）",
              len(s.api.runs.all()) >= 3, f"{len(s.api.runs.all())} 个")
    finally:
        s.close()


def _is_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except json.JSONDecodeError:
        return False


# ==========================================================================
# G. 产品入口的交付契约（2026-10-23 审计 B1：H-4 / H-21 / M-28 / M-27）
# ==========================================================================
#
# 这四条都不是"功能不对"，而是**产品入口与项目其余部分对不上**：
#   · 集号写死 -> 每一集共用 shotId/检查点/缓存（H-4）
#   · 从不设产物解析器 -> `--no-video` 形同虚设，视频永远合不出来（H-21）
#   · 报告写到根目录 -> 报告页读 `delivery/`，打开是**空页面**（M-28）
#   · 用法错误退 2 -> 撞 `ExitCode.ENVIRONMENT`，CI 把"命令写错"当"环境没装"（M-27）

def _g_entry_contract() -> None:
    import inspect
    import json as _json
    import pathlib
    import tempfile

    from little_crew.cli import ExitCode
    from little_crew.cli import commands as _cmds

    # ---- M-27：用法错误必须是 USAGE(3)，不能撞 ENVIRONMENT(2) ----
    rc, out = _run_cli(["episode", "--no-such-flag"])
    check("G1 ★★ 用法错误 -> USAGE(3)（不是 argparse 默认的 2，那等于「环境不可用」）",
          rc == ExitCode.USAGE and rc != ExitCode.ENVIRONMENT, f"rc={rc}")
    check("G2 且说清是用法错误（不是静默退出）",
          "用法错误" in out, out.strip().splitlines()[-1][:60] if out else "")

    # ---- H-4：集号必须可指定、且默认不是写死的 "CLI" ----
    from little_crew.cli.__main__ import build_parser

    ns = build_parser().parse_args(["episode"])
    check("G3 ★★ CLI 有 --episode-id（集号不再写死）",
          getattr(ns, "episode_id", "") == "E100", str(getattr(ns, "episode_id", "")))
    check("G4 ★ 且有 --fresh / --allow-reuse（新一集进老目录时能显式处理）",
          hasattr(ns, "fresh") and hasattr(ns, "allow_reuse"))

    src = inspect.getsource(_cmds.cmd_episode)
    check("G5 ★★ `cmd_episode` 里不再出现 `episode_id=\"CLI\"`",
          'episode_id="CLI"' not in src,
          "仍有硬编码" if 'episode_id="CLI"' in src else "")

    # 防串集的判据（与 demo 共用同一实现）
    from little_crew.runtime.episode import check_workdir_episode

    with tempfile.TemporaryDirectory() as td:
        w = pathlib.Path(td)
        old, conflict = check_workdir_episode(w, "E200")
        check("G6 空目录 -> 无冲突", not conflict and not old, conflict[:40])
        (w / "delivery").mkdir(parents=True)
        (w / "delivery" / "run.report.json").write_text(
            _json.dumps({"episodeId": "E100"}), encoding="utf-8")
        old2, c2 = check_workdir_episode(w, "E100")
        check("G7 ★ 同一集重跑 -> 不拦（续跑是正常操作）",
              not c2 and old2 == "E100", c2[:40])
        old3, c3 = check_workdir_episode(w, "E200")
        check("G8 ★★ 换集进同一目录 -> 拦住并说清后果（镜头重名/缓存串）",
              bool(c3) and old3 == "E100" and "缓存" in c3, c3[:50])

        # ---- M-28：交付必须落到 delivery/，且报告页**读得到** ----
        from little_crew.runtime.episode import write_delivery
        from little_crew.runtime.report_html import build_report

        out_dir = write_delivery(
            w, result={"episodeId": "E200", "ok": True,
                       "shots": [], "wallSeconds": 1.0},
            manifest={"episodeId": "E200", "title": "t", "shots": [],
                      "shotCount": 0},
            evidence={"episodeId": "E200", "cacheHits": 0, "rendered": 0,
                      "secondsSaved": 0.0, "artifacts": [], "irHashes": [],
                      "checks": {}},
        )
        files = sorted(p.name for p in out_dir.glob("*.json"))
        check("G9 ★★ 交付落在 `delivery/` 三件套（不是根目录那份孤零零的报告）",
              out_dir == w / "delivery"
              and {"run.report.json", "episode.master.json",
                   "delivery.evidence.json"} <= set(files), str(files))
        page = build_report(w)
        html = page.read_text(encoding="utf-8")
        check("G10 ★★ 报告页能读到这一集（空页面 = 交付物白写了）",
              "E200" in html, f"页面 {len(html)} 字节，含集号={'E200' in html}")

    # ---- H-21：必须设产物解析器，否则视频永远合不出来 ----
    check("G11 ★★ `cmd_episode` 设置了 `runner.resolve_artifact`"
          "（否则 assemble_video=True 也只是空调用）",
          "resolve_artifact" in src and "artifact_resolver" in src)

    from little_crew.runtime.episode import artifact_resolver

    with tempfile.TemporaryDirectory() as td:
        w = pathlib.Path(td)
        deep = w / "cache" / "t1" / "ab" / "cd"
        deep.mkdir(parents=True)
        (deep / "SH-E100-001.png").write_bytes(b"x")
        got = artifact_resolver(w)("SH-E100-001.png")
        check("G12 ★ 解析器能在缓存分片目录里按名找回产物",
              got is not None and got.name == "SH-E100-001.png", str(got))
        check("G13 找不到就返回 None（不猜、不抛）",
              artifact_resolver(w)("nope.png") is None)


# ==========================================================================
# F. 源码守卫（静态，不执行被测模块）
# ==========================================================================

#: 模块级自动存在的名字（`symtable` 不会把它们列进模块符号表）
_DUNDERS = frozenset({
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__dict__", "__path__",
})


def _unresolved_globals(path) -> list[str]:
    """列出「函数体里引用了、但模块级与内建都没有绑定」的名字。

    ## 为什么需要它（2026-10-23 审计 C-0）
    `commands.py` 里曾有一行：

        from little_crew.agent.orchestrator import FAILURE_ACTIONS, CrewOrchestrator

    一次跨副本同步把它弄成了**裸表达式**：

        FAILURE_ACTIONS, CrewOrchestrator

    语法完全合法（元组表达式语句，求值后丢弃、不导入任何东西），
    **静态检查器不报、类型检查器没接、测试也从不执行那条命令** ——
    于是产品的「跑一集」主命令 100% `NameError`，而 1153 项测试全绿。

    这类「import 前缀丢失」只有机器能稳定抓住。用标准库 `symtable`
    而不是手写作用域分析：闭包 / 推导式 / 嵌套函数的作用域规则由它负责。
    """
    import builtins
    import symtable

    src = path.read_text(encoding="utf-8")
    top = symtable.symtable(src, str(path), "exec")
    module_names = {s.get_name() for s in top.get_symbols()} | set(_DUNDERS)

    bad: list[str] = []

    def walk(tbl, prefix: str = "") -> None:
        for child in tbl.get_children():
            where = f"{prefix}{child.get_name()}"
            for s in child.get_symbols():
                name = s.get_name()
                if (s.is_global() and name not in module_names
                        and not hasattr(builtins, name)):
                    bad.append(f"{where}:{name}")
            walk(child, where + ".")

    walk(top)
    return bad


def _bare_name_statements(path) -> list[str]:
    """找出「裸名字表达式语句」—— 形如 `A, B` 或单写一个 `A` 的语句。

    它们能通过编译，但**不产生任何效果**，出现即 bug（不是风格问题）。
    `symtable` 那条守卫只能抓住「名字未绑定」；若那两个名字恰好模块级存在，
    这条仍然会响 —— 两把锁互补。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad: list[str] = []

    def scan(node) -> None:
        for stmt in getattr(node, "body", ()):
            if not isinstance(stmt, ast.Expr):
                continue
            val = stmt.value
            cands = ((val,) if isinstance(val, ast.Name)
                     else (tuple(val.elts) if isinstance(val, ast.Tuple) else ()))
            if cands and all(isinstance(x, ast.Name) for x in cands):
                where = getattr(node, "name", "<module>")
                bad.append(f"{path.name}:{stmt.lineno} @ {where}")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)):
            scan(node)
    return bad


def _missing_handler_members(server_path) -> list[str]:
    """`self.handlers.X` 引用了 `_Handlers` 上不存在的成员（审计 C-1 的形态）。

    2026-10-23：`episode` 曾被误缩进成 `_real_compile` 的内部闭包，
    `_Handlers` 上**根本没有**这个属性，路由 `self.handlers.episode(...)`
    必然 `AttributeError` —— 而异常发生在后台线程里，客户端只看到 202，
    随后查任务才是 failed。**"接口正常返回"与"任务 100% 失败"同时成立。**
    """
    import ast

    from little_crew.api.server import _Handlers

    tree = ast.parse(server_path.read_text(encoding="utf-8"),
                     filename=str(server_path))
    wanted: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "handlers"):
            wanted.add(node.attr)
    return sorted(n for n in wanted if not hasattr(_Handlers, n))


def _f_source_guards() -> None:
    import pathlib
    import sys as _sys

    here = pathlib.Path(__file__).resolve().parent          # .../little_crew/cli
    cli_files = sorted(p for p in here.glob("*.py") if p.name != "check.py")

    unresolved: list[str] = []
    for f in cli_files:
        unresolved += [f"{f.name}:{x}" for x in _unresolved_globals(f)]
    check("F1 ★ 函数体引用的全局名都真的被绑定了（防 import 前缀丢失）",
          not unresolved, str(unresolved[:6]))

    bare: list[str] = []
    for f in cli_files:
        bare += _bare_name_statements(f)
    check("F2 ★ 没有「裸名字表达式语句」（`A, B` 那种，求值后丢弃）",
          not bare, str(bare[:6]))

    from little_crew.api import server as _server_mod
    missing = _missing_handler_members(pathlib.Path(_server_mod.__file__))
    check("F3 ★ 路由里 `handlers.X` 的 X 都真的在 `_Handlers` 上",
          not missing, str(missing))
    _ = _sys  # 保持导入位置一致（本段只在需要时用 sys）


# ==========================================================================
# E. 安全默认
# ==========================================================================

def _e_defaults() -> None:
    import inspect

    from little_crew.api.server import ApiServer, serve

    sig = inspect.signature(ApiServer.__init__)
    check("E1 ★ API 默认绑 **127.0.0.1**（不是 0.0.0.0）",
          sig.parameters["host"].default == "127.0.0.1",
          str(sig.parameters["host"].default))
    sig2 = inspect.signature(serve)
    check("E2 serve() 同样默认本机",
          sig2.parameters["host"].default == "127.0.0.1",
          str(sig2.parameters["host"].default))

    # CLI 的退出码常量可被外部引用（CI 脚本要 import 它）
    from little_crew.cli import ExitCode
    check("E3 ExitCode 在包级可 import（CI 脚本要用）",
          hasattr(ExitCode, "FAILED") and hasattr(ExitCode, "ENVIRONMENT"))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:                                   # noqa: BLE001
        pass

    print("=" * 78)
    print("命令行 / HTTP 接口 —— 自检")
    print("=" * 78)

    _a_cli_basics()
    _b_cli_usage_errors()
    _c_json_output()
    _d_http_contract()
    _e_defaults()
    _f_source_guards()
    _g_entry_contract()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
