"""一集运行选项的**公共装配层**（2026-11-14，A1）。

## 为什么需要它

`--render-model / --cast-anchor / --style-hint / --shots-target /
--target-seconds / --cover / --char-sheets / --script-threshold` 这些能力先在
`episode_demo` 里长出来，然后是产品 CLI 与 Server API 需要同一套语义。

**同一份契约两处维护必然漂移**（本项目的既有教训：HTTP 模板与 `_values()`
各写一份 → 422；demo 能合视频、CLI 永远合不出来）。所以这里只写一次：

```python
opts = normalize(...)          # 解析/校验（角色锚点、路径存在性、appearance 注入）
apply_options(runner, opts)    # 把语义落到 runner 属性上
pick_main_backend(render_model)  # 主渲染用哪个模型/后端（本地 or 云端）
```

## 纪律

- **锚点外观必须注入**：只给图不够（实测出来是黑风衣+背包）——这里读同目录
  `sheet.json` 的 `appearance` 拼进统一画风，并明确"禁止换装/改发型"。
- **云端后端不探活**：远端根路径 401，探活必假（与角色设计师同一结论）。
- **失败要能说清楚**：模型没注册 / 锚点路径不存在 → 明确报错，不静默忽略。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 这些选项直接映射到 runner 属性（名字 → 默认值）
_RUNNER_ATTRS: dict[str, Any] = {
    "shots_target": 0,
    "target_seconds": 0.0,
    "style_hint": "",
    "render_path": "local",
    "cloud_model": "",
    "compare_local": False,
    "vision_max_shots": 12,
    "cover_enabled": False,
    "cover_mode": "select",
    "cover_title": "",
    "cover_title_mode": "overlay",
    "character_sheets_enabled": False,
    "character_sheet_style": "",
    "character_sheet_proportion": "adult",
    "character_sheet_refine": False,
    "character_sheet_qc": False,
}


def parse_cast_anchors(items: Iterable[str] | Mapping[str, str] | None) -> dict[str, str]:
    """把 `["少女=path", ...]` 或 `{name: path}` 解析成 `{name: 绝对路径}`。

    **路径必须存在**：不存在就报错（否则会在渲染阶段变成"锚点 2 个"里少一个，
    而画面只是"没那么像"——很难察觉）。
    """
    out: dict[str, str] = {}
    if not items:
        return out
    if isinstance(items, Mapping):
        pairs = [(str(k), str(v)) for k, v in items.items()]
    else:
        pairs = []
        for raw in items:
            s = str(raw or "").strip()
            if not s:
                continue
            if "=" not in s:
                raise ValueError(f"角色锚点格式应为 NAME=PATH：{s!r}")
            n, p = s.split("=", 1)
            pairs.append((n.strip(), p.strip()))
    for name, path in pairs:
        if not name:
            raise ValueError(f"角色锚点缺少角色名：{name!r}")
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"角色锚点不存在：{path}")
        out[name] = str(p.resolve())
    return out


def anchor_appearances(anchors: Mapping[str, str]) -> list[str]:
    """读锚点同目录 `sheet.json` 的 `appearance`，拼成"固定外观"句子列表。"""
    apis: list[str] = []
    for name, pth in anchors.items():
        try:
            meta = Path(pth).with_name("sheet.json")
            if meta.is_file():
                obj = json.loads(meta.read_text(encoding="utf-8")) or {}
                app = str(obj.get("appearance") or "").strip()
                if app:
                    apis.append(f"「{name}」固定外观：{app}")
        except Exception:                                     # noqa: BLE001
            continue
    return apis


def style_with_appearances(style: str, anchors: Mapping[str, str]) -> str:
    """把锚点外观拼进画风提示（**只给图不够，必须给文字约束**）。"""
    base = str(style or "").strip()
    apis = anchor_appearances(anchors) if anchors else []
    if not apis:
        return base
    return (base + "；" if base else "") + "；".join(apis) + \
        "。每一镜的服装、发型、配饰必须与参考图完全一致，" \
        "禁止换装、禁止改发型、禁止添加背包/外套等参考图没有的物件。"


def apply_anchor_appearance(runner: Any, items: Any) -> dict[str, Any]:
    """给 runner 装上 `cast_anchors`，并把外观注入 `style_hint`。

    返回 `{"castAnchors": [...], "appearances": n, "styleHint": len}`，
    调用方可直接打印/写进证据。
    """
    anchors = parse_cast_anchors(items)
    runner.cast_anchors = dict(anchors)
    runner.style_hint = style_with_appearances(
        str(getattr(runner, "style_hint", "") or ""), anchors)
    return {"castAnchors": sorted(anchors),
            "appearances": len(anchor_appearances(anchors)),
            "styleHint": len(str(runner.style_hint or ""))}


def set_script_threshold(value: float) -> float:
    """设置剧本阈值（0 = 不设，沿用默认 65）。返回实际生效值。"""
    th = float(value or 0.0)
    if th > 0:
        os.environ["LITTLE_CREW_SCRIPT_THRESHOLD"] = str(th)
    return th


def normalize(*, cast_anchors: Iterable[str] | Mapping[str, str] | None = None,
              style_hint: str = "",
              render_path: str = "local",
              render_model: str = "", shots_target: int = 0,
              target_seconds: float = 0.0, cover: bool = False,
              cover_title: str = "", cover_mode: str = "select",
              cover_title_mode: str = "overlay",
              char_sheets: bool = False, char_sheet_style: str = "",
              char_sheet_proportion: str = "adult",
              char_sheet_refine: bool = False, char_sheet_qc: bool = False,
              vision_max: int = 12, script_threshold: float = 0.0,
              compare_local: bool = False,
              ) -> dict[str, Any]:
    """解析 + 校验 + （锚点存在时）**自动注入外观**。返回可直接应用的选择字典。"""
    path = str(render_path or "local").strip().lower() or "local"
    if path not in ("local", "cloud"):
        raise ValueError(f"render_path 只能是 local / cloud：{render_path!r}")
    anchors = parse_cast_anchors(cast_anchors)
    apis = anchor_appearances(anchors) if anchors else []
    style = style_with_appearances(str(style_hint or ""), anchors)
    return {
        "cast_anchors": anchors,
        "anchorAppearances": apis,
        "shots_target": int(shots_target or 0),
        "target_seconds": float(target_seconds or 0.0),
        "style_hint": style,
        "render_path": path,
        "cloud_model": str(render_model or ""),
        "compare_local": bool(compare_local),
        "vision_max_shots": int(vision_max or 12),
        "cover_enabled": bool(cover),
        "cover_mode": str(cover_mode or "select"),
        "cover_title": str(cover_title or ""),
        "cover_title_mode": str(cover_title_mode or "overlay"),
        "character_sheets_enabled": bool(char_sheets),
        "character_sheet_style": str(char_sheet_style or ""),
        "character_sheet_proportion": str(char_sheet_proportion or "adult"),
        "character_sheet_refine": bool(char_sheet_refine),
        "character_sheet_qc": bool(char_sheet_qc),
        "script_threshold": float(script_threshold or 0.0),
    }


def apply_options(runner: Any, opts: Mapping[str, Any]) -> dict[str, Any]:
    """把 `normalize()` 的结果落到 runner 属性上，返回"实际生效"的摘要。"""
    for attr, default in _RUNNER_ATTRS.items():
        try:
            setattr(runner, attr, opts.get(attr, default))
        except Exception:                                     # noqa: BLE001
            continue
    # 角色锚点（覆盖/补充库里发现的锚点）
    runner.cast_anchors = dict(opts.get("cast_anchors") or {})
    # 剧本阈值：只放宽分数线（阻断级意见/硬约束照旧阻断）
    th = float(opts.get("script_threshold") or 0.0)
    if th > 0:
        os.environ["LITTLE_CREW_SCRIPT_THRESHOLD"] = str(th)
    return {"castAnchors": sorted((opts.get("cast_anchors") or {}).keys()),
            "anchorAppearances": len(opts.get("anchorAppearances") or ()),
            "styleHint": bool(opts.get("style_hint")),
            "renderModel": str(opts.get("cloud_model") or ""),
            "renderPath": str(opts.get("render_path") or "local"),
            "shotsTarget": int(opts.get("shots_target") or 0),
            "targetSeconds": float(opts.get("target_seconds") or 0.0),
            "cover": bool(opts.get("cover_enabled")),
            "charSheets": bool(opts.get("character_sheets_enabled")),
            "scriptThreshold": th}


def pick_main_spec(render_model: str = "",
                   render_path: str = "local") -> tuple[Any, Any]:
    """主渲染用哪个模型 —— 返回 `(spec, HTTP backend)`。

    `render_model` 为空时退到内置 `z-image-turbo`（与旧行为一致）。
    云端模型（自定义供应商）后端**不探活**：远端根路径通常 401。
    """
    from little_crew.models.registry import build_default_registry
    from little_crew.models.spec import BackendKind

    reg = build_default_registry()
    default_cloud = "gpt-image-2.6-sunburst"
    mid = str(render_model or "").strip()
    if not mid:
        mid = default_cloud if str(render_path or "local") == "cloud" else "z-image-turbo"
    spec = reg.get(mid)
    if spec is None:
        raise RuntimeError(
            f"渲染模型 {mid!r} 未注册（可用 `little-crew provider list` 查看已配置模型）")
    be = next((b for b in (getattr(spec, "backends", ()) or ())
               if b.kind is BackendKind.HTTP), None)
    if be is None:
        raise RuntimeError(f"模型 {mid!r} 未声明 HTTP 后端")
    return spec, be


def is_local_backend(be: Any) -> bool:
    """后端是否本机（云端不探活，见模块说明）。"""
    try:
        from little_crew.runtime.render_policy import is_local

        return bool(is_local(be))
    except Exception:                                         # noqa: BLE001
        ep = str(getattr(be, "endpoint", "") or "").lower()
        return ("127.0.0.1" in ep or "localhost" in ep or not ep)
