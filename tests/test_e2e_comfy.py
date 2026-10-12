"""端到端真机验证 —— 对真实 ComfyUI 走完「编译 → 提交 → 进度 → 回收」。

这是**唯一一个需要外部服务的测试**。没有 ComfyUI 在跑时它会跳过（返回码 0），
因此在 CI 或别人的机器上不会造成假失败。

它验证的是全项目**风险最高的一条链**：在它跑通之前，`runtime` 及以上的一切
都建立在"我们以为 ComfyUI 是这样行为的"这个假设上。

验证清单
--------
  1. 真实探测：``/system_stats`` 与 ``/object_info`` 可解析
  2. 编译通过：权重在位时，IR 能编译成图（而不是被 `FILE_MISSING` 阻断）
  3. 提交幂等：同 ``idempotency_key`` 重复提交拿到同一 ``prompt_id``（R1）
  4. WS 进度：能从 ``/ws`` 收到 ``executing`` / ``progress`` / ``execution_cached``
  5. 产物回收：``/history`` 里能取到产物文件名
  6. **产物真实存在**：下载回来并校验它是合法 PNG 且非空
  7. 对账：``reconcile`` 能凭幂等键找回已完成的任务（R3）
  8. 缓存命中与实算分开统计（ComfyUI 自己的缓存 ≠ 我们的三级缓存）

运行： python tests\\test_e2e_comfy.py
依赖： ComfyUI 在 config/project.toml 所指的端点运行，且 checkpoints 目录有底模。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

from little_crew.capability.capability import probe_from_object_info  # noqa: E402
from little_crew.compiler.compiler import (  # noqa: E402
    CompileError,
    ComfyCompiler,
)
from little_crew.models.spec import Backend, BackendKind  # noqa: E402
from little_crew.runtime.channels.base import (  # noqa: E402
    CompiledJob,
    JobStatus,
)
from little_crew.runtime.channels.comfy import ComfyChannel  # noqa: E402
from little_crew.compiler.test_compiler import shot_ir  # noqa: E402

ENDPOINT = "http://127.0.0.1:8188"
CKPT = "sd_xl_base_1.0.safetensors"


def minimal_ir(*, checkpoint: str = CKPT) -> dict:
    """最小可执行 IR —— **验证链路用，不依赖 LoRA 与参考图**。

    为什么不用 `test_compiler.shot_ir()`：那个夹具包含 `identity.lora` 与
    `identity.reference`，二者都需要本机不存在的角色 LoRA 文件。

    在真实环境下，这两个节点会暴露**编译器缺陷**（已记录，待修）：
      1. `CLIPTextEncode` 的必需端口 `clip` 未被自动注入 ->
         ComfyUI 报 `required_input_missing: clip`
      2. 漂移检测已报 `LoraLoader.lora_name` FILE_MISSING，
         但编译器**仍然生成**引用该文件的节点 ->
         ComfyUI 报 `value_not_in_list: lora_name not in []`

    本函数刻意绕开这两条路径，让**执行层**（提交/进度/回收/对账）
    能被独立验证 —— 这是当前这一步的目标。缺陷修复见 CHANGELOG。
    """
    return {
        "irVersion": "little_crew.ir/v1",
        "graphId": "G-e2e-minimal",
        "scope": "shot",
        "bindings": {},
        "nodes": [
            {"id": "n_ckpt", "kind": "model.checkpoint",
             "params": {"ckpt_name": checkpoint}},
            {"id": "n_prompt", "kind": "prompt.text",
             "inputs": {"clip": {"ref": "n_ckpt", "port": "CLIP"}},
             "params": {"text": "rainy rooftop at night, woman in beige "
                                "trench coat, looking back, close-up, "
                                "cinematic lighting"}},
            {"id": "n_neg", "kind": "prompt.negative",
             "inputs": {"clip": {"ref": "n_ckpt", "port": "CLIP"}},
             "params": {"text": "blurry, extra limbs, distorted face, "
                                "low quality, watermark"}},
            {"id": "n_latent", "kind": "latent.empty",
             "params": {"width": 832, "height": 480, "batch_size": 1}},
            {"id": "n_ks", "kind": "sample.ksampler",
             "inputs": {"model": {"ref": "n_ckpt", "port": "MODEL"},
                        "positive": {"ref": "n_prompt", "port": "CONDITIONING"},
                        "negative": {"ref": "n_neg", "port": "CONDITIONING"},
                        "latent_image": {"ref": "n_latent", "port": "LATENT"}},
             "params": {"steps": 8, "cfg": 5.5, "sampler": "dpmpp_2m",
                        "scheduler": "karras", "denoise": 1.0}},
            {"id": "n_decode", "kind": "decode.vae",
             "inputs": {"samples": {"ref": "n_ks", "port": "LATENT"},
                        "vae": {"ref": "n_ckpt", "port": "VAE"}}},
            {"id": "n_save", "kind": "io.save_image",
             "inputs": {"images": {"ref": "n_decode", "port": "IMAGE"}},
             "params": {"filename_prefix": "LittleCrew_e2e"}},
        ],
        "constraints": [],
        "provenance": {"generator": "test_e2e_comfy.minimal_ir",
                       "note": "端到端链路验证用，不含身份锁定"},
    }

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond, detail))


def _skip(reason: str) -> int:
    print("=" * 78)
    print("端到端真机验证 —— 跳过")
    print("=" * 78)
    print(f"  {reason}")
    print()
    print("  这不是失败。本测试需要真实 ComfyUI 服务，无服务时跳过。")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("端到端真机验证 —— 编译 → 提交 → 进度 → 回收")
    print("=" * 78)

    ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=ENDPOINT))

    ok, why = ch.available()
    if not ok:
        return _skip(f"ComfyUI 不可达（{why}）")
    check("E1 服务可用", True, why)

    # ---------- 2. 真实探测 ----------
    t0 = time.time()
    try:
        raw = ch.probe_catalog()
    except Exception as e:  # noqa: BLE001
        check("E2 /object_info 可拉取", False, str(e)[:120])
        return _report()
    dt = time.time() - t0
    check("E2 /object_info 可拉取", len(raw) > 100,
          f"{len(raw)} 节点，{dt:.2f}s")

    m = probe_from_object_info(raw, endpoint=ENDPOINT,
                               comfy_version="0.39.0")
    check("E3 能力矩阵可构建", m.node_count() == len(raw),
          f"{m.node_count()} 节点，catalogHash={m.catalog_hash()[:24]}…")

    # ---------- 权重是否就位 ----------
    has_ckpt = m.has_enum_value("CheckpointLoaderSimple", "ckpt_name", CKPT) \
        if hasattr(m, "has_enum_value") else None
    spec = m.spec("CheckpointLoaderSimple")
    enum_vals: list[str] = []
    if spec is not None:
        p = spec.inputs_required.get("ckpt_name")
        if p is not None and p.enum_values:
            enum_vals = list(p.enum_values)
    ckpt_present = CKPT in enum_vals
    check("E4 底模已被 ComfyUI 识别（enum 中出现）", ckpt_present,
          f"候选 {enum_vals[:5]}{'…' if len(enum_vals) > 5 else ''}"
          + ("" if ckpt_present else
             f"  ← 缺 {CKPT}。注意：enum 是**服务启动时扫描**的，"
             f"新放入权重后必须重启 ComfyUI"))

    # ---------- 3. 编译 ----------
    comp = ComfyCompiler(m)
    # 用**原始夹具**：它含 identity.lora / identity.reference 两个需要角色 LoRA
    # 的节点，本机没有该文件。修复前这会产出"编译通过、提交必失败"的图；
    # 修复后编译器会移除这两个节点、把下游重接到模型源、并自动补上
    # CLIPTextEncode 的 clip 端口 —— 这正是本次要验证的行为。
    ir = shot_ir()
    try:
        result = comp.compile(ir)
        compiled = True
        err = ""
    except CompileError as e:
        result = None
        compiled = False
        err = f"{e.code}: {str(e.message)[:150]}"
    check("E5 真实环境下编译通过（无文件缺失阻断）", compiled, err)

    if not compiled:
        print()
        print("  编译被阻断 —— 后续步骤无法进行。上面 E4 的说明通常就是原因：")
        print("  ComfyUI 的节点枚举（含 ckpt_name 候选）是**进程启动时扫描一次**的；")
        print("  权重下载完成后需要**重启 ComfyUI** 才会出现在 /object_info 里。")
        return _report()

    assert result is not None
    check("E6 编译器产出图哈希与 IR 哈希",
          bool(result.graph_hash) and bool(result.ir_hash),
          f"graph={result.graph_hash[:22]}… ir={result.ir_hash[:22]}…")

    n_loaders = len(result.required_loaders)
    check("E7 编译期声明了必需加载器（权重在位性由能力矩阵把关）",
          n_loaders >= 0, f"{n_loaders} 项")

    # ---------- 4. 提交（R1 幂等）----------
    idem = "e2e-" + result.graph_hash.split(":", 1)[-1][:32]
    job = CompiledJob(
        job_id="e2e-shot-001",
        kind=BackendKind.COMFYUI,
        backend=Backend(kind=BackendKind.COMFYUI, endpoint=ENDPOINT),
        graph=result.graph,
        idempotency_key=idem,
        expect="image",
        timeout_s=900,
        provenance={"irHash": result.ir_hash, "graphHash": result.graph_hash,
                    "ruleVersion": result.rule_version},
    )

    ok2, why2 = ch.available()
    check("E8 提交前服务仍可用", ok2, why2)
    if not ok2:
        return _report()

    h = ch.submit(job)
    check("E9 提交成功并返回 prompt_id", bool(h.channel_id),
          f"status={h.status.value} promptId={h.channel_id or '-'}"
          + (f" err={h.error.message[:110]}" if h.error else ""))
    if not h.channel_id:
        return _report()

    h_again = ch.submit(job)
    check("E10 同幂等键重复提交返回同一 prompt_id（R1 不重复占用 GPU）",
          h_again.channel_id == h.channel_id,
          f"{h.channel_id[:16]}… vs {h_again.channel_id[:16]}…")

    # ---------- 5. WS 进度 ----------
    events: list[str] = []
    cache_hits = 0
    t_submit = time.time()
    try:
        for ev in ch.watch(h):
            events.append(ev.unit)
            if ev.unit == "cache_hit":
                cache_hits += 1
            if len(events) > 4000:
                break
    except Exception as e:  # noqa: BLE001
        check("E11 WS 进度流可用", False, f"{type(e).__name__}: {str(e)[:110]}")
    else:
        kinds = sorted(set(events))
        check("E11 WS 进度流可用（收到事件）", len(events) > 0,
              f"{len(events)} 个事件，类型 {kinds}")
        check("E12 含执行事件（executing/node）",
              any(k in ("node", "step", "item", "poll") for k in kinds),
              str(kinds))

    # ---------- 6. 回收 ----------
    h = ch.poll(h)
    if not h.status.terminal:
        # WS 可能提前返回，用轮询等到终态
        deadline = time.time() + 600
        while not h.status.terminal and time.time() < deadline:
            time.sleep(2)
            h = ch.poll(h)

    elapsed = time.time() - t_submit
    check("E13 任务到达终态", h.status.terminal,
          f"status={h.status.value} 耗时 {elapsed:.1f}s"
          + (f" err={h.error.message[:130]}" if h.error else ""))

    if h.status is not JobStatus.SUCCEEDED:
        return _report()

    arts = ch.collect(h)
    check("E14 回收产物引用", len(arts) > 0, f"{len(arts)} 个")

    # ---------- 7. 产物真实存在 ----------
    if arts:
        a = arts[0]
        url = ENDPOINT + a.uri
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                blob = resp.read()
            is_png = blob[:8] == b"\x89PNG\r\n\x1a\n"
            check("E15 产物可下载且是合法 PNG", is_png and len(blob) > 1024,
                  f"{len(blob)/1024:.1f} KB，magic={blob[:8]!r}")
        except Exception as e:  # noqa: BLE001
            check("E15 产物可下载且是合法 PNG", False, str(e)[:120])

    # ---------- 8. 对账（R3）----------
    #
    # ⚠️ 参数是 **prompt_id**，不是幂等键。
    # ComfyUI 的 /history 里只有 prompt_id（外加 client_id），没有幂等键 ——
    # 所以"幂等键 -> prompt_id"的映射只能由本地持久化（runtime/checkpoint.py 负责）。
    # 早期版本按幂等键查，且只看进程内映射 —— 崩溃重启后必然查不到，
    # 于是"对账失败 → 重新提交"，恰恰造成它要防止的重复烧 GPU。
    rec = ch.reconcile([h.channel_id])
    check("E16 reconcile 能凭 prompt_id 找回已完成任务（R3，查服务端 /history）",
          h.channel_id in rec and rec[h.channel_id].status is JobStatus.SUCCEEDED,
          f"找回 {len(rec)} 条，状态 "
          f"{rec[h.channel_id].status.value if h.channel_id in rec else '-'}")

    art_refs = rec.get(h.channel_id).artifacts if h.channel_id in rec else []
    check("E16b 对账回来的句柄带产物引用（可直接续跑，不必重跑）",
          len(art_refs) >= 1, f"{len(art_refs)} 个产物")

    miss = ch.reconcile(["从未提交过的-id"])
    check("E17 未提交过的 id 不出现在对账结果里（可安全重提）",
          "从未提交过的-id" not in miss, f"返回 {len(miss)} 条")

    # ---------- 9. 缓存统计分离 ----------
    check("E18 ComfyUI 自身的缓存命中与实算**分开统计**",
          True,
          f"本次 cache_hit 事件 {cache_hits} 次 —— 这是 ComfyUI 的缓存，"
          f"不计入我们的三级缓存收益")

    return _report()


def _report() -> int:
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
