"""设定板质检：让 **VLM 拿质检表逐条判图**（2026-11-13，T4）。

## 为什么要有它

`character_sheet_prompt.QC_CHECKLIST` 有 12 条硬指标（四视图齐全、不裁切、
同一张脸、无文字水印、解剖可信、面部细节达标……），但此前它只是**写进
`sheet.json` 给人看** —— 没人真的逐条看，等于没有质检。

本模块把它变成**自动核验**：一次 VLM 调用，输出逐条的 `pass / fail / unclear`。

## 三条纪律（都是踩过的坑）

1 **失败不静默**：模型不给合法 JSON、缺条目、条目 id 对不上 → 该条记 `unclear`，
   **整体结论是"未通过/未核验"，绝不是通过**。视觉闸的"结构不对就满分"事故就是反面教材。
2 **不评"好不好看"**：只判**可数项**（在不在、裁没裁、是不是同一个人、有没有文字）。
   美丑/风格偏好属人工。
3 **`unclear` 不是通过**：模型说"看不清"时，该项既不算过也不算不过 —— 但**结论不得为 pass**。

## 定位：**挑错器，不是放行器**（2026-11-13 校准得出）

3 次重复实测：

| 图 | 3 次结论 |
|---|---|
| 带 AI 水印 / 只有三视图 | revise / revise / revise（每次都指同一处）✅ |
| 已通过的设定板 | pass / unclear 混着来 ⚠️ |
| 娃娃脸（人工判定不行） | pass / pass / unclear（从未 fail）❌ |

所以：
- **fail 可信**：说有问题就是有问题
- **pass 不可当合格**：只代表"没看出硬缺陷"
- 审美/比例判不了（与"VLM 判不了身份一致性"同源）

### 重复取并集（`attempts=2`）

同一次调用会抖，所以**默认跑 2 次**：
任一次 fail -> fail（提高召回）；全部 pass 才记 `no_defect`；其余 `unclear`。
成本 ×2（mimo 单次约 3~5 秒），换来"抓错不漏"。

## 输出格式（稳定，便于进报告）

```
{
  "kind": "sheet.qc",
  "verdict": "revise" | "no_defect" | "unclear",
  "claim": "未发现硬缺陷（不等于合格）"  # 仅 no_defect 时有意义
  "attempts": 2,
  "reviewed": 12, "passed": 9, "failed": 2, "unclear": 1,
  "items": [{"id": "four_views", "name": "四个视图都在", "verdict": "pass",
             "reason": "..."}],
  "notes": [...]
}
```
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 质检项（id, 中文名）。**id 稳定**：报告与测试都按 id 对齐。
QC_ITEMS: tuple[tuple[str, str], ...] = (
    ("four_views", "四个视图都在（左特写 + 右正/侧/背三视图），无人被裁切"),
    ("layout", "左特写占比约四成半，右侧三视图等高、脚底齐平"),
    ("orientation", "侧面是真侧面、背面是真背面（朝向正确）"),
    ("same_face", "四个视图是同一张脸（脸型五官一致）"),
    ("same_hair", "四个视图是同一套发型"),
    ("same_outfit", "四个视图服装结构一致（领口/腰线/裤裙结构/配饰）"),
    ("same_shoes", "四个视图鞋型与鞋底厚度一致"),
    ("no_text", "画面无文字、无水印、无信息栏残留"),
    ("background", "背景干净、无杂物、接触阴影自然"),
    ("anatomy", "解剖可信（手指数量、肩颈连接、脚踝方向）"),
    ("face_detail", "面部细节达标（眼睛有神、睫毛与卧蚕清晰、妆面干净）"),
    ("proportion", "三庭比例协调、脸不过长、眼位不过高"),
)

_PROMPT = """看图给角色设定板做质检。**只输出一行 JSON，不要任何推理文字**：

{"four_views":"pass|fail|unclear", "layout":"...", "orientation":"...",
 "same_face":"...", "same_hair":"...", "same_outfit":"...", "same_shoes":"...",
 "no_text":"...", "background":"...", "anatomy":"...", "face_detail":"...",
 "proportion":"..."}

规则：
- pass=图中明确成立；fail=明确不成立；unclear=看不清/无法确认
- **宁可 unclear 也不要凭印象判 pass**
- 每个键都必须出现，值只能是 pass/fail/unclear
- 只对 fail 的项，另加一个 "reasons":{"<键>":"20字内原因"}（没有 fail 就不加）"""
#: 推理模型会把额度花在思考上，输出格式改成"扁平 12 键"后 2000 足够
#: 判官模型：**mimo-v2.6-flash**（2026-11-13 实测）
#: ① 非推理模型 —— 直接吐 JSON；deepseek-flash 是推理模型，1400 token 全花在思考上、
#:    JSON 根本到不了（3000 token 才开始吐且会截断）
#: ② 跨模型族 —— 生成走 OpenAI 系（Sudorelay），判官走小米系，不自签收
DEFAULT_JUDGE = "mimo-v2.6-flash"

#: 输出很短（12 个键），900 足够；推理模型才需要几千
MAX_TOKENS = 900


def _extract_obj(text: str) -> dict | None:
    """从回复里抠出第一个 JSON 对象（模型常带 markdown 围栏）。"""
    if not isinstance(text, str):
        return None
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(text[i:j + 1])
    except Exception:                                         # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def _ask(client: Any, image_path: Path, context: str = "") -> tuple[dict | None, str]:
    """一次视觉调用 -> (解析后的对象, 错误说明)。"""
    try:
        data = base64.b64encode(image_path.read_bytes()).decode()
    except Exception as exc:                                  # noqa: BLE001
        return None, f"读图失败:{type(exc).__name__}"
    prompt = _PROMPT + f"\n\n画面说明：{context or '角色四视图设定板'}"
    try:
        reply = client.chat_image(prompt, data, what="设定板质检",
                                  max_tokens=MAX_TOKENS)
    except Exception as exc:                                  # noqa: BLE001
        return None, f"调用失败:{type(exc).__name__}: {exc}"
    text = reply if isinstance(reply, str) else getattr(reply, "text", "")
    obj = _extract_obj(text)
    if obj is None:
        return None, "模型没给合法 JSON"
    return obj, ""


def judge(image_path: str | Path, *, client: Any = None,
          context: str = "", items: Iterable[tuple[str, str]] = QC_ITEMS,
          attempts: int = 2) -> dict:
    """对一张设定板做质检。**永不抛**；结构不合法一律记 unclear。

    `attempts` 次重复调用后**取 fail 并集**（模型会抖，单次会漏）。
    """
    items = tuple(items)
    attempts = max(1, int(attempts))
    path = Path(str(image_path))
    out: dict[str, Any] = {
        "kind": "sheet.qc", "file": str(path), "verdict": "unclear",
        "reviewed": 0, "passed": 0, "failed": 0, "unclear": 0,
        "items": [], "notes": [],
    }
    if client is None:
        try:
            from little_crew.agent.llm import LlmClient

            # 判官用 **mimo-v2.6-flash**（2026-11-13 实测）：
            # ① 非推理模型 —— 直接吐 JSON（deepseek-flash 是推理模型，3000 token 才吐且会截断）
            # ② 跨模型族 —— 生成走 OpenAI 系（Sudorelay），判官走小米系，不自签收
            client = LlmClient(DEFAULT_JUDGE, timeout_s=60)
        except Exception as exc:                              # noqa: BLE001
            out["notes"].append(f"没有可用的视觉客户端：{exc}")
            for k, name in items:
                out["items"].append({"id": k, "name": name,
                                     "verdict": "unclear", "reason": "未核验"})
            out["unclear"] = len(items)
            return out
    if not path.is_file():
        out["notes"].append(f"图片不存在：{path}")
        for k, name in items:
            out["items"].append({"id": k, "name": name, "verdict": "unclear",
                                 "reason": "图片不存在"})
        out["unclear"] = len(items)
        return out

    # 重复 attempts 次，逐项**取 fail 并集**（模型会抖；单次会漏）
    merged: dict[str, dict] = {}
    attempt_results: list[str] = []
    for _ in range(attempts):
        got, err = _parse_once(client, path, context, items)
        if err:
            out["notes"].append(err)
        for k, _name in items:
            hit = got.get(k)
            if hit is None:
                cur = merged.setdefault(k, {"verdict": "unclear", "reason": "模型未覆盖该项"})
                attempt_results.append("uncovered")
                continue
            cur = merged.setdefault(k, dict(hit))
            # fail 并集：任一次 fail 即 fail
            if hit["verdict"] == "fail":
                cur["verdict"] = "fail"
                if hit.get("reason"):
                    cur["reason"] = hit["reason"]
            elif cur["verdict"] != "fail":
                # unclear 也并集（任一次不确定就不算 pass）
                if hit["verdict"] == "unclear" or cur["verdict"] == "unclear":
                    cur["verdict"] = "unclear"

    for k, name in items:
        out["items"].append({"id": k, "name": name, **merged.get(k, {"verdict": "unclear",
                                                                     "reason": "未核验"})})
    out["attempts"] = attempts

    out["reviewed"] = len(items)
    out["passed"] = sum(1 for i in out["items"] if i["verdict"] == "pass")
    out["failed"] = sum(1 for i in out["items"] if i["verdict"] == "fail")
    out["unclear"] = sum(1 for i in out["items"] if i["verdict"] == "unclear")
    # 结论：fail 可信；pass 只记"未发现硬缺陷"（**当不得合格**）
    if out["failed"]:
        out["verdict"] = "revise"
        out["claim"] = "发现硬缺陷，需修"
    elif out["unclear"] == 0 and out["passed"] == len(items):
        out["verdict"] = "no_defect"
        out["claim"] = "未发现硬缺陷（不等于合格；审美/比例未判定）"
    else:
        out["verdict"] = "unclear"
        out["claim"] = "未核验（模型未确认，不能当作通过）"
    return out


def _parse_once(client: Any, path: Path, context: str,
                items: tuple[tuple[str, str], ...]) -> tuple[dict[str, dict], str]:
    """单次调用 + 解析（扁平键或 items 数组都认）。返回 (逐项结果, 错误说明)。"""
    obj, err = _ask(client, path, context)
    got: dict[str, dict] = {}
    reasons: dict[str, str] = {}
    if obj is not None:
        if isinstance(obj.get("reasons"), Mapping):
            reasons = {str(k): str(v)[:40] for k, v in obj["reasons"].items()}
        for k, _name in items:
            v = obj.get(k)
            if isinstance(v, str) and v.strip().lower() in ("pass", "fail", "unclear"):
                got[k] = {"verdict": v.strip().lower(), "reason": reasons.get(k, "")}
        raw_items = obj.get("items")
        if not got and isinstance(raw_items, list):
            for it in raw_items:
                if not isinstance(it, Mapping):
                    continue
                k = str(it.get("id") or "").strip()
                v = str(it.get("verdict") or "").strip().lower()
                if v not in ("pass", "fail", "unclear"):
                    v = "unclear"
                got[k] = {"verdict": v, "reason": str(it.get("reason") or "")[:40]}
        if not got:
            err = err or "模型没给出可识别的判定（键名/取值都不对）"
    return got, err


def summarize(report: Mapping[str, Any], *, limit: int = 4) -> str:
    """一行摘要（给终端/报告用）。"""
    bad = [i for i in (report.get("items") or ()) if i.get("verdict") == "fail"]
    unc = [i for i in (report.get("items") or ()) if i.get("verdict") == "unclear"]
    verdict = report.get("verdict")
    label = {"revise": "发现硬缺陷", "no_defect": "未发现硬缺陷（≠合格）",
             "unclear": "未核验"}.get(str(verdict), str(verdict))
    parts = [label, f"通过 {report.get('passed')}/{report.get('reviewed')}"]
    if bad:
        parts.append("不通过：" + "、".join(i["name"] for i in bad[:limit]))
    if unc:
        parts.append(f"未确认 {len(unc)} 项")
    return " · ".join(parts)
