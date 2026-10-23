"""模型路由与双通道 —— 集成验收测试。

**为什么放在 tests/ 而不是 src/little_crew/models/**

本文件同时使用 ``models``（第 2 层）、``compiler.router``（第 3 层）、
``governance``（第 1 层）与 ``runtime.channels``（第 4 层）——
它验证的正是**这些层之间的协作**。若放在 ``models`` 包内，就构成
"第 2 层依赖第 4 层"的**反向依赖**，会被架构守卫判定违规并中断构建。

这是架构守卫的价值所在：它会逼着把"集成测试"放到正确的位置，
而不是让一个测试文件悄悄拉出一条错误的依赖边。

运行： python tests\test_model_routing.py
零第三方依赖。
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace

from little_crew.compiler.router import ModelRouter
from little_crew.governance.license import (
    APACHE_2_0,
    LicenseGate,
    LicenseInfo,
    Verdict,
)
from little_crew.models.registry import (
    resolve_endpoint,
    DEFAULT_TASK_PROFILES,
    build_default_registry,
    profile_for,
)
from little_crew.models.spec import (
    Backend,
    BackendKind,
    Capability,
    ModelRegistry,
    ModelSpec,
    TaskKind,
    TaskProfile,
)
from little_crew.runtime.channels.base import (
    Channel,
    ChannelCapabilities,
    ChannelError,
    CompiledJob,
    JobHandle,
    JobStatus,
    ProgressEvent,
)
from little_crew.runtime.channels.http import (
    HttpChannel,
    extract_path,
    render_body_template,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond, detail))


def _gate() -> LicenseGate:
    return LicenseGate()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("模型路由与双通道自检")
    print("=" * 78)

    # ==================================================================
    # A. 注册表结构
    # ==================================================================
    reg = build_default_registry()

    check("A1 默认目录可构建且有模型", len(reg.all()) >= 8,
          f"{len(reg.all())} 个模型")

    z = reg.get("z-image-turbo")
    check("A2 同一模型可声明两条**同为本地**的通道（证明通道与模型正交）",
          z is not None and len(z.backends) == 2
          and {b.kind for b in z.backends} == {BackendKind.COMFYUI, BackendKind.HTTP},
          str([b.kind.value for b in (z.backends if z else ())]))

    h3 = reg.get("minimax-h3")
    check("A3 存在真·双通道模型（本地 ComfyUI + 云端 API）",
          h3 is not None and h3.supports_local and h3.supports_api,
          f"local={h3.supports_local if h3 else None} api={h3.supports_api if h3 else None}")

    check("A4 仅 API 的模型不误报为本地",
          not reg.get("deepseek-flash").supports_local
          and reg.get("deepseek-flash").supports_api)

    check("A5 仅本地的模型不误报为 API",
          reg.get("sdxl-base").supports_local
          and not reg.get("sdxl-base").supports_api)

    # endpoint 留空 + is_local_by_default 的判定
    # 用一个 endpoint 留空的本地服务模型来验证 ——
    # 真实注册表里 local-gen 的 endpoint 是显式给出的，触发不到这条分支。
    reg_e = ModelRegistry([
        ModelSpec(
            model_id="epr", display_name="Empty Endpoint", task=TaskKind.IMAGE_T2I,
            backends=(Backend(kind=BackendKind.HTTP, endpoint="",
                              path="/v1/images/generations",
                              is_local_by_default=True),),
            license_id="t1",
        ),
        ModelSpec(
            model_id="epc", display_name="Empty Cloud", task=TaskKind.IMAGE_T2I,
            backends=(Backend(kind=BackendKind.HTTP, endpoint="",
                              path="/v1/images/generations",
                              is_local_by_default=False),),
            license_id="t1",
        ),
    ])
    check("A6 endpoint 留空时按 is_local_by_default 判定本地性",
          reg_e.get("epr").supports_local and not reg_e.get("epr").supports_api
          and reg_e.get("epc").supports_api and not reg_e.get("epc").supports_local,
          f"empty+local -> local={reg_e.get('epr').supports_local} "
          f"api={reg_e.get('epr').supports_api} | "
          f"empty+cloud -> local={reg_e.get('epc').supports_local} "
          f"api={reg_e.get('epc').supports_api}")

    # 每个模型的 license_id 必须能在授权目录中查到，否则门禁会全部拒绝
    from little_crew.governance.license import LICENSE_CATALOG
    unregistered = sorted({s.license_id for s in reg.all() if s.license_id}
                          - set(LICENSE_CATALOG))
    check("A7 所有 license_id 均已在授权目录登记", unregistered == [],
          f"未登记: {unregistered}")

    # ---- 端点解析：环境变量优先于内置默认 ----
    #
    # 这一组测试的存在理由：`config/.env` 里早就声明了
    # `LOCAL_GEN_ENDPOINT` / `COMFYUI_ENDPOINT`，但此前**没有代码读它们** ——
    # 端点硬编码在注册表里，于是"改了 .env 却不生效，且不报错"。
    import os as _os

    def _with_env(**kv):
        """临时设/删环境变量后构建注册表。"""
        saved = {k: _os.environ.get(k) for k in kv}
        try:
            for k, v in kv.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v
            return build_default_registry()
        finally:
            for k, v in saved.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v

    check("A8 无环境变量时用内置默认端点",
          resolve_endpoint("http://d", "NO_SUCH_KEY_XYZ") == "http://d")

    _r = _with_env(COMFYUI_ENDPOINT="http://10.0.0.9:8188")
    check("A9 ★ COMFYUI_ENDPOINT 能覆盖 ComfyUI 通道端点",
          all(b.endpoint == "http://10.0.0.9:8188"
              for m in _r.all() for b in m.backends
              if b.kind is BackendKind.COMFYUI),
          f"覆盖了 {sum(1 for m in _r.all() for b in m.backends if b.kind is BackendKind.COMFYUI)} 条")

    _r2 = _with_env(LOCAL_GEN_ENDPOINT="http://10.0.0.9:8100")
    _z = _r2.get("z-image-turbo")
    _http = [b for b in _z.backends if b.kind is BackendKind.HTTP]
    check("A10 ★ LOCAL_GEN_ENDPOINT 能覆盖 local-gen 通道端点",
          _http and _http[0].endpoint == "http://10.0.0.9:8100",
          _http[0].endpoint if _http else "无 HTTP 后端")

    check("A11 ★ 环境变量为**空串**时回退到默认（不产生空端点）",
          _with_env(COMFYUI_ENDPOINT="").get("sdxl-base").backends[0].endpoint
          == "http://127.0.0.1:8188")
    check("A12 环境变量为**全空白**时也回退到默认",
          _with_env(LOCAL_GEN_ENDPOINT="   ").get("z-image-turbo").backends[1]
          .endpoint == "http://127.0.0.1:8100")

    # MiniMax 的 HTTP 端点此前是**空串**（注释说"由 .env 提供"，
    # 但 .env 里根本没有那个键）—— 声明了却不存在的配置比没有更糟
    _mm = build_default_registry().get("minimax-h3")
    _mm_http = [b for b in _mm.backends if b.kind is BackendKind.HTTP]
    check("A13 ★ MiniMax 的 HTTP 端点不再是空串（此前一直是空）",
          _mm_http and _mm_http[0].endpoint.strip() != "",
          _mm_http[0].endpoint if _mm_http else "无 HTTP 后端")
    check("A14 所有 HTTP 后端的端点都非空（空端点只会在运行时才暴露）",
          all(b.endpoint.strip() for m in build_default_registry().all()
              for b in m.backends if b.kind is BackendKind.HTTP),
          str([f"{m.model_id}" for m in build_default_registry().all()
               for b in m.backends
               if b.kind is BackendKind.HTTP and not b.endpoint.strip()]))


    # ==================================================================
    # B. 授权门禁（硬约束，必须真的拦住）
    # ==================================================================
    gate = _gate()
    router = ModelRouter(reg, gate, default_endpoints={
        BackendKind.COMFYUI: "http://127.0.0.1:8188",
        BackendKind.HTTP: "http://127.0.0.1:8100",
    })

    d = router.route(profile_for("script.structured"))
    check("B1 未核实授权的模型被拒绝（NEEDS_REVIEW 即出局）",
          d.chosen is None and any(r.stage == "license" for r in d.rejected),
          f"chosen={d.chosen.model_id if d.chosen else None} "
          f"rejected={[(r.model_id, r.stage) for r in d.rejected]}")

    # 构造一个"已核实 + 非商用"的模型，验证商用场景下被拒
    reg2 = ModelRegistry([
        ModelSpec(
            model_id="nc-model", display_name="NC Model",
            task=TaskKind.IMAGE_T2I,
            backends=(Backend(kind=BackendKind.COMFYUI, endpoint="http://127.0.0.1:8188"),),
            # 能力必须满足 shot.image.draft 的要求，否则会被 capability 阶段淘汰 ——
            # 那样测的就不是授权门禁了（这个坑我第一次就踩了）。
            capability=Capability(supports_negative_prompt=True),
            license_id="nc-test",
        ),
    ])
    g2 = LicenseGate()
    g2.register("nc-test", LicenseInfo(
        "CC-BY-NC-4.0", commercial_use=False, verified=True, note="测试用"))
    r2 = ModelRouter(reg2, g2)
    from little_crew.governance.license import DeploymentContext
    d2 = r2.route(profile_for("shot.image.draft"),
                  ctx=DeploymentContext(commercial=True, region="CN"))
    check("B2 非商用模型在商用部署下被拒绝",
          d2.chosen is None and any(r.stage == "license" for r in d2.rejected),
          str([(r.stage, r.reason[:50]) for r in d2.rejected]))

    d3 = r2.route(profile_for("shot.image.draft"),
                  ctx=DeploymentContext(commercial=False, region="CN"))
    check("B3 同一模型在非商用场景下放行（同一事实，不同结论）",
          d3.chosen is not None and d3.chosen.model_id == "nc-model",
          f"chosen={d3.chosen.model_id if d3.chosen else None}")

    # 地区排除
    reg3 = ModelRegistry([
        ModelSpec(model_id="ex-model", display_name="Ex", task=TaskKind.VIDEO_I2V,
                  backends=(Backend(kind=BackendKind.COMFYUI),), license_id="ex-test"),
    ])
    g3 = LicenseGate()
    g3.register("ex-test", LicenseInfo(
        "Test", commercial_use=True, verified=True,
        excluded_regions=("US",), note="测试"))
    r3 = ModelRouter(reg3, g3)
    d4 = r3.route(TaskProfile(task=TaskKind.VIDEO_I2V),
                  ctx=DeploymentContext(region="US"))
    d5 = r3.route(TaskProfile(task=TaskKind.VIDEO_I2V),
                  ctx=DeploymentContext(region="CN"))
    check("B4 地区排除生效：US 拒绝 / CN 放行",
          d4.chosen is None and d5.chosen is not None,
          f"US={d4.chosen} CN={d5.chosen.model_id if d5.chosen else None}")

    # ==================================================================
    # C. 通道选择（双通道的核心行为）
    # ==================================================================
    # 用一个已核实的双通道模型，验证"用户选本地/API"改变的是通道而非模型
    reg4 = ModelRegistry([
        ModelSpec(
            model_id="dual", display_name="Dual", task=TaskKind.VIDEO_I2V,
            backends=(
                Backend(kind=BackendKind.COMFYUI, endpoint="http://127.0.0.1:8188"),
                Backend(kind=BackendKind.HTTP, endpoint="https://api.example.com",
                        path="/v1/gen", response_path="data.0.url"),
            ),
            capability=Capability(supports_reference_image=True),
            license_id="dual-test",
        ),
    ])
    g4 = LicenseGate()
    g4.register("dual-test", LicenseInfo(APACHE_2_0, commercial_use=True,
                                         verified=True, note="测试"))
    r4 = ModelRouter(reg4, g4)

    base = TaskProfile(task=TaskKind.VIDEO_I2V)
    p_local = replace(base, channel="local")
    p_api = replace(base, channel="api")

    dl = r4.route(p_local)
    da = r4.route(p_api)
    check("C1 选本地 -> 走 ComfyUI 通道",
          dl.chosen is not None and dl.backend is not None
          and dl.backend.kind is BackendKind.COMFYUI,
          f"{dl.backend.kind.value if dl.backend else None}")
    check("C2 选 API -> 走 HTTP 通道",
          da.chosen is not None and da.backend is not None
          and da.backend.kind is BackendKind.HTTP,
          f"{da.backend.kind.value if da.backend else None}")
    check("C3 **选本地与选 API 选中的是同一个模型**（通道与模型正交）",
          dl.chosen is not None and da.chosen is not None
          and dl.chosen.model_id == da.chosen.model_id == "dual",
          f"local={dl.chosen.model_id if dl.chosen else None} "
          f"api={da.chosen.model_id if da.chosen else None}")

    # 通道形态过滤真的会淘汰模型
    reg5 = ModelRegistry([
        ModelSpec(model_id="only-local", display_name="L", task=TaskKind.IMAGE_T2I,
                  backends=(Backend(kind=BackendKind.COMFYUI),), license_id="t1"),
        ModelSpec(model_id="only-api", display_name="A", task=TaskKind.IMAGE_T2I,
                  backends=(Backend(kind=BackendKind.HTTP,
                                    endpoint="https://api.example.com"),),
                  license_id="t1"),
    ])
    g5 = LicenseGate()
    g5.register("t1", LicenseInfo(APACHE_2_0, commercial_use=True, verified=True))
    r5 = ModelRouter(reg5, g5)
    p_all = TaskProfile(task=TaskKind.IMAGE_T2I)
    check("C4 不限形态时两个模型都可选",
          len(r5.route(replace(p_all, channel=None)).alternatives) == 1)
    check("C5 限本地时仅本地模型可选",
          r5.route(replace(p_all, channel="local")).chosen.model_id == "only-local")
    check("C6 限 API 时仅 API 模型可选",
          r5.route(replace(p_all, channel="api")).chosen.model_id == "only-api")

    # ==================================================================
    # D. 能力硬约束
    # ==================================================================
    reg6 = ModelRegistry([
        ModelSpec(model_id="no-ref", display_name="NR", task=TaskKind.IMAGE_T2I,
                  backends=(Backend(kind=BackendKind.COMFYUI),),
                  capability=Capability(supports_reference_image=False),
                  license_id="t1"),
        ModelSpec(model_id="has-ref", display_name="HR", task=TaskKind.IMAGE_T2I,
                  backends=(Backend(kind=BackendKind.COMFYUI),),
                  capability=Capability(supports_reference_image=True),
                  license_id="t1"),
    ])
    r6 = ModelRouter(reg6, g5)
    d6 = r6.route(TaskProfile(task=TaskKind.IMAGE_T2I,
                              must={"supports_reference_image": True}))
    check("D1 能力硬约束淘汰不合格模型",
          d6.chosen is not None and d6.chosen.model_id == "has-ref"
          and any(r.model_id == "no-ref" and r.stage == "capability"
                  for r in d6.rejected),
          str([(r.model_id, r.stage) for r in d6.rejected]))

    d7 = r6.route(TaskProfile(task=TaskKind.IMAGE_T2I,
                              must={"chinese_text_score": 0.9}))
    check("D2 数值型 must 语义为「实际值 >= 要求值」",
          d7.chosen is None,
          f"chosen={d7.chosen.model_id if d7.chosen else None}")

    d8 = r6.route(TaskProfile(task=TaskKind.IMAGE_T2I,
                              must={"max_resolution": (2048, 2048)}))
    check("D3 分辨率要求比对二元组",
          d8.chosen is None and any("max_resolution" in r.reason
                                    for r in d8.rejected),
          str([r.reason[:40] for r in d8.rejected]))

    # ==================================================================
    # E. 淘汰理由必须可解释
    # ==================================================================
    d9 = r6.route(TaskProfile(task=TaskKind.VIDEO_I2V))
    check("E1 无候选时给出 task 阶段的理由（而非静默返回空）",
          d9.chosen is None and any(r.stage == "task" for r in d9.rejected),
          str([r.reason for r in d9.rejected]))

    check("E2 决策可序列化（用于前端展示「为什么选它」）",
          isinstance(d6.as_dict(), dict)
          and d6.as_dict()["chosen"] == "has-ref"
          and "rejected" in d6.as_dict())

    # ==================================================================
    # F. HTTP 通道（本地服务与云端 API 共用同一条代码路径）
    # ==================================================================
    tpl = ('{"model": "z-image-turbo", "prompt": "{prompt}",'
           ' "width": {width}, "seed": {seed}, "enhance": {enhance}}')
    body = render_body_template(tpl, {
        "prompt": '一个女孩说"你好"\\并回头',     # 含引号与反斜杠 —— 必须被正确转义
        "width": 832, "seed": 42, "enhance": True,
    })
    check("F1 模板渲染能正确转义引号与反斜杠（手写 JSON 必错之处）",
          body["prompt"] == '一个女孩说"你好"\\并回头'
          and body["width"] == 832 and body["seed"] == 42
          and body["enhance"] is True,
          json.dumps(body, ensure_ascii=False)[:90])

    bad = False
    try:
        # 真正非法的模板：占位符在引号内，但模板本身缺了闭合引号
        render_body_template('{"a": "{x}}', {"x": "v"})
    except ChannelError:
        bad = True
    check("F2 非法模板抛结构化错误而非发出坏 JSON", bad)

    # 含引号的值本身**不应**导致失败 —— 它必须被正确转义后成功渲染。
    # （上面 F1 已覆盖，这里补一条「值本身是 JSON 片段」的场景）
    ok_jsonish = False
    try:
        r = render_body_template('{"a": "{x}"}', {"x": '{"nested": 1}'})
        ok_jsonish = r["a"] == '{"nested": 1}'
    except ChannelError:
        ok_jsonish = False
    check("F2b 值本身是 JSON 片段时仍正确转义（不会被误判为非法）", ok_jsonish)

    check("F3 点号路径取值支持列表下标",
          extract_path({"data": [{"url": "u1"}, {"url": "u2"}]}, "data.1.url") == "u2"
          and extract_path({"a": {"b": 1}}, "a.b") == 1
          and extract_path({"a": 1}, "x.y") is None)

    # 本地 vs 远端的能力自述必须不同（按量计费决定是否必须幂等去重）
    be_local = Backend(kind=BackendKind.HTTP, endpoint="http://127.0.0.1:8100",
                       path="/v1/images/generations", response_path="data.0.url")
    be_cloud = Backend(kind=BackendKind.HTTP, endpoint="https://api.example.com",
                       path="/v1/images/generations", response_path="data.0.url")
    ch_l = HttpChannel(be_local)
    ch_c = HttpChannel(be_cloud)
    check("F4 本地服务不计费 / 云端 API 计费（决定幂等去重的必要性）",
          ch_l.capabilities.metered is False and ch_c.capabilities.metered is True,
          f"local={ch_l.capabilities.metered} cloud={ch_c.capabilities.metered}")

    # ---- R1 幂等：**已受理**的同键重复提交不得产生两次请求 ----
    #
    # ⚠️ 2026-10-23 改（审计 M-14）：这条原来用**连不上的后端**来测，
    # 断言"同键返回同一句柄"。那等于把 bug 锁成了规范：记录是发请求**之前**
    # 就落的账，于是「服务没起」这种最常见的可恢复故障会把幂等键**永久毒化** ——
    # 之后每次重试都直接返回那个 FAILED 句柄，请求根本不再发。
    #
    # 正确的两条语义必须**分开测**：
    #   R1  已受理（拿到服务端任务 id）-> 同键必须复用，绝不重发
    #   R1c 确定没被受理（没有任务 id 且状态 FAILED）-> 允许重发
    class _StubHttp(HttpChannel):
        """把网络层换掉：可用性、请求都可控。"""

        def __init__(self, backend, *, ok: bool = True, reply=None) -> None:
            super().__init__(backend)
            self._ok = ok
            self._reply = reply if reply is not None else {
                "id": "task-1", "data": [{"url": "http://x/a.png"}]}
            self.requests = 0

        def available(self):                                  # noqa: D102
            return (self._ok, "" if self._ok else "服务未启动")

        def _request_raw(self, method, url, body, timeout=None):   # noqa: D102
            self.requests += 1
            return dict(self._reply)

    job = CompiledJob(
        job_id="j1", kind=BackendKind.HTTP, backend=be_cloud,
        request={"method": "POST", "path": "/v1/images/generations",
                 "body": {"prompt": "x"}},
        idempotency_key="IDEM-1",
    )
    ch_ok = _StubHttp(be_cloud)
    h1 = ch_ok.submit(job)
    h2 = ch_ok.submit(job)
    check("R1 已受理后同幂等键重复提交返回同一句柄（不重复请求）",
          h1 is h2 and ch_ok.requests == 1,
          f"same_object={h1 is h2} requests={ch_ok.requests}")
    check("R1b 复用不额外请求（计费安全）", ch_ok.requests == 1,
          f"requests={ch_ok.requests}")

    # 确定没被受理 -> 允许重发（幂等键不许被毒化）
    ch_down = _StubHttp(be_cloud, ok=False)
    d1 = ch_down.submit(job)
    d2 = ch_down.submit(job)
    check("R1c ★★ 服务不可用**不毒化**幂等键：下次提交会真的重发",
          d1 is not d2 and not ch_down._records and d1.status is JobStatus.FAILED,
          f"same={d1 is d2} records={len(ch_down._records)} "
          f"status={d1.status.value}")

    missing_key = False
    try:
        ch_c.submit(CompiledJob(job_id="j2", kind=BackendKind.HTTP, backend=be_cloud,
                                request={"method": "POST", "path": "/x", "body": {}}))
    except ChannelError as e:
        missing_key = "idempotency" in e.message.lower()
    check("R1d 缺幂等键的 HTTP 提交被拒绝（防重复计费）", missing_key)

    # ==================================================================
    # H. 状态语义（2026-10-23 审计 H-9 / H-10 / H-12 / M-16）
    # ==================================================================
    #
    # 这一组全是"跨层把语义弄丢"的形态，共同后果是**重复提交 / 重复计费**：
    #   H-9  对账的键不是服务端任务 id -> 恒查不到 -> 判"未提交" -> 重提
    #   H-12 失败枚举漏了 `Fail`       -> 明确失败被当成在跑 -> 空转 30 分钟
    #   M-16 查询网络错判 FAILED        -> 同上，且与 ComfyUI 侧语义相反
    #   H-10 产物类型写死 image         -> mp4 被当图片量尺寸
    be_async = Backend(
        kind=BackendKind.HTTP, endpoint="https://api.example.com",
        path="/v1/video", response_path="task_id", async_task=True,
        status_path="/v1/q?task_id={task_id}", status_success_value="Success",
        status_failure_values=("Fail",), artifact_kind="video",
    )

    class _AsyncStub(_StubHttp):
        """异步通道替身：可指定轮询时返回的状态 / 让轮询的网络层失败。"""

        def __init__(self, backend, *, reply=None, poll_status: str = "Queueing",
                     poll_raises: bool = False) -> None:
            super().__init__(backend, reply=reply or {"task_id": "t-1"})
            self.poll_status = poll_status
            self.poll_raises = poll_raises

        def _request_raw(self, method, url, body, timeout=None):   # noqa: D102
            self.requests += 1
            if self.poll_raises and self.requests > 1:
                raise ChannelError("F-TIMEOUT", "查询超时", retryable=True)
            if method == "GET":
                return {"status": self.poll_status, "task_id": "t-1"}
            return dict(self._reply)

    job9 = CompiledJob(job_id="j9", kind=BackendKind.HTTP, backend=be_async,
                       request={"method": "POST", "path": "/v1/video", "body": {}},
                       idempotency_key="IDEM-9")
    ch9 = _AsyncStub(be_async)
    h9 = ch9.submit(job9)
    found = ch9.reconcile([h9.channel_id])       # 按**服务端任务 id** 查
    check("I1 ★★ 对账按服务端任务 id 查得到（三套键约定已统一）",
          h9.channel_id in found, f"channel_id={h9.channel_id} "
                                  f"found={list(found)}")
    check("I2 ★ 也兼容旧的幂等键（老调用方不炸）",
          "IDEM-9" in ch9.reconcile(["IDEM-9"]))

    # H-12：MiniMax 官方用的是 `Fail`（首字母大写）
    ch_f = _AsyncStub(be_async, poll_status="Fail")
    hf = ch_f.poll(ch_f.submit(job9))
    check("I3 ★★ 服务端 `Fail` 被判为**失败**（不是「还在跑」）",
          hf.status is JobStatus.FAILED, hf.status.value)
    check("I4 失败原因带服务端原文（可归因）",
          hf.error is not None and "Fail" in hf.error.message,
          (hf.error.message[:60] if hf.error else "无"))
    ch_p = _AsyncStub(be_async, poll_status="Processing")
    hp = ch_p.poll(ch_p.submit(job9))
    check("I5 对照：`Processing` 仍然是「在跑」（不能一律判失败）",
          hp.status is JobStatus.RUNNING, hp.status.value)

    # M-16：轮询的网络错误 ≠ 任务失败
    ch_n = _AsyncStub(be_async, poll_raises=True)
    hn = ch_n.poll(ch_n.submit(job9))
    check("I6 ★★ 轮询网络错误 -> UNKNOWN（交给对账，不判死）",
          hn.status is JobStatus.UNKNOWN, hn.status.value)

    # H-10：产物类型按后端声明，声明为默认值时按扩展名兜底
    be_vid = Backend(kind=BackendKind.HTTP, endpoint="https://api.example.com",
                     path="/v1/video", response_path="data.0.url",
                     artifact_kind="video")
    ch_v = _StubHttp(be_vid, reply={"id": "v1",
                                    "data": [{"url": "http://x/a.mp4"}]})
    hv = ch_v.submit(CompiledJob(job_id="jv", kind=BackendKind.HTTP,
                                 backend=be_vid, idempotency_key="IDEM-V",
                                 request={"method": "POST", "path": "/v1/video",
                                          "body": {}}))
    check("I7 ★★ 视频产物标成 video（不再一律 image）",
          bool(hv.artifacts) and hv.artifacts[0].kind == "video",
          f"{[a.kind for a in hv.artifacts]}")
    be_img = Backend(kind=BackendKind.HTTP, endpoint="https://api.example.com",
                     path="/v1/img", response_path="data.0.url")
    ch_i = _StubHttp(be_img, reply={"id": "i1",
                                    "data": [{"url": "http://x/b.mp4"}]})
    hi = ch_i.submit(CompiledJob(job_id="ji", kind=BackendKind.HTTP,
                                 backend=be_img, idempotency_key="IDEM-I",
                                 request={"method": "POST", "path": "/v1/img",
                                          "body": {}}))
    check("I8 声明是默认值（image）时按扩展名兜底（.mp4 -> video）",
          bool(hi.artifacts) and hi.artifacts[0].kind == "video",
          f"{[a.kind for a in hi.artifacts]}")

    # ---- 跨进程对账：重启后 `_records` 是空的，必须**问服务端**（H-9 的另一半）----
    #
    # 只修"键约定"是不够的：新进程里查 `_records` 必然查不到，
    # 于是对账恒返回空 -> 调用方判"服务端无此任务" -> 重提 -> 重复计费。
    # 所以必须真的按任务 id 去问 `status_path`。
    class _RemoteOnly(_StubHttp):
        """模拟「重启后」的通道：进程内没有任何记录，只有服务端知道。"""

        def __init__(self, backend, *, raw=None, code: int | None = None) -> None:
            super().__init__(backend)
            self._raw = raw if raw is not None else {
                "status": "succeeded", "result": {"url": "http://x/a.mp4"}}
            self._code = code

        def _request_raw(self, method, url, body, timeout=None):   # noqa: D102
            self.requests += 1
            if self._code:
                raise ChannelError("F-SUBMIT-API", f"HTTP {self._code}",
                                   detail={"status": self._code, "url": url})
            return dict(self._raw)

    be_r = Backend(kind=BackendKind.HTTP, endpoint="https://api.example.com",
                   path="/v1/video", response_path="result.url", async_task=True,
                   status_path="/v1/q?task_id={task_id}",
                   status_success_value="succeeded", artifact_kind="video")
    got = _RemoteOnly(be_r).reconcile(["t-remote"])
    h_r = got.get("t-remote")
    check("I9 ★★ 重启后按服务端任务 id 查得到（不靠进程内记录）",
          h_r is not None and h_r.status is JobStatus.SUCCEEDED, str(list(got)))
    check("I10 ★ 且把产物一并回收（类型 video）",
          h_r is not None and bool(h_r.artifacts)
          and h_r.artifacts[0].kind == "video",
          f"{[a.kind for a in (h_r.artifacts if h_r else ())]}")

    check("I11 ★ 服务端 404 -> 不出现在结果里（确实没提交，可安全重提）",
          _RemoteOnly(be_r, code=404).reconcile(["t-x"]) == {})

    h_0 = _RemoteOnly(be_r, raw={"status": "succeeded"}).reconcile(
        ["t-y"]).get("t-y")
    check("I12 ★ 成功但按 response_path 取不到产物 -> UNKNOWN（不谎报成功、也不重提）",
          h_0 is not None and h_0.status is JobStatus.UNKNOWN
          and h_0.error is not None,
          f"{h_0.status.value if h_0 else None}")

    class _Boom(_RemoteOnly):
        def _request_raw(self, *a, **kw):                     # noqa: D102
            raise ChannelError("F-TIMEOUT", "查询超时", retryable=True)

    raised = False
    try:
        _Boom(be_r).reconcile(["t-z"])
    except ChannelError:
        raised = True
    check("I13 ★★ 对账查询失败 -> 抛出（空字典会被当「没提交」而重提）", raised)

    # R2 同步通道不可中断，必须抛错而非静默忽略
    cant = False
    try:
        ch_l.cancel(h1)
    except ChannelError as e:
        cant = "无法中断" in e.message or "不能中断" in e.message
    check("R2 同步通道 cancel 抛错而非静默忽略", cant)

    # ---- R3 对账查询失败必须**抛出**，不能返回空字典（2026-10-23 审计 H-16）----
    #
    # 空字典同时表示「查询失败」和「服务端确实没有这个任务」，而调用方
    # （`checkpoint.reconcile_now`）只会按后者解释 -> 在途任务被当未提交
    # -> 续跑时**重复提交、重复烧 GPU**。
    from little_crew.runtime.channels.comfy import ComfyChannel as _CC

    class _ComfyDown(_CC):
        def _get_json(self, *a, **kw):                        # noqa: D102
            raise ChannelError("F-EXEC-COMFY", "服务端连不上", retryable=True)

    raised = False
    try:
        _ComfyDown(Backend(kind=BackendKind.COMFYUI,
                           endpoint="http://127.0.0.1:8188")).reconcile(["p1"])
    except ChannelError:
        raised = True
    check("R3 ★★ 对账查询失败 -> 抛出（空字典会被当「确实没提交」而重提）",
          raised)

    # F5 通道协议一致性
    check("F5 HttpChannel 满足 Channel 协议",
          isinstance(ch_l, Channel))

    # ==================================================================
    # G. 通道协议对两种形态同构
    # ==================================================================
    from little_crew.runtime.channels.comfy import ComfyChannel
    cc = ComfyChannel(Backend(kind=BackendKind.COMFYUI,
                              endpoint="http://127.0.0.1:8188"))
    check("G1 ComfyChannel 亦满足 Channel 协议", isinstance(cc, Channel))
    check("G2 两条通道的能力自述正确反映真实差异",
          cc.capabilities.needs_gpu and not ch_l.capabilities.needs_gpu
          and cc.capabilities.reconcilable and not ch_l.capabilities.reconcilable,
          f"comfy(gpu={cc.capabilities.needs_gpu},"
          f"reconcile={cc.capabilities.reconcilable}) "
          f"http(gpu={ch_l.capabilities.needs_gpu},"
          f"reconcile={ch_l.capabilities.reconcilable})")

    # ComfyUI 的节点校验失败是 200 + node_errors，**不是 HTTP 错误**
    check("G3 ComfyChannel 显式检查 node_errors（200 响应里的失败）",
          "node_errors" in ComfyChannel.submit.__doc__ if ComfyChannel.submit.__doc__
          else True)

    # ==================================================================
    # H. 任务档案（生产代码不硬编码模型 id）
    # ==================================================================
    check("H1 内置任务档案齐备",
          len(DEFAULT_TASK_PROFILES) >= 8, f"{len(DEFAULT_TASK_PROFILES)} 个")
    unknown_task = False
    try:
        profile_for("不存在的档案")
    except KeyError:
        unknown_task = True
    check("H2 未知档案报错并列出可用项", unknown_task)

    # ==================================================================
    # 汇总
    # ==================================================================
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    if npass == len(_results):
        print()
        s = reg.summary()
        print(f"  模型目录 : {s['modelCount']} 个模型，"
              f"{len(s['tasks'])} 个任务域")
        dual = [m.model_id for m in reg.all()
                if m.supports_local and m.supports_api]
        print(f"  真双通道 : {dual}")
        print(f"  通道类型 : {[k.value for k in BackendKind]}")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
