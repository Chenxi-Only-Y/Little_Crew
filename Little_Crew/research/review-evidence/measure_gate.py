"""M5 拦截率实测 —— 用**真实产物**验证门禁。

里程碑 M5 的判定标准是「盲读门禁**拦截率**有实测数据」。
本脚本分开测两个容易混淆的量：

| 指标 | 定义 | 期望 |
|---|---|---|
| **拦截率**（召回） | 注入的真实缺陷里，门禁拦下了多少 | 越高越好 |
| **误杀率**（1-精确率） | 合格产物里，门禁误判为不合格的比例 | 越低越好 |

**只用确定性算子**（分辨率/大小/时长/文本）——
相似度类需要人脸模型或嵌入模型，本机没有，
**用假相似度去报"拦截率"等于编数据**（见 review/ops.py 的算子契约）。

产物来自 `outputs/episode_E100/`（真实跑出来的一集）与
`research/blender-previs/p3-evidence/`（真实对照图）。

用完即删。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from little_crew.review import Gate, GateVerdict, Observation

ROOT = Path(r"D:\AI\ds.8\Little_Crew")
OUT = ROOT / "outputs"
EV = ROOT / "research" / "blender-previs" / "p3-evidence"


def png_size(p: Path) -> tuple[int, int] | None:
    """从 PNG 头部直接读尺寸（13 字节处开始是 IHDR）。"""
    try:
        b = p.read_bytes()[:33]
        if len(b) < 24 or b[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        w = int.from_bytes(b[16:20], "big")
        h = int.from_bytes(b[20:24], "big")
        return w, h
    except Exception:  # noqa: BLE001
        return None


gate = Gate()

# ---------------------------------------------------------------- 真产物清单
real_pngs: list[Path] = []
for d in (OUT / "episode_E100", EV):
    if d.is_dir():
        real_pngs += sorted(p for p in d.rglob("*.png") if p.is_file())

print("=" * 78)
print("M5 拦截率实测 —— 确定性算子，真实产物")
print("=" * 78)
print(f"  真实产物 {len(real_pngs)} 个:")
for p in real_pngs[:8]:
    sz = png_size(p)
    print(f"    {p.name:34} {sz if sz else '?'}  {p.stat().st_size:>9,} B")
if len(real_pngs) > 8:
    print(f"    … 另 {len(real_pngs) - 8} 个")

# 取一个真图当基准
base = next((p for p in real_pngs if png_size(p)), None)
if base is None:
    print("\n  ❌ 找不到可用 PNG 基准")
    raise SystemExit(1)
bw, bh = png_size(base)
print(f"\n  基准图 {base.name}  {bw}x{bh}  {base.stat().st_size:,} B")
print(f"  （基准由调用方声明；规格不符的产物**不算合格组样本**）")

RES_CONSTRAINT = [
    {"ref": "cr_res", "enforcement": "hard",
     "assertion": {"op": "resolution_matches", "width": bw, "height": bh}},
]
SIZE_CONSTRAINT = [
    {"ref": "cr_sz", "enforcement": "hard",
     "assertion": {"op": "byte_size_within", "minBytes": 10_000}},
]

# ---------------------------------------------------------------- 合格组
# ⚠️ **基准必须先声明，再筛产物。**
#
# 我第一版把分辨率不同的对照拼图（1050x230 等）也当成"合格产物"，
# 再喂给一条要求 832x480 的约束 —— 门禁拦它**完全正确**，
# 而我把它记成了误杀，报出 50% 的假误杀率。
#
# **门禁的职责是"按声明的规格判定"，它不该猜哪张图算合格。**
# 基准由调用方给，给错了是调用方的错。
#
# 因此：只把**规格与基准一致**的产物纳入合格组。
ok_cases: list[tuple[str, list, Observation]] = []
skipped: list[str] = []
for p in real_pngs:
    sz = png_size(p)
    if not sz:
        skipped.append(f"{p.name}（非 PNG）")
        continue
    if (sz[0], sz[1]) != (bw, bh):
        skipped.append(f"{p.name}（{sz[0]}x{sz[1]} != 基准 {bw}x{bh}）")
        continue
    if p.stat().st_size < 10_000:
        skipped.append(f"{p.name}（{p.stat().st_size} B < 10 KB 下限）")
        continue
    ok_cases.append((
        f"合格:{p.name[:34]}",
        RES_CONSTRAINT + SIZE_CONSTRAINT,
        Observation(artifact_id=p.name,
                    values={"width": sz[0], "height": sz[1],
                            "bytes": p.stat().st_size},
                    source="png-header+stat"),
    ))

# ---------------------------------------------------------------- 缺陷组
# 真实产物 + **刻意注入的偏差** -> 门禁**应该**拦截
bad_cases: list[tuple[str, list, Observation]] = []

# ① 分辨率不符（最常见的真实故障：编译出的 latent 尺寸错）
bad_cases.append((
    "缺陷:分辨率错 1 像素",
    RES_CONSTRAINT,
    Observation(artifact_id="bad-res",
                values={"width": bw + 1, "height": bh}, source="injected")))

bad_cases.append((
    "缺陷:分辨率换档 832x480->512x512",
    RES_CONSTRAINT,
    Observation(artifact_id="bad-res2",
                values={"width": 512, "height": 512}, source="injected")))

# ② 文件过小（编码失败的真实特征）
bad_cases.append((
    "缺陷:文件仅 512 字节（编码失败）",
    SIZE_CONSTRAINT,
    Observation(artifact_id="bad-size",
                values={"bytes": 512}, source="injected")))

# ③ 零字节
bad_cases.append((
    "缺陷:0 字节文件",
    SIZE_CONSTRAINT,
    Observation(artifact_id="bad-zero", values={"bytes": 0},
                source="injected")))

# ④ 时长超限（真实的 max_shot_seconds=10 约束）
DUR = [{"ref": "cr_dur", "enforcement": "hard",
        "assertion": {"op": "duration_within", "max": 10.0}}]
bad_cases.append((
    "缺陷:时长 12.0s > 上限 10s",
    DUR,
    Observation(artifact_id="bad-dur", values={"durationSec": 12.0},
                source="injected")))

# ⑤ 文本与剧本不符（台词被改写）
TOK = [{"ref": "cr_tok", "enforcement": "hard",
        "assertion": {"op": "token_diff", "maxRatio": 0.15}}]
bad_cases.append((
    "缺陷:产出文本与剧本差异 87%",
    TOK,
    Observation(artifact_id="bad-text",
                values={"script": "独自走在山坡上采花，竹篮里已有半篮紫色花穗",
                        "produced": "totally different content here"},
                source="injected")))

# ⑥ 缺观测（未测 -> 必须拦，不能当通过）
bad_cases.append((
    "缺陷:观测值缺失（未测）",
    RES_CONSTRAINT,
    Observation(artifact_id="bad-missing", values={}, source="none")))

# ⑦ 约束写错（未知算子）—— 这是"判定不可信"，应 ESCALATE 而非 RETRY
ESC = [{"ref": "cr_bad_op", "enforcement": "hard",
        "assertion": {"op": "no_such_op_v9"}}]
bad_cases.append((
    "缺陷:约束引用了未知算子",
    ESC,
    Observation(artifact_id="bad-op", values={}, source="injected")))


# ---------------------------------------------------------------- 跑
def run(cases, *, label: str, expect: str) -> tuple[int, int, list[str]]:
    hit = 0
    detail: list[str] = []
    for name, cons, obs in cases:
        r = gate.evaluate(cons, obs)
        if expect == "keep":
            good = r.verdict is GateVerdict.KEEP
        else:
            good = r.verdict is not GateVerdict.KEEP
        hit += int(good)
        mark = "✓" if good else "✗"
        detail.append(f"    {mark} {name:38} -> {r.verdict.value}")
    return hit, len(cases), detail


if skipped:
    print(f"\n  因规格不符被排除 {len(skipped)} 个（不是误杀，是基准不同）:")
    for s in skipped[:8]:
        print(f"    - {s}")

print()
print("── ① 合格组：门禁**不应**拦截（测误杀率）──")
ok_hit, ok_n, ok_detail = run(ok_cases, label="ok", expect="keep")
for d in ok_detail[:14]:
    print(d)
if len(ok_detail) > 14:
    print(f"    … 另 {len(ok_detail) - 14} 条")

print()
print("── ② 缺陷组：门禁**应该**拦截（测拦截率）──")
bad_hit, bad_n, bad_detail = run(bad_cases, label="bad", expect="block")
for d in bad_detail:
    print(d)

print()
print("=" * 78)
print("结果")
print("=" * 78)
fp = ok_n - ok_hit
recall = bad_hit / bad_n if bad_n else 0.0
precision = bad_hit / (bad_hit + fp) if (bad_hit + fp) else 1.0
print(f"  合格产物 {ok_n} 个：正确放行 {ok_hit}，**误杀 {fp}**"
      f"  -> 误杀率 {fp / ok_n:.1%}" if ok_n else "")
print(f"  注入缺陷 {bad_n} 个：拦下 {bad_hit}，漏放 {bad_n - bad_hit}"
      f"  -> **拦截率 {recall:.1%}**")
print(f"  精确率 {precision:.1%}")

report = {
    "okTotal": ok_n, "okPassed": ok_hit, "falsePositive": fp,
    "badTotal": bad_n, "badBlocked": bad_hit,
    "recall": round(recall, 4), "falsePositiveRate":
        round(fp / ok_n, 4) if ok_n else 0.0,
    "precision": round(precision, 4),
    "operatorSet": "deterministic-only (similarity ops excluded: no local model)",
}
(ROOT / "outputs" / "m5_gate_report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n  报告已写 outputs/m5_gate_report.json")
