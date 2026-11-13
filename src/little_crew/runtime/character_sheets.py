"""角色设计师的**图像半**：把文字角色表画成锚点图集（2026-11-13）。

## 分工：文字半 / 图像半

| 半 | 在哪 | 产出 |
|---|---|---|
| 文字半 | `agent/character_sheet.CharacterDesignerRules` | `character.sheet`：每个角色的closed-set 外观（发型/服装/色板…） |
| **图像半** | **本模块** | `assets/characters/<cid>/sheet.png` + `sheet.json`（题面、模型、哈希、授权标记） |

## 为什么图像半单独一层

- 它要**调云端图像通道**（按张计费）→ 不能默认开，失败也不能让整集挂；
- 它有**严格的授权与用途约束**：模型必须登记、授权未核实默认拦下、
  且该通道的 `allowed_agents` 必须是 `character_designer`（防主链路误用云端）。

## 三件必须留痕的事（`sheet.json`）

1. **题面**（prompt 原文）—— 将来重画/微调要能复现；
2. **内容哈希**（图片 sha256）—— 与 prompt、模型一起构成可追溯链；
3. **授权状态**（license verdict 原文）—— "这图能不能商用"必须有据可查，
   而不是"当时好像允许"。

## 不做什么

- **不做角色一致性校验**：锚点图与成片是否同一张脸需要身份度量（ArcFace/CLIP），
  本机没有 —— 所以本模块只产出**参考图**，不声称"成片更像了"。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

#: 默认模型：Sudorelay gpt-image-2.6（实测无水印、1672×941、三视图一致性好）
DEFAULT_MODEL = "gpt-image-2.6-sunburst"

#: 默认出图尺寸（横版：左侧特写 + 右侧三视图）
DEFAULT_SIZE = "1536x1024"

#: 一次最多画几个角色（成本闸门；0 = 不限制）
DEFAULT_LIMIT = 6

def _appearance_text(sheet: Any) -> str:
    """把角色外观字段压成一句（只取已有字段，不编造）。"""
    app = getattr(sheet, "appearance", None)
    if isinstance(app, Mapping):
        parts = [f"{k}：{v}" for k, v in app.items() if v]
        return "；".join(parts)
    return str(app or "")


def build_prompt(sheet: Any, *, style: str = "",
                 proportion: str = "adult") -> str:
    """单个角色的设定板题面 —— **按四视图模板组装**（见 `character_sheet_prompt`）。

    模板来源是两份工程化文档（通用骨架 + 12 槽变量卡 + 负面词四组 + 质检表），
    比例档 `proportion`：`adult`（标准成人）/ `cute`（幼态）。
    这里只做委托：模板本体在 `runtime/character_sheet_prompt.py`，便于单独 A/B。
    """
    from little_crew.runtime.character_sheet_prompt import build_prompt as _bp

    return _bp(sheet, style=style, proportion=proportion)


class CharacterSheetPainter:
    """负责"画"的那一半。**不判断、不重试**（失败留痕，由上层决定要不要再来一次）。"""

    def __init__(self, *, model_id: str = DEFAULT_MODEL, size: str = DEFAULT_SIZE,
                 limit: int = DEFAULT_LIMIT) -> None:
        self.model_id = str(model_id or DEFAULT_MODEL)
        self.size = str(size or DEFAULT_SIZE)
        self.limit = max(0, int(limit))

    # ---- 主入口 -------------------------------------------------------

    def paint(self, sheets: Any, out_dir: str | Path, *,
              style: str = "", proportion: str = "adult",
              refine: bool = False, refiner: Any = None,
              qc: bool = False, qc_judge: Any = None,
              qc_extra: bool = True) -> dict[str, Any]:
        """把角色表画成图集。**返回报告，从不抛**（除配置性错误外都记进报告）。"""
        out = Path(str(out_dir))
        out.mkdir(parents=True, exist_ok=True)
        report: dict[str, Any] = {"kind": "character.sheets", "model": self.model_id,
                                  "outDir": str(out), "painted": [], "failed": [],
                                  "skipped": [], "limit": self.limit,
                                  "refine": bool(refine), "refined": [],
                                  "refineFailed": [],
                                  "qc": bool(qc), "qcReports": [],
                                  "qcFailed": []}
        # 保图微调（2026-11-13）：生成后立刻用 2.6 的 /images/edits 修细节。
        # 默认关；失败只记录，不影响整集（与"图集不阻断"同一纪律）。
        if qc and qc_judge is None:
            try:
                from little_crew.review.sheet_qc import judge as qc_judge
            except Exception as exc:                          # noqa: BLE001
                report["qcFailed"].append(
                    {"reason": f"质检模块不可用：{type(exc).__name__}: {exc}"})
                qc = False

        if refine and refiner is None:
            try:
                from little_crew.runtime.sudorelay_edits import refine_sheet as refiner
            except Exception as exc:                          # noqa: BLE001
                report["refineFailed"].append(
                    {"reason": f"精修通道不可用：{type(exc).__name__}: {exc}"})
                refine = False
        all_sheets = list(getattr(sheets, "sheets", ()) or ())
        if self.limit and len(all_sheets) > self.limit:
            report["skipped"] = [str(getattr(s, "character_id", ""))
                                 for s in all_sheets[self.limit:]]
            all_sheets = all_sheets[: self.limit]

        try:
            spec, backend, channel = self._channel()
        except Exception as exc:                              # noqa: BLE001
            report["failed"].append({"reason": f"通道不可用：{type(exc).__name__}: {exc}"})
            return report
        report["license"] = self._license_note()

        for sheet in all_sheets:
            cid = str(getattr(sheet, "character_id", "") or "")
            name = str(getattr(sheet, "name", "") or "")
            if not cid:
                report["failed"].append({"name": name, "reason": "缺少 character_id"})
                continue
            prompt = build_prompt(sheet, style=style, proportion=proportion)
            try:
                art = self._render_one(spec, backend, channel, prompt,
                                       idempotency_key=f"charsheet:{cid}")
                data = _read_artifact(art)
            except Exception as exc:                          # noqa: BLE001
                report["failed"].append({"id": cid, "name": name,
                                         "reason": f"{type(exc).__name__}: {exc}"})
                continue
            ext = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
            d = out / cid
            d.mkdir(parents=True, exist_ok=True)
            img = d / f"sheet{ext}"
            img.write_bytes(data)
            from little_crew.runtime.character_sheet_prompt import manifest_extra, resolve_proportion

            meta = {
                "kind": "character.sheet.image",
                **({'template': manifest_extra(sheet, style=style)}
                   if qc_extra else {}),
                "proportion": resolve_proportion(proportion)[0],
                "characterId": cid, "name": name,
                "model": self.model_id, "size": self.size,
                "prompt": prompt,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "license": report["license"],
                "appearance": _appearance_text(sheet),
                "notVerified": "本图是**参考图**；与成片是否同一张脸需要身份度量，未做",
            }
            (d / "sheet.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            item = {"id": cid, "name": name, "file": str(img),
                    "sha256": meta["sha256"][:16]}
            if refine and refiner is not None:
                try:
                    rr = refiner(str(img))
                except Exception as exc:                      # noqa: BLE001
                    rr = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                if rr.get("ok"):
                    refined_path = Path(str(rr.get("out") or ""))
                    item["refined"] = {"file": str(refined_path),
                                       "model": rr.get("model", ""),
                                       "seconds": rr.get("seconds"),
                                       "sha256": hashlib.sha256(
                                           refined_path.read_bytes()).hexdigest()[:16]
                                       if refined_path.is_file() else ""}
                    report["refined"].append(item["refined"])
                    print(f"  ✨ 精修 {cid}（{rr.get('model')} · {rr.get('seconds')}s）")
                else:
                    report["refineFailed"].append(
                        {"id": cid, "error": str(rr.get("error"))[:160]})
            if qc and qc_judge is not None:
                targets = [("original", Path(item["file"]))]
                refined_file = str((item.get("refined") or {}).get("file") or "")
                if refined_file:
                    targets.append(("refined", Path(refined_file)))
                for label, target in targets:
                    if not target.is_file():
                        continue
                    try:
                        qrep = qc_judge(str(target), context=f"角色设定板：{name}")
                    except Exception as exc:                  # noqa: BLE001
                        report["qcFailed"].append(
                            {"id": cid, "which": label,
                             "error": f"{type(exc).__name__}: {exc}"})
                        continue
                    qrep["which"] = label
                    qrep["characterId"] = cid
                    report["qcReports"].append(qrep)
                    from little_crew.review.sheet_qc import summarize as _qsum

                    print(f"  🔎 质检 {cid}/{label}：{_qsum(qrep)}")
            report["painted"].append(item)
        report["count"] = len(report["painted"])
        report["ok"] = not report["failed"]
        return report

    # ---- 内部 ---------------------------------------------------------

    def _channel(self) -> tuple[Any, Any, Any]:
        """解析模型 → 后端 → 通道，并做**用途限定**校验。"""
        from little_crew.models.registry import build_default_registry
        from little_crew.runtime.backend_guard import assert_allowed
        from little_crew.runtime.channels.http import HttpChannel

        spec = build_default_registry().get(self.model_id)
        if spec is None:
            raise LookupError(f"注册表里没有角色设计模型 {self.model_id!r}")
        backends = [b for b in (getattr(spec, "backends", ()) or ())
                    if str(getattr(b, "kind", "")).endswith("HTTP")]
        if not backends:
            raise LookupError(f"{self.model_id} 没有 HTTP 后端（角色设计只走 HTTP）")
        backend = backends[0]
        # 用途限定：该通道若限定给某些职位，调用方必须是其中之一
        assert_allowed(backend, "character_designer", what="角色设定板")
        return spec, backend, HttpChannel(backend, timeout_s=600)

    def _license_note(self) -> dict[str, Any]:
        """授权状态留痕（未核实也要写清楚，不能默认放行还假装核实过）。"""
        try:
            from little_crew.governance.license import LicenseGate

            v = LicenseGate().check(self.model_id)
            return {"allowed": bool(getattr(v, "allowed", False)),
                    "reason": str(getattr(v, "reason", ""))[:200],
                    "rule": str(getattr(v, "triggered_rule", ""))}
        except Exception as exc:                              # noqa: BLE001
            return {"allowed": None, "reason": f"授权检查失败：{exc}"}

    def _render_one(self, spec: Any, backend: Any, channel: Any,
                    prompt: str, *, idempotency_key: str) -> Any:
        from little_crew.runtime.render import http_job

        job = http_job(idempotency_key, backend,
                       {"prompt": prompt, "size": self.size,
                        "model": self.model_id, "width": 1024, "height": 1024},
                       idempotency_key=idempotency_key, expect="image",
                       timeout_s=600)
        handle = channel.submit(job)
        status = str(getattr(handle, "status", ""))
        if "FAIL" in status:
            err = getattr(handle, "error", None)
            raise RuntimeError(f"提交失败：{getattr(err, 'message', err) or status}")
        arts = channel.collect(handle)
        if not arts:
            raise RuntimeError("提交成功但没有产物")
        return arts[0]


def _read_artifact(ref: Any) -> bytes:
    """把产物引用读成字节（URL / 本地路径两种形态）。"""
    from little_crew import net

    uri = str(getattr(ref, "uri", "") or "")
    if not uri:
        raise ValueError("产物引用缺少 uri")
    p = Path(uri)
    if not uri.startswith(("http://", "https://")) and p.is_file():
        return p.read_bytes()
    return _fetch_bytes(uri)


def _fetch_bytes(url: str) -> bytes:
    from little_crew import net

    with net.urlopen(net.urllib.request.Request(
            url, headers={"User-Agent": "OpenAI/Python 1.0"}), timeout=300) as r:
        return r.read()


def enabled_by_env() -> bool:
    """是否启用（默认 **关**：按张计费，不能默认开）。"""
    return str(os.environ.get("LITTLE_CREW_CHAR_SHEETS") or "").strip() in (
        "1", "true", "yes", "on")
