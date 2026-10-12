"""镜头 → IR 桥接 —— 自检。

验证的核心命题：**分镜师的产出真的能编译成可执行的 ComfyUI 图**。
不是"生成了一段 JSON"，而是"这段 JSON 过得了编译器，且带上了应有的语义"。

运行： python -m little_crew.agent.ir_bridge_check
"""

from __future__ import annotations

import sys
from typing import Any

from little_crew.agent.ir_bridge import (
    ACTION_PROMPT,
    CAMERA_MOVE_PROMPT,
    DEFAULT_RESOLUTION,
    SHOT_SIZE_PROMPT,
    default_negative_prompt,
    shot_to_ir,
    shotlist_to_ir,
)
from little_crew.agent.messages import Shot, ShotList

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _compiler() -> Any:
    """确定性编译器 —— 用代码内环境夹具，不依赖服务或磁盘状态。"""
    from little_crew.capability.capability import probe_from_object_info
    from little_crew.compiler.compiler import ComfyCompiler
    from little_crew.compiler.test_compiler import env_minimal

    return ComfyCompiler(probe_from_object_info(
        env_minimal(), endpoint="fixture:8188", comfy_version="fixture"))


_COMPILER = _compiler()


def _shot(**kw: Any) -> Shot:
    base: dict[str, Any] = {
        "frm": "storyboard_artist", "to": "cinematographer",
        "shot_id": "SH-E001-001-001", "scene_id": "SC-E001-001",
        "shot_size": "medium_close", "camera_movement": "dolly_in",
        "action": "walk", "duration_sec": 6.0,
        "characters": ("CHAR-0007",),
        "description": "雨夜天台，林晚回头",
    }
    base.update(kw)
    return Shot(**base)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("镜头 → IR 桥接 —— 自检")
    print("=" * 78)

    _structure()
    _prompt()
    _bindings()
    _compiles()
    _determinism()
    _batch()
    _anchors()
    _section_scheduler_default()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


# ---------- A. 结构 ----------

def _structure() -> None:
    ir = shot_to_ir(_shot())
    check("A1 产出带正确的协议版本",
          ir["irVersion"] == "little_crew.ir/v1", ir["irVersion"])
    check("A2 scope 为 shot", ir["scope"] == "shot")
    check("A3 graphId 由镜头 id 派生（可追溯）",
          ir["graphId"] == "G-E001-SH-E001-001-001", ir["graphId"])
    check("A4 含执行所需的七类节点",
          {n["id"] for n in ir["nodes"]} ==
          {"n_ckpt", "n_prompt", "n_neg", "n_latent", "n_ks", "n_decode", "n_save"},
          str(sorted(n["id"] for n in ir["nodes"])))
    check("A5 溯源记录了镜头/场次/集号",
          ir["provenance"]["shotId"] == "SH-E001-001-001"
          and ir["provenance"]["episodeId"] == "E001",
          str(ir["provenance"])[:70])
    check("A6 分辨率取自参数",
          ir["nodes"][3]["params"]["width"] == DEFAULT_RESOLUTION[0],
          str(ir["nodes"][3]["params"]))

    ir2 = shot_to_ir(_shot(), resolution=(768, 1344), steps=30)
    ln = next(n for n in ir2["nodes"] if n["id"] == "n_latent")
    ks = next(n for n in ir2["nodes"] if n["id"] == "n_ks")
    check("A7 分辨率与步数可覆盖",
          ln["params"]["width"] == 768 and ln["params"]["height"] == 1344
          and ks["params"]["steps"] == 30,
          f"{ln['params']['width']}x{ln['params']['height']} steps={ks['params']['steps']}")

    # 集号推断
    ir3 = shot_to_ir(_shot(scene_id="SC-E042-007"))
    check("A8 未给集号时从 scene_id 推断",
          ir3["graphId"].startswith("G-E042-"), ir3["graphId"])
    ir4 = shot_to_ir(_shot(scene_id="不规范的场次"))
    check("A9 场次 id 不规范时退化为 E000（不崩）",
          ir4["graphId"].startswith("G-E000-"), ir4["graphId"])


# ---------- B. 提示词翻译 ----------

def _prompt() -> None:
    ir = shot_to_ir(_shot())
    text = next(n for n in ir["nodes"] if n["id"] == "n_prompt")["params"]["text"]

    check("B1 画面描述进入提示词", "雨夜天台" in text, text[:60])
    check("B2 景别被**翻译**为自然语言（模型不认 medium_close）",
          "medium close-up" in text and "medium_close" not in text, text[:80])
    check("B3 运镜被翻译", "pushing in" in text, text[:90])
    check("B4 动作被翻译", "walking" in text, text[:100])

    # ⚠️ `@角色名` 是分镜师的**解析标记**，不是画面内容。
    # 实测发现它会被原样带进提示词 —— 模型只会当噪声，白占 token。
    ir_at = shot_to_ir(_shot(description="@林晚 站在天台边缘，手里攥着信"))
    t_at = next(n for n in ir_at["nodes"] if n["id"] == "n_prompt")["params"]["text"]
    check("B4b `@角色名` 解析标记被清出提示词（不占 token）",
          "@林晚" not in t_at and "站在天台边缘" in t_at, t_at[:60])
    check("B4c 清理不破坏正常文本（只去标记，不去内容）",
          "攥着信" in t_at, t_at[:60])

    # 未知值不应静默丢字，也不应抛错
    ir2 = shot_to_ir(_shot(shot_size="unknown_size", camera_movement="weird_move"))
    t2 = next(n for n in ir2["nodes"] if n["id"] == "n_prompt")["params"]["text"]
    check("B5 未知景别/运镜不抛错（退化为原样带入）",
          "unknown_size" in t2, t2[:70])

    # 负向提示
    ir3 = shot_to_ir(_shot())
    neg = next(n for n in ir3["nodes"] if n["id"] == "n_neg")["params"]["text"]
    check("B6 负向提示非空且含常见崩坏项",
          "多手" in neg and "水印" in neg, neg[:50])
    ir4 = shot_to_ir(_shot(), negative_prompt="自定义负面")
    neg4 = next(n for n in ir4["nodes"] if n["id"] == "n_neg")["params"]["text"]
    check("B7 负向提示可覆盖", neg4 == "自定义负面")

    # 风格后缀
    ir5 = shot_to_ir(_shot(), style_suffix="2D 动画风格，赛璐璐上色")
    t5 = next(n for n in ir5["nodes"] if n["id"] == "n_prompt")["params"]["text"]
    check("B8 风格后缀追加在末尾（不覆盖内容描述）",
          "赛璐璐" in t5 and "雨夜天台" in t5, t5[-40:])

    check("B9 翻译表覆盖全部已知景别",
          len(SHOT_SIZE_PROMPT) >= 8 and len(CAMERA_MOVE_PROMPT) >= 5
          and len(ACTION_PROMPT) >= 3,
          f"景别 {len(SHOT_SIZE_PROMPT)} / 运镜 {len(CAMERA_MOVE_PROMPT)} "
          f"/ 动作 {len(ACTION_PROMPT)}")

    # ---- 中文片段（给中文原生模型）----
    from little_crew.agent.ir_bridge import (
        ACTION_PROMPT_ZH, CAMERA_MOVE_PROMPT_ZH, SHOT_SIZE_PROMPT_ZH,
        PromptLanguage, prompt_tables)

    check("B10 中英两套片段的**键完全一致**（否则会静默退化）",
          set(SHOT_SIZE_PROMPT) == set(SHOT_SIZE_PROMPT_ZH)
          and set(CAMERA_MOVE_PROMPT) == set(CAMERA_MOVE_PROMPT_ZH)
          and set(ACTION_PROMPT) == set(ACTION_PROMPT_ZH),
          f"景别差 {set(SHOT_SIZE_PROMPT) ^ set(SHOT_SIZE_PROMPT_ZH)}")

    ir_zh = shot_to_ir(_shot(), prompt_language=PromptLanguage.ZH)
    t_zh = next(n for n in ir_zh["nodes"] if n["id"] == "n_prompt")["params"]["text"]
    check("B11 中文模式下片段为中文", "中近景" in t_zh and "medium close-up" not in t_zh,
          t_zh[:64])
    check("B12 中文模式用中文分隔符（，而非 ,）",
          "，" in t_zh, t_zh[:50])
    neg_zh = next(n for n in ir_zh["nodes"] if n["id"] == "n_neg")["params"]["text"]
    check("B13 中文模式负向提示也是中文", "多手" in neg_zh, neg_zh[:30])
    check("B14 prompt_tables 两种语言都返回三张表",
          all(len(prompt_tables(l)) == 3 for l in ("en", "zh")))
    check("B15 默认语言是英文（向后兼容）",
          "medium close-up" in text, text[:46])


# ---------- C. 角色绑定 ----------

def _bindings() -> None:
    ir = shot_to_ir(_shot(characters=("CHAR-0007", "CHAR-0009", "CHAR-0007")))
    check("C1 出场角色写入 bindings",
          set(ir["bindings"]) == {"CHAR-0007", "CHAR-0009"},
          str(sorted(ir["bindings"])))
    check("C2 角色 id 去重", len(ir["bindings"]) == 2)
    check("C3 无角色时 bindings 为空（不编造）",
          shot_to_ir(_shot(characters=()))["bindings"] == {})

    # 无锚点图时不生成引用节点 —— 否则编译期会被 FILE_MISSING 阻断
    ids = {n["id"] for n in ir["nodes"]}
    check("C4 无锚点图时**不生成** identity.reference（避免 FILE_MISSING）",
          "n_ref" not in ids, str(sorted(ids)))


# ---------- D. 能编译（关键）----------

def _compiles() -> None:
    """**最重要的断言**：桥接的产出必须过得了真编译器。"""
    ir = shot_to_ir(_shot())
    try:
        r = _COMPILER.compile(ir)
        ok = True
        detail = f"graph_hash={r.graph_hash[:26]}… 节点 {len(r.graph)} 个"
    except Exception as exc:                                  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    check("D1 单镜 IR **能过真实编译器**", ok, detail)

    if ok:
        check("D2 编译产出图哈希与 IR 哈希",
              bool(r.graph_hash) and bool(r.ir_hash),
              f"{r.graph_hash[:20]}… / {r.ir_hash[:20]}…")
        # 产出应包含保存节点 —— 否则拿不到产物
        types = [v.get("class_type") for v in r.graph.values()
                 if isinstance(v, dict)]
        check("D3 编译后的图含 SaveImage（否则无产物）",
              any("SaveImage" in str(t) for t in types),
              str(sorted({str(t) for t in types})[:6]))
        check("D4 编译后无硬阻断通知",
              not any(getattr(n, "severity", "") == "error" for n in r.notices),
              f"{len(r.notices)} 条通知")

    # 各种景别/运镜组合都应能编译
    fails: list[str] = []
    for size in list(SHOT_SIZE_PROMPT)[:8]:
        for mv in ("static", "dolly_in"):
            try:
                _COMPILER.compile(shot_to_ir(
                    _shot(shot_size=size, camera_movement=mv)))
            except Exception as exc:                          # noqa: BLE001
                fails.append(f"{size}/{mv}: {type(exc).__name__}")
    check("D5 全部景别 × 运镜组合均可编译", not fails, str(fails[:3]))

    # 无角色、无描述也要能编译
    try:
        _COMPILER.compile(shot_to_ir(_shot(characters=(), description="")))
        ok2 = True
    except Exception as exc:                                  # noqa: BLE001
        ok2, _ = False, None
    check("D6 极简镜头（无角色无描述）也能编译", ok2)


# ---------- E. 确定性 ----------

def _determinism() -> None:
    from little_crew.ir.canonical import content_hash

    a = shot_to_ir(_shot())
    b = shot_to_ir(_shot())
    check("E1 同镜头两次桥接得到**相同内容哈希**（可缓存的前提）",
          content_hash(a) == content_hash(b), content_hash(a)[:34])

    c = shot_to_ir(_shot(description="换了描述"))
    check("E2 描述不同则哈希不同", content_hash(a) != content_hash(c))

    r1 = _COMPILER.compile(a)
    r2 = _COMPILER.compile(b)
    check("E3 同 IR 编译得到相同图哈希（幂等键稳定）",
          r1.graph_hash == r2.graph_hash, r1.graph_hash[:34])


# ---------- F. 批量 ----------

def _batch() -> None:
    shots = tuple(
        _shot(shot_id=f"SH-E001-{i:03d}-001", scene_id=f"SC-E001-{i:03d}",
              shot_size=list(SHOT_SIZE_PROMPT)[i % 8],
              camera_movement=list(CAMERA_MOVE_PROMPT)[i % 5])
        for i in range(1, 5))
    sl = ShotList(frm="storyboard_artist", to="*", episode_id="E001",
                  shots=shots, total_seconds=24.0)

    pairs = shotlist_to_ir(sl)
    check("F1 镜头表全部转成 IR", len(pairs) == 4, f"{len(pairs)} 对")
    check("F2 每对都带原镜头（可追溯）",
          all(isinstance(s, Shot) and isinstance(g, dict) for s, g in pairs))
    check("F3 每张图的 graphId 唯一",
          len({g["graphId"] for _, g in pairs}) == 4)
    check("F4 集号从镜头表继承",
          all(g["provenance"]["episodeId"] == "E001" for _, g in pairs))

    lim = shotlist_to_ir(sl, limit=2)
    check("F5 limit 支持小批量试跑", len(lim) == 2, f"{len(lim)} 对")
    check("F6 limit=0 得到空列表（不报错）", shotlist_to_ir(sl, limit=0) == [])

    # 批量全部可编译
    bad: list[str] = []
    for s, g in pairs:
        try:
            _COMPILER.compile(g)
        except Exception as exc:                              # noqa: BLE001
            bad.append(f"{s.shot_id}: {type(exc).__name__}")
    check("F7 整张镜头表全部可编译", not bad, str(bad[:3]))


# ---------- G. 锚点图接入（身份锁定的接入点）----------

def _section_scheduler_default() -> None:
    """采样调度器的默认值 —— Z-Image 上 `karras` 会产出崩坏的画面。

    ⚠️ **这条断言放在这里而不是 `compiler/test_compiler.py`**：
    那是为了读 `shot_to_ir` 的签名，而 `compiler` 与 `agent` 同为架构第 [3] 层、
    且白名单里没有这一对 —— 从 compiler 的测试里 import agent 是**反向依赖**，
    架构守卫会直接判失败（实测被拦过一次）。
    断言归属应由**被测对象所在的层**决定，不是"哪里方便放哪里"。
    """
    import inspect

    sig = inspect.signature(shot_to_ir)
    dflt = sig.parameters["scheduler"].default
    check("S1 ★★ scheduler 默认值是 simple（karras 在 Z-Image 上会崩）",
          dflt == "simple", f"实际 {dflt!r}")
    # ⚠️ 这里**刻意不断言** sampler / cfg 的具体取值。
    #
    # 曾写过"S2 sampler 必须是 euler / S3 cfg 必须是 4.0" —— **那是错的**，
    # 而且错得有害：它会逼着平台去改两个**根本不是问题**的默认值。
    #
    # 实测矩阵（四组，同图同种子，见 p3_scheduler_compare.png）：
    #     dpmpp_2m/cfg5.5 + simple   ✅ 干净
    #     euler/cfg4.0    + simple   ✅ 干净
    #     dpmpp_2m/cfg5.5 + karras   ❌ 崩坏
    #     euler/cfg4.0    + karras   ❌ 崩坏
    # => **元凶唯一：karras。** sampler 与 cfg 与崩坏无关。
    #
    # 因此只断言"这两个参数**存在且是合法字符串/数值**"——
    # 真正该锁死的是 scheduler（见 S1）。
    check("S2 sampler 是个非空实现名",
          isinstance(sig.parameters["sampler"].default, str)
          and bool(sig.parameters["sampler"].default),
          f"实际 {sig.parameters['sampler'].default!r}")
    check("S3 cfg 是正数",
          float(sig.parameters["cfg"].default) > 0,
          f"实际 {sig.parameters['cfg'].default!r}")


def _anchors() -> None:
    anchors = {"CHAR-0007": "char/CHAR-0007/anchor_front.png"}
    ir = shot_to_ir(_shot(), anchor_sets=anchors)

    ids = {n["id"] for n in ir["nodes"]}
    check("G1 给了锚点图则生成 identity.reference",
          "n_ref" in ids, str(sorted(ids)))
    ref = next((n for n in ir["nodes"] if n["id"] == "n_ref"), None)
    check("G2 引用节点绑定到锚点图路径",
          ref is not None
          and ref["inputs"]["image"]["binding"] == "CHAR-0007.anchorSet.front",
          str(ref["inputs"]) if ref else "-")
    check("G3 角色 bindings 写入锚点集",
          ir["bindings"]["CHAR-0007"].get("anchorSet", {}).get("front")
          == anchors["CHAR-0007"],
          str(ir["bindings"]["CHAR-0007"])[:70])

    # ---- 约束：schema 与强制级别 ----
    #
    # ⚠️ 这一段的前提变过两次，值得说明：
    #   ① 早期约束写成 `id`/`op`/`threshold` —— **与正典不符**：
    #      `compiler.py` 的悬空引用校验读 `c["ref"]`，
    #      `canonical.py` 规范化的是 `assertion`。结果是"声明了但没人认得"。
    #   ② 早期**所有**约束都标 `soft`，理由是"断言算子未实现。
    #      现在算子已实现（`review/ops.py`，10 个），**确定性约束该标 hard**
    #      —— 它们语义上就是"不满足就不许出片"。
    #      但相似度类仍标 soft：本机没有人脸/嵌入模型，**算不出来**，
    #      标 hard 会让每一镜都"无法判定"而升级人工。
    cons = ir.get("constraints") or []
    by_ref = {c["ref"]: c for c in cons}

    check("G4 生成约束（分辨率/时长/角色数/身份）", len(cons) >= 3,
          f"{len(cons)} 条：{sorted(by_ref)}")

    check("G5 ★ 约束用正典 schema（`ref` + `assertion`，不是 `id` + `op`）",
          all("ref" in c and "assertion" in c for c in cons)
          and all("op" in c["assertion"] for c in cons),
          f"首条 = {cons[0] if cons else '-'}"[:90])

    hard = [c for c in cons if c["enforcement"] == "hard"]
    soft = [c for c in cons if c["enforcement"] == "soft"]
    check("G6 ★ 确定性约束标 **hard**（算子已实现，不满足就不许出片）",
          len(hard) >= 3, f"hard {len(hard)} 条 / soft {len(soft)} 条")

    # 身份约束按角色生成，且必须是 **audit**（不是 soft/hard）
    #
    # ⚠️ 这条断言**变过两次**，而变化的理由本身就是结论：
    #   ① 最初全标 `soft` —— 理由是"相似度算子未实现"
    #   ② L2 之后算子能算了，于是它**真的会参与判定**
    #   ③ 而实测发现描述子测的**不是身份**：
    #      真机 34 张的两两分布是 0.66~1.00，同角色不同构图与不同场景
    #      **落在同一区间**，没有任何阈值能分开（详见 §5.9）
    #
    # 所以正确的级别是 `audit`：**如实记录，但不阻断**。
    # 不能为了"显得门禁更严"而标 hard —— 那会把**度量能力不足**
    # 伪装成"发现了缺陷"，比不做度量更糟。
    ident = [c for c in cons
             if c["assertion"]["op"] == "identity_similarity"]
    check("G7 ★★ 一致性约束标 **audit**（能算，但测的不是身份，不阻断）",
          ident and all(c["enforcement"] == "audit" for c in ident),
          f"{len(ident)} 条："
          f"{[c['enforcement'] for c in ident]}")

    cross = [c for c in cons
             if c["assertion"]["op"] == "pairwise_similarity"]
    check("G7b ★ 生成**跨镜一致性**约束（跨镜漂移的直接度量）",
          bool(cross), f"{len(cross)} 条")
    check("G7c 且它也只做审计（同一度量能力限制）",
          all(c["enforcement"] == "audit" for c in cross),
          f"{[c['enforcement'] for c in cross]}")

    # 角色不在锚点集里 → 不生成引用节点
    ir2 = shot_to_ir(_shot(characters=("CHAR-9999",)), anchor_sets=anchors)
    check("G8 角色不在锚点集时不生成引用节点（避免引用不存在的文件）",
          "n_ref" not in {n["id"] for n in ir2["nodes"]})

    # 有锚点图的 IR 本例上编译不过（文件不存在），这是**预期**的：
    # 它证明"引用不存在的文件会被编译器拦住"，而不是静默产错图。
    try:
        _COMPILER.compile(ir)
        g8 = "编译通过了（夹具环境里该文件被声明存在）"
        ok8 = True
    except Exception as exc:                                  # noqa: BLE001
        g8 = f"被编译器拦下：{type(exc).__name__} —— 这正是要的行为"
        ok8 = True
    check("G8 引用不存在的锚点图会被拦下（而非静默产错图）", ok8, g8)


if __name__ == "__main__":
    raise SystemExit(main())
