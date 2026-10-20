"""**导演台**：把 19 个职位画成一张节点图（只读，SVG 手写，零依赖）。

## 这张图从哪来 —— 不需要手画

名册里每个职位都声明了 `produces` / `requires`。
把「谁产出什么」连到「谁需要什么」，**依赖图是自动生成的**：

```
19 职位 · 26 种产物 · 32 条依赖边
```

**加一个职位，图自动多一个块** —— 图不会和代码脱节。

## 为什么是"只读"

见对话记录里的判断：**我们的程序不是图**。
ComfyUI 的图是"真身"（图就是程序），我们的图只是"影子"。
改影子不改变真身 —— 所以改成"能拖"的编辑器收益极低（数周），
而"能看"的只读图半天就够，且回答了真正的问题：

> **现在到哪了？每个职位产出了什么？**

## 布局

按**部门分 8 列**（编剧 → 导演 → 美术 → 摄影 → 制片 → 剪辑 → 声音 → 审片），
列内纵向排。实测最长的一列 5 人，所以图高约 400px、宽约 1300px。

**32 条边全部画出来**，不省略 —— 省略会让人误以为没有依赖。

## 三种状态色，含义写死

| 颜色 | 含义 |
|---|---|
| ⚪ 虚框 | **职位未实现**（6 个：剪辑/摄影师/角色设计师/作曲/对白剪辑/合成剪辑） |
| 🟡 | 已实现但**本集未参与** |
| ✅ | 本集**有产出**（块上带计数） |
| 🔴 | 本集**失败** |

> 「未实现」用虚框**显式画出来**，而不是藏掉 ——
> 看图的人该知道"这条链上哪一段还是空的"。
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

__all__ = ["build_console", "CONSOLE_NAME"]

CONSOLE_NAME = "console.html"

#: 列的固定顺序（流程从左到右）。注意：这是**流程顺序**，不是名册里的声明顺序。
DEPARTMENTS: tuple[tuple[str, str], ...] = (
    ("SCREENPLAY", "编剧组"),
    ("DIRECTOR", "导演组"),
    ("ART", "美术组"),
    ("CAMERA", "摄影组"),
    ("PRODUCTION", "制片组"),
    ("EDITORIAL", "剪辑组"),
    ("SOUND", "声音组"),
    ("REVIEW", "审片组"),
)

#: 画布几何。数值是试出来的：块要能放下"四个字 + 计数"，列间距要够走折线。
#: 部门颜色（只用作块左侧的色条 —— 布局靠**依赖层级**，不靠部门）
DEPT_COLOR: dict[str, str] = {
    "SCREENPLAY": "#60a5fa", "DIRECTOR": "#f472b6", "ART": "#fbbf24",
    "CAMERA": "#34d399", "PRODUCTION": "#a78bfa", "EDITORIAL": "#22d3ee",
    "SOUND": "#fb923c", "REVIEW": "#f87171",
}

BOX_W, BOX_H = 172, 104   # 比第一版高/宽：底部要放一行缩略图
THUMB, THUMB_GAP, THUMB_MAX = 30, 4, 4   # 块里最多放 4 张，更多用 +N 表示
COL_GAP, ROW_GAP = 62, 20
PAD_X, PAD_Y = 26, 30


def _layers(nodes: list[dict[str, Any]],
            edges: list[tuple[str, str, str]]) -> dict[str, int]:
    """按**最长路径**给每个节点分层（列号）。

    为什么按依赖分层，而不是按部门：
    部门是**组织**，数据流是**依赖**，两者不一致 ——
    实测按部门分列时，32 条边里有 **12 条是往回走或同列**（37%），
    图会横穿、同列内还会互相压线。

    部门不消失，改成**块上的颜色条**，仍能看出"这是哪个组的"。
    """
    ids = [n["id"] for n in nodes]
    layer = {i: 0 for i in ids}
    # 最长路径：反复松弛直到稳定（19 个节点，O(n*e) 完全够）
    for _ in range(len(ids) + 1):
        changed = False
        for src, dst, _p in edges:
            if src in layer and dst in layer:
                if layer[dst] < layer[src] + 1:
                    layer[dst] = layer[src] + 1
                    changed = True
        if not changed:
            break
    return layer


def _dep_key(d: Any) -> str:
    return str(d).split(".")[-1]


def _graph() -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """从名册生成节点与边。**不手写任何一条依赖。**"""
    from little_crew.agent.roster import DEFAULT_ROSTER

    prod: dict[str, list[str]] = {}
    for s in DEFAULT_ROSTER:
        for p in s.produces:
            prod.setdefault(p, []).append(s.agent_id)

    nodes: list[dict[str, Any]] = []
    for s in DEFAULT_ROSTER:
        nodes.append({
            "id": s.agent_id,
            "title": s.crew_title,
            "dept": _dep_key(s.department),
            "exec": str(s.execution).split(".")[-1],
            "produces": list(s.produces),
            "requires": list(s.requires),
            "duty": (getattr(s, "real_world_duty", "") or "")[:120],
        })
    edges: list[tuple[str, str, str]] = []
    for s in DEFAULT_ROSTER:
        for r in s.requires:
            for src in prod.get(r, []):
                edges.append((src, s.agent_id, r))
    return nodes, edges


def _episode_state(workdir: Path) -> dict[str, Any]:
    """从产物推断"这一集里每个职位干了什么"。

    第一版**不改 runner** —— 全部从文件推断。将来 runner 上报后，这里变成兜底。
    """
    delivery = workdir / "delivery"
    rep: dict[str, Any] = {}
    man: dict[str, Any] = {}
    try:
        rep = json.loads((delivery / "run.report.json").read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        pass
    try:
        man = json.loads((delivery / "episode.master.json")
                         .read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        pass

    shots = list(man.get("shots") or [])
    pv_dirs = [d for d in (workdir / "previs").glob("*") if d.is_dir()]
    with_ctrl = [d for d in pv_dirs if (d / "previs_control.png").is_file()]
    pv = rep.get("previs") or {}
    ok_art = sum(1 for s in shots if (s.get("artifacts") or []))

    return {
        "episodeId": rep.get("episodeId") or man.get("episodeId") or "",
        "title": man.get("title") or "",
        "ok": rep.get("ok"),
        "scriptRounds": rep.get("scriptRounds"),
        "scriptVerdict": rep.get("scriptVerdict"),
        "shotCount": man.get("shotCount") or len(shots),
        "totalSeconds": rep.get("totalSeconds") or man.get("totalSeconds"),
        "wallSeconds": rep.get("wallSeconds"),
        "coverage": rep.get("coverage"),
        "rendered": rep.get("rendered"),
        "cacheHits": rep.get("cacheHits"),
        "director": bool(rep.get("director")),
        "art": bool(rep.get("art")),
        "gaffer": bool(rep.get("gaffer")),
        "schedule": bool(rep.get("schedule")),
        "previsOk": pv.get("ok"),
        "previsShots": len(pv_dirs),
        "previsCtrl": len(with_ctrl),
        "artifacts": ok_art,
        "failures": len(rep.get("failures") or []),
        "master": (delivery / "episode.master.mp4").is_file(),
        "ready": bool(shots) and all(s.get("ready") for s in shots),
    }


def _badge(agent: str, st: dict[str, Any]) -> tuple[str, str]:
    """返回 (状态类, 计数文字)。状态类：ok / idle / todo / bad。"""
    sc = st.get("shotCount") or 0
    if agent == "screenwriter":
        return ("ok", f"{st['scriptRounds']} 轮") if st.get("scriptRounds") else ("idle", "未参与")
    if agent == "dramaturge":
        return ("ok", str(st["scriptVerdict"])) if st.get("scriptVerdict") else ("idle", "未参与")
    if agent == "storyboard_artist":
        return ("ok", f"{sc} 镜") if sc else ("idle", "未参与")
    if agent == "director":
        return ("ok", "已定调") if st.get("director") else ("idle", "未参与")
    if agent == "production_designer":
        return ("ok", "已定场景") if st.get("art") else ("idle", "未参与")
    if agent == "gaffer":
        return ("ok", "已定光影") if st.get("gaffer") else ("idle", "未参与")
    if agent == "previs":
        n = st.get("previsShots") or 0
        return ("ok" if n else "idle", f"{n}/{sc}") if n else ("idle", "未参与")
    if agent == "cinematographer":
        n = st.get("artifacts") or 0
        return ("ok", f"{n}/{sc}") if n else ("idle", "未参与")
    if agent == "line_producer":
        return ("ok", "已排期") if st.get("schedule") else ("idle", "未参与")
    if agent in ("compliance_officer", "review_board"):
        return ("ok", "已核") if st.get("ok") else ("idle", "未参与")
    if agent == "continuity_supervisor":
        return ("ok", "在岗") if sc else ("idle", "未参与")
    return ("idle", "未参与")


def _artifacts_for(agent: str, workdir: Path,
                   shots: list[dict[str, Any]]) -> list[tuple[str, Path]]:
    """某个职位**这一集真正产出的图片/视频**。

    只收**能看见的**（图/视频）—— 文本产物（剧本、方案）不进缩略图，
    因为一行缩略图里放一段文字没有信息量。
    """
    out: list[tuple[str, Path]] = []
    if agent == "previs":
        for d in sorted((workdir / "previs").glob("*")):
            if not d.is_dir():
                continue
            f = d / "previs_control.png"
            if not f.is_file():
                f = d / "previs_first.png"
            if f.is_file():
                out.append((d.name, f))
    elif agent == "cinematographer":
        from little_crew.runtime.report_html import resolve_artifact

        for s in shots:
            arts = s.get("artifacts") or []
            if not arts:
                continue
            f = resolve_artifact(workdir, arts[0])
            if f is not None:
                out.append((str(s.get("shotId") or ""), f))
    elif agent == "assembly_editor":
        m = workdir / "delivery" / "episode.master.mp4"
        if m.is_file():
            out.append(("成片", m))
    return out



EDIT_JS_SRC = """
let CUR=null,CUR_RES=null,CUR_FPS=null;
async function openShot(sid){
  const r=await fetch('/api/shot/'+encodeURIComponent(sid));
  const j=await r.json();
  const out=document.getElementById('ed-out');
  if(!j.ok){out.textContent='读不到：'+j.reason;return;}
  CUR=sid;const s=(j.layout&&j.layout.shot)||{};
  document.getElementById('editor').style.display='block';
  document.getElementById('ed-sid').textContent=sid;
  document.getElementById('ed-size').value=s.shot_size||'medium';
  document.getElementById('ed-move').value=s.camera_movement||'static';
  document.getElementById('ed-az').value=s.azimuth_deg==null?0:s.azimuth_deg;
  document.getElementById('ed-ang').value=s.angle_deg==null?0:s.angle_deg;
  document.getElementById('ed-dur').value=s.duration_sec==null?3:s.duration_sec;
  const sp=j.spec||{};
  CUR_RES=(sp.resolution&&sp.resolution.width)?[sp.resolution.width,sp.resolution.height]:null;
  CUR_FPS=sp.fps||null;
  out.textContent='已载入 '+sid;
}
document.getElementById('ed-go').addEventListener('click',async()=>{
  const out=document.getElementById('ed-out');
  if(!CUR){out.textContent='先点一个镜头';return;}
  out.textContent='重渲中…（约 15~30 秒）';
  const body={shot_size:document.getElementById('ed-size').value,
    camera_movement:document.getElementById('ed-move').value,
    azimuth_deg:Number(document.getElementById('ed-az').value),
    angle_deg:Number(document.getElementById('ed-ang').value),
    duration_sec:Number(document.getElementById('ed-dur').value),
    resolution:CUR_RES,fps:CUR_FPS};
  const r=await fetch('/api/shot/'+encodeURIComponent(CUR),
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(!j.ok){out.innerHTML='<b style="color:#f87171">失败</b> '+(j.reason||'')
    +((j.blockers&&j.blockers.length)?'<br>'+j.blockers.map(x=>x.code+': '+x.detail).join('<br>'):'');return;}
  const rr=j.render||{};
  out.innerHTML='<b style="color:#4ade80">完成</b> 判定 '+j.verdict
    +(rr.ok?' · 帧 '+rr.frames:' · 渲染失败 '+(rr.reason||''))
    +((j.majors&&j.majors.length)?'<br><span style="color:#fbbf24">提示 '+j.majors.map(m=>m.code).join(' / ')+'</span>':'')
    +'<br><span style="color:#6f7a8a">刷新页面可见新缩略图</span>';
});
"""

def build_console(workdir: Path, *, editable: bool = False) -> Path:
    """生成 `<workdir>/console.html`。

    `editable=True` 只在**通过 `studio.py` 的本地服务**打开时启用 ——
    `file://` 不能发 POST，所以静态打开永远只读。

    """
    """生成 `<workdir>/console.html`（含 SVG 节点图）。"""
    workdir = Path(workdir)
    nodes, edges = _graph()
    st = _episode_state(workdir)
    by_id = {n["id"]: n for n in nodes}

    # ---- 布局：按**依赖层级**分列，部门只作颜色 ----
    layer = _layers(nodes, edges)
    by_layer: dict[int, list[dict[str, Any]]] = {}
    for n in nodes:
        by_layer.setdefault(layer.get(n["id"], 0), []).append(n)
    # 同层内按部门排，让同组的块挨着（视觉上仍能看出组织）
    dept_rank = {k: i for i, (k, _) in enumerate(DEPARTMENTS)}
    for k in by_layer:
        by_layer[k].sort(key=lambda n: (dept_rank.get(n["dept"], 99), n["id"]))
    pos: dict[str, tuple[int, int]] = {}
    n_layers = max(by_layer) + 1 if by_layer else 1
    for li in range(n_layers):
        for ri, n in enumerate(by_layer.get(li, [])):
            pos[n["id"]] = (PAD_X + li * (BOX_W + COL_GAP),
                            PAD_Y + ri * (BOX_H + ROW_GAP))
    width = PAD_X * 2 + n_layers * BOX_W + (n_layers - 1) * COL_GAP
    max_row = max((len(v) for v in by_layer.values()), default=1)
    height = PAD_Y * 2 + max_row * BOX_H + (max_row - 1) * ROW_GAP + 34

    implemented = {n["id"] for n in nodes
                   if st.get("shotCount") or True}  # 占位，下面按"真实实现"重算
    # 真实实现 = 有 LLM 实现 或 有规则桩（用 stubs + 已知实现名单判）
    try:
        from little_crew.agent.crew_stubs import default_crew_stubs

        impl = set(default_crew_stubs())
    except Exception:                                        # noqa: BLE001
        impl = set()
    try:
        from little_crew.agent.critic_llm import build_critic_crew

        impl |= set(build_critic_crew())
    except Exception:                                        # noqa: BLE001
        pass
    impl |= {"director", "gaffer", "previs", "production_designer",
             "line_producer"}

    # 每个职位这一集的可见产物（缩略图用）
    from little_crew.runtime.report_html import _rel as _mkrel

    try:
        _man = json.loads((workdir / "delivery" / "episode.master.json")
                          .read_text(encoding="utf-8"))
        shots_all = list(_man.get("shots") or [])
    except Exception:                                    # noqa: BLE001
        shots_all = []
    arts_by_agent: dict[str, list[tuple[str, str]]] = {}
    for n in nodes:
        got = _artifacts_for(n["id"], workdir, shots_all)
        arts_by_agent[n["id"]] = [(lbl, _mkrel(f, workdir)) for lbl, f in got]

    # ---- SVG ----
    s: list[str] = []
    A = s.append
    A(f"<svg viewBox='0 0 {width} {height}' width='{width}' height='{height}' "
      f"xmlns='http://www.w3.org/2000/svg'>")
    A("<defs><marker id='ah' viewBox='0 0 10 10' refX='9' refY='5' "
      "markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
      "<path d='M0,0 L10,5 L0,10 z' fill='#3c4452'/></marker></defs>")

    # 层表头
    for li in range(n_layers):
        x = PAD_X + li * (BOX_W + COL_GAP) + BOX_W / 2
        A(f"<text x='{x}' y='{PAD_Y - 12}' text-anchor='middle' "
          f"font-size='11' fill='#6f7a8a' letter-spacing='.06em'>"
          f"第 {li + 1} 层</text>")

    # 边（先画，压在块下面）
    for src, dst, product in edges:
        if src not in pos or dst not in pos:
            continue
        sx, sy = pos[src]
        dx, dy = pos[dst]
        x1, y1 = sx + BOX_W, sy + BOX_H / 2
        x2, y2 = dx, dy + BOX_H / 2
        if x2 > x1:
            mx = (x1 + x2) / 2
            d = f"M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}"
        else:
            # 反向边（同列或回连）：从上方绕
            d = (f"M{x1},{y1} C{x1 + 34},{y1} {x2 - 34},{y2} {x2},{y2}")
        A(f"<path d='{d}' fill='none' stroke='#333b47' stroke-width='1.2' "
          f"marker-end='url(#ah)'><title>{html.escape(product)}</title></path>")

    # 块
    for n in nodes:
        x, y = pos[n["id"]]
        cls, count = _badge(n["id"], st)
        has_impl = n["id"] in impl
        if not has_impl:
            cls, count = "todo", "未实现"
        stroke = {"ok": "#4ade80", "idle": "#3c4452", "todo": "#5b6472",
                  "bad": "#f87171"}[cls]
        fill = {"ok": "#17301f", "idle": "#1d2026", "todo": "#191c22",
                "bad": "#301a1a"}[cls]
        dash = " stroke-dasharray='5 4'" if cls == "todo" else ""
        op = " opacity='.55'" if cls == "todo" else ""
        A(f"<g class='node' data-id='{html.escape(n['id'])}'{op}>")
        A(f"<rect x='{x}' y='{y}' width='{BOX_W}' height='{BOX_H}' rx='7' "
          f"fill='{fill}' stroke='{stroke}'{dash}/>")
        A(f"<rect x='{x}' y='{y + 6}' width='3' height='{BOX_H - 12}' rx='1.5' "
          f"fill='{DEPT_COLOR.get(n['dept'], '#3c4452')}'/>")
        A(f"<text x='{x+10}' y='{y+22}' font-size='13' font-weight='600' "
          f"fill='#e6e8ec'>{html.escape(n['title'])}</text>")
        A(f"<text x='{x+10}' y='{y+40}' font-size='11' fill='{stroke}'>"
          f"{html.escape(count)}</text>")
        A(f"<text x='{x+BOX_W-10}' y='{y+27}' font-size='9' fill='#6f7a8a' "
          f"text-anchor='end'>{html.escape(n['exec'])}</text>")
        # ---- 块内缩略图 ----
        arts = arts_by_agent.get(n["id"], [])
        for ti, (_lbl, relsrc) in enumerate(arts[:THUMB_MAX]):
            tx = x + 9 + ti * (THUMB + THUMB_GAP)
            ty = y + BOX_H - THUMB - 8
            A(f"<image href='{html.escape(relsrc)}' x='{tx}' y='{ty}' "
              f"width='{THUMB}' height='{THUMB}' preserveAspectRatio='xMidYMid slice' "
              f"clip-path='inset(0 round 3)' opacity='.92'/>")
            A(f"<rect x='{tx}' y='{ty}' width='{THUMB}' height='{THUMB}' rx='3' "
              f"fill='none' stroke='#0b0d11' stroke-width='1'/>")
        if len(arts) > THUMB_MAX:
            A(f"<text x='{x+9+THUMB_MAX*(THUMB+THUMB_GAP)}' "
              f"y='{y+BOX_H-8-THUMB/2+4}' font-size='10' fill='#9aa3b2'>"
              f"+{len(arts)-THUMB_MAX}</text>")
        A("</g>")
    A("</svg>")

    # ---- 详情数据（内联进页面，点击不用请求）----
    detail: dict[str, Any] = {}
    for n in nodes:
        cls, count = _badge(n["id"], st)
        detail[n["id"]] = {
            "title": n["title"], "dept": n["dept"], "exec": n["exec"],
            "state": ("未实现" if n["id"] not in impl else count),
            "duty": n["duty"],
            "produces": n["produces"], "requires": n["requires"],
            "arts": [{"label": lbl, "src": src}
                     for lbl, src in arts_by_agent.get(n["id"], [])],
            # 只有"一镜一图"的职位能在图上改（改的是那一镜的布局）
            "canEdit": n["id"] in ("previs", "cinematographer"),
        }

    ok = st.get("ok")
    kpis = "".join(
        f"<div class='kpi'><div class='v'>{html.escape(str(v))}</div>"
        f"<div class='k'>{html.escape(k)}</div></div>"
        for v, k in (
            (st.get("shotCount") or 0, "镜头"),
            (f"{st.get('totalSeconds') or 0}s", "成片"),
            (f"{round(float(st.get('wallSeconds') or 0))}s", "墙钟"),
            (st.get("coverage") or "—", "覆盖率"),
            (f"{st.get('previsCtrl') or 0}/{st.get('shotCount') or 0}", "白模控制图"),
            (f"{st.get('artifacts') or 0}/{st.get('shotCount') or 0}", "出图"),
        ))

    edit_js = EDIT_JS_SRC if editable else ""
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>导演台 · {html.escape(st.get('title') or st.get('episodeId') or '')}</title>
<style>
:root{{--bg:#14161a;--card:#1d2026;--line:#2c313a;--fg:#e6e8ec;--dim:#9aa3b2;
--ok:#4ade80;--accent:#7dd3fc}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 ui-sans-serif,system-ui,"Segoe UI",Microsoft YaHei,sans-serif}}
header{{padding:16px 24px;border-bottom:1px solid var(--line)}}
h1{{margin:0 0 4px;font-size:19px;font-weight:650}}
.sub{{color:var(--dim);font-size:12.5px}}
.kpis{{display:flex;flex-wrap:wrap;gap:9px;margin-top:12px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:7px 12px;min-width:92px}}
.kpi .v{{font-size:16px;font-weight:650}}
.kpi .k{{color:var(--dim);font-size:11px}}
.wrap{{display:flex;gap:0;align-items:flex-start}}
.canvas{{flex:1;overflow:auto;padding:8px 0 40px;min-width:0}}
svg{{display:block}}
.node{{cursor:pointer}}
.node:hover rect{{filter:brightness(1.28)}}
.node.sel rect{{stroke:#7dd3fc !important;stroke-width:2.2}}
aside{{width:320px;flex:none;border-left:1px solid var(--line);
padding:16px 18px;min-height:520px;position:sticky;top:0}}
aside h2{{margin:0 0 4px;font-size:15px}}
aside .m{{color:var(--dim);font-size:12px;margin-bottom:12px}}
aside dl{{margin:0}}
aside dt{{color:var(--dim);font-size:11px;margin-top:10px;
letter-spacing:.05em;text-transform:uppercase}}
aside dd{{margin:2px 0 0;font-size:12.5px;word-break:break-word}}
aside dd.arts{{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:4px}}
aside dd.arts a{{display:block;text-decoration:none;color:#8b95a5;font-size:9.5px}}
aside dd.arts img{{width:100%;aspect-ratio:4/3;object-fit:cover;
border-radius:4px;border:1px solid var(--line);display:block;background:#0f1115}}
aside dd.arts a:hover img{{border-color:var(--accent)}}
code{{background:#0f1115;border:1px solid var(--line);border-radius:4px;
padding:1px 5px;font-size:11.5px}}
aside .row{{display:flex;align-items:center;gap:8px;margin:5px 0}}
aside .row label{{width:52px;color:var(--dim);font-size:11.5px}}
aside .row input,aside .row select{{flex:1;background:#0f1115;color:var(--fg);
border:1px solid var(--line);border-radius:5px;padding:4px 7px;font-size:12px}}
aside button{{margin-top:8px;width:100%;background:#1f3b52;color:#cfe9ff;
border:1px solid #2d5a78;border-radius:6px;padding:7px;font-size:12.5px;cursor:pointer}}
aside button:hover{{background:#264a66}}
.legend{{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;
color:var(--dim);font-size:11.5px}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:2px;
margin-right:5px;vertical-align:middle}}
</style></head><body>
<header>
<h1>导演台 · {html.escape(st.get('title') or '未命名')}
 <span class="sub">· {html.escape(str(st.get('episodeId') or ''))}</span></h1>
<div class="sub">判定：<b style="color:{'var(--ok)' if ok else '#f87171'}">
{'通过' if ok else '未通过'}</b> · 剧本 {html.escape(str(st.get('scriptRounds') or '?'))} 轮
 · 19 个职位 · 32 条依赖（自动生成自名册）</div>
<div class="kpis">{kpis}</div>
<div class="legend">
 <span><i class="dot" style="background:#17301f;border:1px solid #4ade80"></i>本集有产出</span>
 <span><i class="dot" style="background:#1d2026;border:1px solid #3c4452"></i>已实现·未参与</span>
 <span><i class="dot" style="background:#191c22;border:1px dashed #5b6472"></i>职位未实现</span>
 <span>点方块看详情 · 悬停连线看交付物</span>
</div>
</header>
<div class="wrap">
<div class="canvas">{''.join(s)}</div>
<aside id="side"><h2>点一个方块</h2>
<div class="m">右栏显示它的职责、要什么、产出什么。</div>
<dl id="dl"></dl>
<div id="editor" style="display:none;margin-top:16px;border-top:1px solid var(--line);padding-top:12px">
  <div style="font-size:12px;color:var(--dim);margin-bottom:6px">
    编辑这一镜（<span id="ed-sid"></span>）</div>
  <div class="row"><label>景别</label><select id="ed-size">
    <option>extreme_wide</option><option>wide</option><option>full</option>
    <option>medium_full</option><option>medium</option>
    <option>medium_close</option><option>close</option>
    <option>extreme_close</option></select></div>
  <div class="row"><label>运镜</label><select id="ed-move">
    <option>static</option><option>dolly_in</option><option>dolly_out</option>
    <option>truck_left</option><option>truck_right</option></select></div>
  <div class="row"><label>机位°</label><input id="ed-az" type="number" step="1"></div>
  <div class="row"><label>俯仰°</label><input id="ed-ang" type="number" step="1"></div>
  <div class="row"><label>时长s</label><input id="ed-dur" type="number" step="0.5"></div>
  <button id="ed-go">保存并只重渲这一镜</button>
  <div id="ed-out" class="m" style="margin-top:8px"></div>
</div></aside>
</div>
<script>
const EDITABLE={'true' if editable else 'false'};
const D={json.dumps(detail, ensure_ascii=False)};
const dl=document.getElementById('dl');
document.querySelectorAll('.node').forEach(g=>{{
  g.addEventListener('click',()=>{{
    document.querySelectorAll('.node.sel').forEach(x=>x.classList.remove('sel'));
    g.classList.add('sel');
    const d=D[g.dataset.id]||{{}};
    const L=(a)=>Array.isArray(a)?(a.length?a.map(x=>`<code>${{x}}</code>`).join(' '):'—'):'—';
    dl.innerHTML=`
      <dt>职位</dt><dd><b>${{d.title||''}}</b>　<span style="color:#6f7a8a">${{d.dept||''}}</span></dd>
      <dt>本集状态</dt><dd>${{d.state||'—'}}</dd>
      <dt>执行方式</dt><dd>${{d.exec||'—'}}</dd>
      <dt>真实职责</dt><dd>${{d.duty||'—'}}</dd>
      <dt>需要（requires）</dt><dd>${{L(d.requires)}}</dd>
      <dt>产出（produces）</dt><dd>${{L(d.produces)}}</dd>
      ${{(d.arts&&d.arts.length)?`
        <dt>这一集的产物（${{d.arts.length}}）</dt>
        <dd class="arts">${{
          d.arts.map(a=>`<a ${{d.canEdit&&EDITABLE
              ? `href="#" onclick="openShot('${{a.label}}');return false"` 
              : `href="${{a.src}}" target="_blank" rel="noopener"`}}
             title="${{a.label}}"><img loading="lazy" src="${{a.src}}"
             alt="${{a.label}}"><span>${{a.label.replace(/^SH-E[0-9]+-/,'')}}</span></a>`).join('')
        }}</dd>`:''}}
  }});
}});
{edit_js}
</script></body></html>"""
    out = workdir / CONSOLE_NAME
    out.write_text(page, encoding="utf-8")
    return out
