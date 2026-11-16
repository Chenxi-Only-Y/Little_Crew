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
    """读一个 JSON 对象；**读不到、或读到的不是对象，都返回空 dict**。

    ⚠️ 文件里写着 `null` 时 `json.loads` 返回的是 `None` —— 那不是 dict，
    后面 `man.get(...)` 会 `AttributeError`，**报告页直接崩**（审计 L-6）。
    而 `run.report.json` 里的 `"manifest": null` 正是
    `EpisodeResult.as_dict()` 在"没有 manifest"时写出来的形状 ——
    也就是说这条崩溃路径**真实可达**，不是理论风险。
    """
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


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

.graphwrap{overflow:auto;background:#101318;border:1px solid var(--line);
border-radius:10px;padding:6px}
.graphwrap svg{display:block;min-width:100%}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--dim);font-size:11.5px;
margin:8px 0 0}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;
margin-right:5px;vertical-align:middle}
.badge{display:inline-block;font-size:11.5px;border-radius:5px;padding:1px 7px;
border:1px solid var(--line);color:var(--dim)}
.badge.ok{color:var(--ok);border-color:var(--ok)}
.badge.warn{color:var(--warn);border-color:var(--warn)}
.badge.bad{color:var(--bad);border-color:var(--bad)}
.cols{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.box h3{margin:0 0 8px;font-size:12.5px;color:var(--dim);font-weight:600}
.kv{display:flex;justify-content:space-between;gap:10px;font-size:12px;
border-bottom:1px dashed rgba(255,255,255,.06);padding:2px 0}
.kv b{font-weight:600;color:#dfe5ee}
.thumb{max-width:100%;border-radius:8px;border:1px solid var(--line)}
ul.tight{margin:4px 0 0 18px;padding:0;color:#c4ccd9;font-size:12px}
"""



#: 画布图里的阶段先后顺序（决定列）
_STAGE_ORDER = ("script", "asset", "storyboard", "render", "gate", "assemble")
_STAGE_CN = {"script": "剧本", "asset": "资产", "storyboard": "分镜",
             "render": "渲染", "gate": "门禁/审片", "assemble": "合成交付"}
_STATUS = {"done": ("#4ade80", "已完成"), "pending": ("#fbbf24", "启用但无证据"),
           "skipped": ("#6b7280", "编制未启用"), "blocked": ("#f87171", "被门禁阻断")}


def graph_svg(graph: Mapping[str, Any]) -> str:
    """把 `pipeline.graph.json` 画成**内联 SVG**（无 JS、无 CDN，离线可看）。"""
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, Mapping)]
    if not nodes:
        return "<div class='miss'>没有 pipeline.graph.json（旧版本运行或未启用画布后端）</div>"
    cols: dict[str, list[Mapping[str, Any]]] = {}
    for n in nodes:
        stage = str(n.get("stage") or "")
        key = stage if stage in _STAGE_ORDER else "gate"
        cols.setdefault(key, []).append(n)
    order = [s for s in _STAGE_ORDER if s in cols]
    srcs = list(graph.get("sources") or [])
    if srcs:
        order = ["__src__"] + order

    NW, NH, GAP, COLW, ROWH, PAD = 150, 40, 22, 196, 58, 20
    max_rows = max([len(cols.get(s) or []) for s in _STAGE_ORDER] + [len(srcs), 1])
    width = PAD * 2 + COLW * len(order)
    height = 40 + max_rows * ROWH + PAD
    pos: dict[str, tuple[int, int]] = {}
    out: list[str] = [
        f"<svg width='{max(width, 640)}' height='{height}' "
        f"viewBox='0 0 {max(width, 640)} {height}' "
        "xmlns='http://www.w3.org/2000/svg' font-family='ui-sans-serif,system-ui'>",
        "<defs><marker id='ar' viewBox='0 0 8 8' refX='7' refY='4' "
        "markerWidth='6' markerHeight='6' orient='auto'>"
        "<path d='M0,0 L8,4 L0,8 z' fill='#3b4250'/></marker></defs>",
    ]
    for ci, key in enumerate(order):
        x = PAD + ci * COLW
        label = ("来源" if key == "__src__" else _STAGE_CN.get(key, key))
        out.append(f"<text x='{x}' y='18' fill='#8b95a6' font-size='12'>{_esc(label)}</text>")
        if key == "__src__":
            for ri, s in enumerate(srcs):
                y = 34 + ri * ROWH
                pos[f"source:{s.get('artifact')}"] = (x, y)
                out.append(
                    f"<rect x='{x}' y='{y}' width='{NW}' height='{NH}' rx='7' "
                    "fill='#1a1e26' stroke='#394150' stroke-dasharray='4 3'/>")
                out.append(f"<text x='{x+10}' y='{y+18}' fill='#aab3c2' "
                           f"font-size='12'>{_esc(str(s.get('title') or '')[:14])}</text>")
                out.append(f"<text x='{x+10}' y='{y+33}' fill='#6d7788' "
                           f"font-size='10'>{_esc(str(s.get('artifact') or '')[:20])}</text>")
            continue
        col = cols.get(key) or []
        for ri, n in enumerate(col):
            y = 34 + ri * ROWH
            nid = str(n.get("id") or "")
            pos[nid] = (x, y)
            status = str(n.get("status") or "pending")
            color = _STATUS.get(status, ("#6b7280", status))[0]
            active = bool(n.get("active", True))
            dash = " stroke-dasharray='5 4'" if not active else ""
            out.append(
                f"<rect x='{x}' y='{y}' width='{NW}' height='{NH}' rx='7' "
                f"fill='#1b2028' stroke='{color}'{dash}/>")
            out.append(f"<text x='{x+9}' y='{y+17}' fill='#e2e7ef' font-size='11.5'>"
                       f"{_esc(str(n.get('title') or nid)[:13])}</text>")
            out.append(f"<text x='{x+10}' y='{y+33}' fill='#77808f' font-size='10'>"
                       f"{_esc(nid[:22])}</text>")
            out.append(f"<circle cx='{x+NW-11}' cy='{y+11}' r='4' fill='{color}'/>")
    # 边（贝塞尔）
    for e in (graph.get("edges") or []):
        if not isinstance(e, Mapping):
            continue
        a, b = pos.get(str(e.get("from") or "")), pos.get(str(e.get("to") or ""))
        if not a or not b:
            continue
        x1, y1 = a[0] + NW, a[1] + NH / 2
        x2, y2 = b[0], b[1] + NH / 2
        mx = (x1 + x2) / 2
        out.append(
            f"<path d='M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}' "
            "fill='none' stroke='#394150' stroke-width='1.2' marker-end='url(#ar)'>"
            f"<title>{_esc(str(e.get('artifact') or ''))}</title></path>")
    out.append("</svg>")
    return "".join(out)


def build_report(workdir: Path, *, title: str = "") -> Path:
    """生成 `<workdir>/report/index.html`，返回路径。"""
    workdir = Path(workdir)
    delivery = workdir / "delivery"
    rep = _read_json(delivery / "run.report.json")
    man = _read_json(delivery / "episode.master.json")
    evidence = _read_json(delivery / "delivery.evidence.json")
    # ---- 2026-11-16 新增产物（缺了也不能崩：全部走 _read_json）----
    graph = _read_json(delivery / "pipeline.graph.json")
    compliance = _read_json(delivery / "compliance.json")
    ai_labels = _read_json(delivery / "ai_labels.json")
    lipsync = _read_json(delivery / "lipsync.json")
    cam_plan = _read_json(delivery / "camera_plan.json")
    scene_cards = _read_json(delivery / "scene_cards.json")
    scene_views = _read_json(delivery / "scene_views.json")
    outfits = _read_json(delivery / "outfits_used.json")
    image_calls = _read_json(delivery / "image_calls.json")
    token_usage = _read_json(delivery / "token_usage.json")
    post_plan = _read_json(delivery / "post.plan.json")
    continuity = _read_json(delivery / "continuity_audit.json")

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

    # ⚠️ `rep.get("manifest") or {}` 而不是 `rep.get("manifest", {})`（审计 L-6）：
    # 键**存在但值是 None** 时，默认值不生效 —— 而 `"manifest": null`
    # 正是没有 manifest 时写出来的形状，于是 `None.get(...)` 直接崩。
    hard = (evidence.get("hardRules") or evidence.get("checks")
            or (rep.get("manifest") or {}).get("checks") or {})
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
    _crew = rep.get("crew") if isinstance(rep.get("crew"), Mapping) else {}
    if _crew.get("active"):
        A(kpi(f"{len(_crew.get('active') or [])}/"
              f"{len(_crew.get('active') or []) + len(_crew.get('skipped') or [])}",
              f"编制·{_crew.get('preset') or '?'}"))
    if isinstance(compliance, Mapping) and compliance.get("status"):
        _st = str(compliance.get("status"))
        _cls = "ok" if _st == "signed" else ("bad" if _st == "blocked" else "warn")
        A(kpi(_st, "合规状态", _cls))
    _lab = (ai_labels.get("aiLabels") or {}) if isinstance(ai_labels, Mapping) else {}
    if _lab:
        A(kpi(f"显式{'✓' if _lab.get('explicit') else '✗'} · "
              f"隐式{'✓' if _lab.get('implicit') else '✗'}", "AI 标识"))
    _costs = image_calls.get("cloud") if isinstance(image_calls, Mapping) else None
    if isinstance(_costs, Mapping):
        _ic = float(_costs.get("estCostCny") or 0.0)
        _lc = float((lipsync or {}).get("costCny") or 0.0) if isinstance(lipsync, Mapping) else 0.0
        if _ic or _lc:
            A(kpi(f"¥{_ic + _lc:.2f}", "生成费用"))
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

    # ---- 剧组编制 + 静态流水线图（2026-11-16）----
    A("<h2>剧组编制与流水线</h2>")
    _graph = graph if isinstance(graph, Mapping) else {}
    if _crew:
        _ver = _crew.get("verification") or {}
        _lvl = _crew.get("level") or "?"
        _lv_cls = {"full": "ok", "reduced": "warn", "draft": "warn"}.get(str(_lvl), "")
        A("<div class='cols'>")
        A("<div class='box'><h3>编制口径</h3>")
        for k, v in (("模板", _crew.get("preset")), ("指纹", _crew.get("hash")),
                     ("启用", len(_crew.get("active") or ())),
                     ("跳过", len(_crew.get("skipped") or ())),
                     ("自动补齐", "、".join(map(str, (_crew.get("autoAdded") or [])[:6])) or "—")):
            A(f"<div class='kv'><span>{_esc(k)}</span><b>{_esc(v)}</b></div>")
        A(f"<div class='kv'><span>验证级别</span><span class='badge {_lv_cls}'>"
          f"{_esc(_lvl)}</span></div>")
        if _crew.get("unverified"):
            A("<div class='kv'><span>未验证</span><b style='color:var(--warn)'>"
              + _esc("、".join(map(str, _crew.get("unverified") or ()))) + "</b></div>")
        A("</div>")
        A("<div class='box'><h3>质量关键位</h3>")
        for key, cn in (("reviewed", "审片"), ("qualityChecked", "质检（场记）"),
                        ("complianceAudited", "合规")):
            okv = _ver.get(key)
            A(f"<div class='kv'><span>{cn}</span>"
              f"<span class='badge {'ok' if okv else 'badwarn'}' "
              f"style='{'color:var(--ok)' if okv else 'color:var(--warn)'}'>"
              f"{'已启用' if okv else '未启用（报告不标通过）'}</span></div>")
        A("</div></div>")
    else:
        A("<div class='note'>本次运行没有记录剧组编制（旧版本或直接调用 EpisodeRunner）。</div>")
    A("<div class='graphwrap'>" + graph_svg(_graph) + "</div>")
    A("<div class='legend'><span><i style='background:#4ade80'></i>done（有证据）</span>"
      "<span><i style='background:#fbbf24'></i>pending（启用但无证据）</span>"
      "<span><i style='background:#6b7280'></i>skipped（编制未启用）</span>"
      "<span>悬停连线可看产物名</span></div>")
    if _graph.get("summary"):
        _s = _graph.get("summary") or {}
        A(f"<div class='sub' style='margin-top:8px'>图：{_esc(_s.get('positions'))} 职位 · "
          f"启用 {_esc(_s.get('active'))} · done {_esc(_s.get('done'))} · "
          f"pending {_esc(_s.get('pending'))} · skipped {_esc(_s.get('skipped'))} · "
          f"边 {_esc(_s.get('edges'))}</div>")

    # ---- 合规与交付标识 ----
    A("<h2>合规与交付标识</h2><div class='cols'>")
    if compliance:
        _cst = str(compliance.get("status") or "?")
        _cc = "ok" if _cst == "signed" else ("bad" if _cst == "blocked" else "warn")
        A("<div class='box'><h3>合规官</h3>")
        A(f"<div class='kv'><span>状态</span><span class='badge {_cc}'>{_esc(_cst)}</span></div>")
        A(f"<div class='kv'><span>是否阻断交付</span><b>{_esc(compliance.get('blocking'))}</b></div>")
        _la = compliance.get("licenseAudit") or {}
        A(f"<div class='kv'><span>授权：拒绝 / 待人工</span>"
          f"<b>{_esc(len(_la.get('denied') or ()))} / "
          f"{_esc(len(_la.get('needsReview') or ()))}</b></div>")
        _da = compliance.get("deliveryAudit") or {}
        A(f"<div class='kv'><span>交付审计</span>"
          f"<b>{'通过' if _da.get('ok') else '缺项'}</b></div>")
        _cf = compliance.get("contentFirstPass") or {}
        A(f"<div class='kv'><span>内容初审</span>"
          f"<b>{'已做（' + str(_cf.get('reviewed')) + ' 张）' if _cf.get('performed') else '未做'}</b></div>")
        _hr = compliance.get("humanReview") or []
        A(f"<div class='kv'><span>人工/法务复审</span><b>{_esc(len(_hr))} 项</b></div>")
        if _hr:
            A("<ul class='tight'>")
            for it in _hr[:8]:
                A(f"<li>{_esc(it.get('why') or it.get('item') or it.get('modelId') or '')}</li>")
            A("</ul>")
        A("</div>")
    else:
        A("<div class='box'><h3>合规官</h3><div class='miss'>本次无 compliance.json</div></div>")
    if ai_labels:
        A("<div class='box'><h3>交付标识 / 署名</h3>")
        for k, v in (("显式标识", _lab.get("explicit")), ("隐式标识（mp4 元数据）", _lab.get("implicit")),
                     ("署名", ai_labels.get("credits")),
                     ("素材登记", "✓ " + str(len(ai_labels.get("sources") or ())) + " 项"
                      if ai_labels.get("sourceRegistryOk") else "✗ 缺项")):
            A(f"<div class='kv'><span>{_esc(k)}</span><b>{_esc(v)}</b></div>")
        _where = ai_labels.get("aiLabels", {}).get("where") or []
        if _where:
            A("<ul class='tight'>" + "".join(f"<li>{_esc(w)}</li>" for w in _where[:5]) + "</ul>")
        A("</div>")
    else:
        A("<div class='box'><h3>交付标识</h3><div class='miss'>本次无 ai_labels.json</div></div>")
    A("</div>")

    # ---- 增强能力（口型/相机/场景卡/造型/后期）----
    A("<h2>增强能力</h2><div class='cols'>")
    if lipsync:
        A("<div class='box'><h3>口型通道</h3>")
        A(f"<div class='kv'><span>启用的镜头</span>"
          f"<b>{_esc(len(lipsync.get('targets') or ()))}</b></div>")
        A(f"<div class='kv'><span>成功</span><b>{_esc(lipsync.get('okCount'))}</b></div>")
        A(f"<div class='kv'><span>费用</span>"
          f"<b>¥{float(lipsync.get('costCny') or 0):.2f}</b></div>")
        for r in (lipsync.get("rows") or [])[:4]:
            _fit = r.get("fit") or {}
            A(f"<div class='kv'><span>{_esc(r.get('shotId'))}</span>"
              f"<b>{_esc(r.get('clipSeconds'))}s / 镜 {_esc(r.get('shotSeconds'))}s "
              f"（{_esc(_fit.get('method') or '—')}）</b></div>")
        A("</div>")
    if cam_plan:
        A("<div class='box'><h3>相机参数（确定性注入）</h3>")
        _def = cam_plan.get("default") or {}
        A(f"<div class='kv'><span>全片默认</span><b>"
          + _esc("、".join(f"{k}={v}" for k, v in _def.items()) or "—") + "</b></div>")
        A(f"<div class='kv'><span>按镜覆盖</span>"
          f"<b>{_esc(len(cam_plan.get('perShot') or ()))} 镜</b></div>")
        A("</div>")
    if scene_cards:
        A("<div class='box'><h3>场景卡</h3>")
        _cards = (scene_cards.get("cards") or {}) if isinstance(scene_cards, Mapping) else {}
        A(f"<div class='kv'><span>场景卡数</span><b>{_esc(len(_cards))}</b></div>")
        A(f"<div class='kv'><span>使用镜头</span>"
          f"<b>{_esc(len(scene_cards.get('perShot') or ()))}</b></div>")
        A("</div>")
    if outfits:
        A("<div class='box'><h3>造型（换装）</h3>")
        A(f"<div class='kv'><span>使用造型的镜头</span>"
          f"<b>{_esc(len(outfits.get('perShot') or ()))}</b></div>")
        _vs = sorted({str(v.get("variant") or "") for v in (outfits.get("perShot") or {}).values()
                      if isinstance(v, Mapping)})
        A(f"<div class='kv'><span>造型版本</span><b>{_esc('、'.join(_vs) or '—')}</b></div>")
        A("</div>")
    if post_plan:
        def _short(v: Any, n: int = 100) -> str:
            if isinstance(v, Mapping):
                parts = [f"{k}={_short(vv, 30)}" for k, vv in list(v.items())[:4]]
                s = "；".join(parts)
            elif isinstance(v, (list, tuple)):
                s = f"{len(v)} 项"
            else:
                s = str(v)
            return s[:n] + ("…" if len(s) > n else "")

        A("<div class='box'><h3>后期统筹</h3>")
        _pp = [k for k in ("audio", "subtitle", "loudness", "transitions")
               if k in post_plan]
        for k in _pp:
            A(f"<div class='kv'><span>{_esc(k)}</span>"
              f"<b>{_esc(_short(post_plan.get(k)))}</b></div>")
        A("</div>")
    if token_usage:
        A("<div class='box'><h3>Token / 模型用量</h3>")
        _tot = token_usage.get("total") or token_usage.get("totals") or token_usage
        if isinstance(_tot, Mapping):
            for k in ("promptTokens", "completionTokens", "calls"):
                if k in _tot:
                    A(f"<div class='kv'><span>{_esc(k)}</span><b>{_esc(_tot.get(k))}</b></div>")
        A("</div>")
    A("</div>")
    # 封面缩略图
    _covers = sorted(delivery.glob("cover*"))
    if _covers:
        A("<h2>封面</h2><div class='cols'>")
        for c in _covers[:2]:
            if c.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                A(f"<div class='box'><h3>{_esc(c.name)}</h3>"
                  f"<img class='thumb' loading='lazy' src='{_esc(_rel(c, outdir))}' "
                  f"alt='cover'></div>")
        A("</div>")

    # ---- 场景空间视图（Tier1 空景锚点 / Tier2 四视图，2026-11-17）----
    if scene_views and isinstance(scene_views.get("scenes"), Mapping) and scene_views["scenes"]:
        _sv = scene_views["scenes"]
        A("<h2>场景空间视图</h2>")
        A("<div class='box'><h3>档位由上游信号决定</h3>"
          "<div class='kv'><span>场景数</span>"
          f"<b>{_esc(len(_sv))}</b></div>"
          f"<div class='kv'><span>Tier2（四视图）</span><b>"
          f"{_esc(sum(1 for v in _sv.values() if int(v.get('tier') or 1) >= 2))}</b></div>"
          f"<div class='kv'><span>成本</span><b>¥{_esc(scene_views.get('costCny') or 0)}</b></div>"
          f"<div class='kv'><span>含人物警告（QC）</span><b>{_esc(scene_views.get('peopleWarnings') or 0)}</b></div>"
          "<div class='kv'><span>说明</span><b>Tier1=空景锚点；Tier2=+反向/45°/正俯"
          "（正俯只作空间草图，不回灌渲染）</b></div></div>")
        A("<div class='grid'>")
        for _sid, _sc in sorted(_sv.items()):
            if not isinstance(_sc, Mapping):
                continue
            A("<div class='card'><div class='hd'>")
            A(f"<span class='id'>{_esc(_sid)}</span>")
            A(f"<span class='tag {'ok' if int(_sc.get('tier') or 1) >= 2 else ''}'>"
              f"Tier{_esc(_sc.get('tier') or 1)}</span>")
            A(f"<span class='tag'>{_esc(len(_sc.get('anchors') or ()))} 锚点</span>")
            A("</div>")
            _imgs = [r for r in (_sc.get("images") or ())
                     if isinstance(r, Mapping) and r.get("ok") and r.get("out")]
            for _r in _imgs[:4]:
                _p = Path(str(_r.get("out")))
                if _p.is_file():
                    A(f"<img class='thumb' loading='lazy' "
                      f"src='{_esc(_rel(_p, outdir))}' alt='{_esc(_r.get('view'))}'>")
                    A(f"<div class='kv'><span>{_esc(_r.get('view'))}</span>"
                      f"<b>{_esc(_p.name)}</b></div>")
            _f = (_sc.get("decision") or {}).get("factors") or []
            A(f"<div class='kv'><span>决策</span><b>{_esc('；'.join(str(x) for x in _f))}</b></div>")
            A("</div>")
        A("</div>")

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
        if art is None:
            # 与合成器同一套兜底：名字不可靠，文件在 assets/shots/<sid>/ 里
            _sd = workdir / "assets" / "shots" / sid
            _hits = sorted(_sd.glob("*.png")) if _sd.is_dir() else []
            art = _hits[0] if _hits else None
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
