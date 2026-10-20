"""把一集的产物变成**一页能看的 HTML**（零依赖、单文件、不复制任何文件）。

## 为什么是"链过去"而不是"拷过来"

一集 40 镜：控制图 40×184KB + 成片 40×~1.7MB ≈ **70 MB**。
拷一份进报告目录，等于把产物翻倍，而且改一次要重拷一次。

所以：**报告只写相对路径，指向已有的文件。**
代价是报告不能单独发出去 —— 那时再做"打包导出"（另说）。

## 它回答什么问题

跑完一集之后，原本要这样看：

```
打开 previs/SH-xxx/  → 看白模
打开 cache/t1/...    → 找成片图（嵌套 6 层）
打开 run.report.json → 读数字
```

**现在一页看完**：每镜的白模 · 控制图 · 成片 · 提示词 · 判定。

## 四个刻意的取舍

1. **不引框架** —— 项目硬约束 `dependencies = []`，手写 HTML + 内联 CSS
2. **成片用原图**，不做缩略图 —— 缩略图要么加依赖，要么失真；用 CSS 限宽即可
3. **`reason` 缺了就明说** —— 早于 `alpha.113` 的运行没有记录 layout 的理由，
   报告里写「本次运行未记录」，而不是留空让人以为是空的
4. **失败的镜头单独列在顶部** —— 别让人翻了 40 个卡片才发现有 3 个没出图
"""
from __future__ import annotations

import json
import html
from pathlib import Path
from typing import Any, Mapping

__all__ = ["build_report", "resolve_artifact"]

#: 报告文件名（放在 `<workdir>/report/` 下）
REPORT_NAME = "index.html"


def resolve_artifact(workdir: Path, name: str) -> Path | None:
    """把 manifest 里的 artifact 名解析成真实文件路径。

    ⚠️ artifact 名是**裸文件名**，真实路径在 `cache/t{N}/<多层哈希>/` 下面。
    优先 `t1`（第一层），没有再 `t2` —— 两层是同一份内容的缓存层级。
    """
    if not name:
        return None
    for tier in ("t1", "t2", "t3"):
        base = workdir / "cache" / tier
        if not base.is_dir():
            continue
        hits = list(base.rglob(name))
        if hits:
            return hits[0]
    return None


def _rel(target: Path, frm: Path) -> str:
    """生成 POSIX 风格的相对路径（HTML 里必须用 /）。"""
    try:
        return target.resolve().relative_to(frm.resolve()).as_posix()
    except ValueError:
        import os

        return Path(os.path.relpath(target.resolve(), frm.resolve())).as_posix()


def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def _read_json(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return {}


CSS = """
:root{--bg:#14161a;--card:#1d2026;--line:#2c313a;--fg:#e6e8ec;--dim:#9aa3b2;
--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--accent:#7dd3fc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 ui-sans-serif,system-ui,"Segoe UI",Microsoft YaHei,sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line)}
h1{margin:0 0 6px;font-size:20px;font-weight:650}
.sub{color:var(--dim);font-size:13px}
.kpis{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:8px 12px;min-width:104px}
.kpi .v{font-size:17px;font-weight:650}
.kpi .k{color:var(--dim);font-size:11px;letter-spacing:.04em}
main{padding:20px 24px 60px}
h2{font-size:15px;margin:26px 0 12px;color:var(--dim);font-weight:600;
letter-spacing:.05em;text-transform:uppercase}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
overflow:hidden;display:flex;flex-direction:column}
.card .hd{display:flex;flex-wrap:wrap;gap:6px;align-items:center;
padding:10px 12px;border-bottom:1px solid var(--line)}
.id{font-weight:650;font-size:12.5px}
.tag{font-size:11px;color:var(--dim);border:1px solid var(--line);
border-radius:5px;padding:1px 6px}
.tag.ok{color:var(--ok);border-color:var(--ok)}
.tag.bad{color:var(--bad);border-color:var(--bad)}
.shots{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line)}
.shot{background:#0f1115;position:relative}
.shot img,.shot video{width:100%;height:132px;object-fit:contain;display:block;
background:#0f1115}
.shot .lbl{position:absolute;left:6px;top:6px;font-size:10.5px;
background:rgba(0,0,0,.62);color:#cfd6e3;padding:1px 5px;border-radius:4px}
.reason{padding:9px 12px;border-top:1px solid var(--line);color:#b9c2d0;
font-size:12px}
.reason b{color:var(--accent);font-weight:600}
.prompt{padding:0 12px 10px;color:var(--dim);font-size:12px}
details summary{cursor:pointer;color:var(--dim);font-size:12px;padding:0 12px 10px}
.miss{padding:9px 12px;color:var(--warn);font-size:12px}
table{border-collapse:collapse;font-size:12.5px}
td,th{border-bottom:1px solid var(--line);padding:4px 10px;text-align:left}
th{color:var(--dim);font-weight:600}
.note{background:#1a1f27;border:1px solid var(--line);border-left:3px solid var(--warn);
border-radius:6px;padding:10px 14px;color:#c8d0dc;font-size:12.5px;margin:14px 0}
a{color:var(--accent)}
"""


def build_report(workdir: Path, *, title: str = "") -> Path:
    """生成 `<workdir>/report/index.html`，返回路径。"""
    workdir = Path(workdir)
    delivery = workdir / "delivery"
    rep = _read_json(delivery / "run.report.json")
    man = _read_json(delivery / "episode.master.json")
    evidence = _read_json(delivery / "delivery.evidence.json")

    outdir = workdir / "report"
    outdir.mkdir(parents=True, exist_ok=True)

    shots = list(man.get("shots") or [])
    previs = {d.name: d for d in (workdir / "previs").glob("*") if d.is_dir()}
    pv_info = (rep.get("previs") or {})
    pv_ok = pv_info.get("ok")
    reason_by_shot: dict[str, str] = {}
    for item in (pv_info.get("shots") or []):
        if isinstance(item, Mapping):
            sid = str(item.get("shotId") or "")
            lay = item.get("layout") or {}
            if sid and lay.get("reason"):
                reason_by_shot[sid] = str(lay["reason"])

    hard = (evidence.get("hardRules") or evidence.get("checks")
            or rep.get("manifest", {}).get("checks") or {})
    hard_txt = ""
    if isinstance(hard, Mapping) and hard:
        okn = sum(1 for v in hard.values()
                  if v is True or (isinstance(v, Mapping) and v.get("ok")))
        hard_txt = f"{okn}/{len(hard)}"

    def kpi(v: Any, k: str, cls: str = "") -> str:
        return (f'<div class="kpi"><div class="v {cls}">{_esc(v)}</div>'
                f'<div class="k">{_esc(k)}</div></div>')

    parts: list[str] = []
    A = parts.append

    A("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>")
    A("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    A(f"<title>{_esc(title or man.get('title') or rep.get('episodeId'))}</title>")
    A(f"<style>{CSS}</style></head><body>")

    A("<header>")
    A(f"<h1>{_esc(title or man.get('title') or '未命名')}")
    A(f" <span class='sub'>· {_esc(rep.get('episodeId') or man.get('episodeId'))}</span></h1>")
    ok = rep.get("ok")
    A(f"<div class='sub'>判定：<b style='color:"
      f"{'var(--ok)' if ok else 'var(--bad)'}'>{'通过' if ok else '未通过'}</b>"
      f" · 剧本 {_esc(rep.get('scriptRounds'))} 轮 → {_esc(rep.get('scriptVerdict'))}"
      f" · 生成于 {_esc(man.get('createdAt'))}</div>")
    A("<div class='kpis'>")
    A(kpi(man.get("shotCount") or len(shots), "镜头数"))
    A(kpi(f"{rep.get('totalSeconds') or man.get('totalSeconds') or 0}s", "成片时长"))
    A(kpi(f"{round(float(rep.get('wallSeconds') or 0))}s", "墙钟"))
    A(kpi(rep.get("coverage") or "—", "覆盖率"))
    if hard_txt:
        A(kpi(hard_txt, "硬规则"))
    A(kpi(rep.get("rendered") or 0, "实算镜数"))
    A(kpi(rep.get("cacheHits") or 0, "缓存命中"))
    A(kpi(len(previs), "白模镜头"))
    A("</div></header><main>")

    # ---- 需要说明的事 ----
    notes: list[str] = []
    if pv_ok is None:
        notes.append("本次运行的 <b>previs 记录为空</b> —— "
                     "它早于 <code>alpha.113</code>（那版才把 "
                     "<code>art</code>/<code>schedule</code>/<code>previs</code> "
                     "写进结报告）。白模文件在磁盘上存在（下面能看到），"
                     "但每个镜头的 <b>reason 没有记录</b>。")
    if rep.get("failures"):
        notes.append(f"有 <b>{len(rep['failures'])}</b> 个镜头失败，已单独列在最前。")
    if not hard_txt:
        notes.append("证据里没有读到硬规则结果 —— 可能字段名与预期不同，"
                     "报告只展示能读到的部分。")
    for n in notes:
        A(f"<div class='note'>{n}</div>")

    # ---- 失败镜（置顶）----
    fails = rep.get("failures") or []
    if fails:
        A("<h2>失败的镜头</h2><table><tr><th>镜号</th><th>原因</th></tr>")
        for f in fails:
            if isinstance(f, Mapping):
                A(f"<tr><td>{_esc(f.get('shotId') or f.get('shot_id'))}</td>"
                  f"<td>{_esc(f.get('reason') or f.get('failure') or '')}</td></tr>")
            else:
                A(f"<tr><td colspan='2'>{_esc(f)}</td></tr>")
        A("</table>")

    # ---- 镜头卡片 ----
    A("<h2>逐镜</h2><div class='grid'>")
    for s in shots:
        sid = str(s.get("shotId") or "")
        pv = previs.get(sid)
        art = resolve_artifact(workdir, (s.get("artifacts") or [""])[0])
        A("<div class='card'><div class='hd'>")
        A(f"<span class='id'>{_esc(sid)}</span>")
        for tag in (s.get("shotSize"), s.get("cameraMovement"),
                    f"{s.get('durationSec')}s",
                    " / ".join(s.get("characters") or ())):
            if tag:
                A(f"<span class='tag'>{_esc(tag)}</span>")
        A(f"<span class='tag {'ok' if s.get('ready') else 'bad'}'>"
          f"{'就绪' if s.get('ready') else '未就绪'}</span>")
        A("</div>")

        cells: list[str] = []
        if pv and (pv / "previs.mp4").is_file():
            src = _rel(pv / "previs.mp4", outdir)
            cells.append(f"<div class='shot'><span class='lbl'>白模视频</span>"
                         f"<video src='{_esc(src)}' controls preload='none' "
                         f"muted loop playsinline></video></div>")
        else:
            cells.append("<div class='shot'><span class='lbl'>白模视频</span>"
                         "<div class='miss'>无</div></div>")
        if pv and (pv / "previs_control.png").is_file():
            src = _rel(pv / "previs_control.png", outdir)
            cells.append(f"<div class='shot'><span class='lbl'>控制图（喂模型）</span>"
                         f"<img loading='lazy' src='{_esc(src)}' alt='control'></div>")
        elif pv and (pv / "previs_first.png").is_file():
            src = _rel(pv / "previs_first.png", outdir)
            cells.append(f"<div class='shot'><span class='lbl'>白模首帧</span>"
                         f"<img loading='lazy' src='{_esc(src)}' alt='first'></div>")
        else:
            cells.append("<div class='shot'><span class='lbl'>控制图</span>"
                         "<div class='miss'>无</div></div>")
        if art is not None:
            src = _rel(art, outdir)
            cells.append(f"<div class='shot'><span class='lbl'>成片</span>"
                         f"<img loading='lazy' src='{_esc(src)}' alt='still'></div>")
        else:
            cells.append("<div class='shot'><span class='lbl'>成片</span>"
                         "<div class='miss'>找不到文件</div></div>")
        A(f"<div class='shots'>{''.join(cells)}</div>")
        # 第三格：成片放右边，左边两格上半
        reason = reason_by_shot.get(sid)
        if reason:
            A(f"<div class='reason'><b>理由</b> 　{_esc(reason)}</div>")
        if s.get("prompt"):
            pr = str(s["prompt"])
            if len(pr) <= 120:
                A(f"<div class='prompt'>{_esc(pr)}</div>")
            else:
                A(f"<details><summary>提示词（{len(pr)} 字）</summary>"
                  f"<div class='prompt'>{_esc(pr)}</div></details>")
        A("</div>")
    A("</div>")

    # ---- 其它阶段 ----
    A("<h2>其它职位</h2><table>")
    for label, key in (("导演", "director"), ("灯光", "gaffer"),
                       ("美术", "art"), ("排期", "schedule")):
        v = rep.get(key)
        state = "有产出" if v else "无"
        A(f"<tr><th>{label}</th><td>{_esc(state)}</td></tr>")
    A("</table>")

    A("<h2>成片</h2>")
    master = delivery / "episode.master.mp4"
    if master.is_file():
        src = _rel(master, outdir)
        A(f"<video src='{_esc(src)}' controls width='720' "
          f"style='max-width:100%;border-radius:8px'></video>")
    else:
        A("<div class='miss'>未找到 episode.master.mp4</div>")

    A("</main></body></html>")

    out = outdir / REPORT_NAME
    out.write_text("\n".join(parts), encoding="utf-8")
    return out
