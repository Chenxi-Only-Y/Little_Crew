"""5 个核心职位的**端到端链路演示** —— 从剧本到镜头表。

跑什么
------
```
剧本素材
  ↓ 编剧（出稿，首稿故意留结构缺陷）
  ↓
剧本顾问 ‖ 文学策划   （并行双维审查）
  ↓
编排器合并（最严裁定 + 权威算分）
  ↓ 返工指令
编剧（修订）→ 复审 → 通过 → script.final
  ↓
分镜师 → shot.list
  ↓
审片（跨族盲读）→ review.verdict
```

**为什么首稿要故意留缺陷**：如果首稿就完美 PASS，
那验证的是"流程能跑"，而不是"协作机制有效"。
留一个规则可检出的缺陷，才能证明
**审查真的抓到了问题、返工指令真的驱动了修订、复检真的通过了**。

运行： python -m little_crew.agent.crew_demo
"""

from __future__ import annotations

import json
import sys

from little_crew.agent.crew_stubs import default_crew_stubs
from little_crew.agent.messages import Verdict
from little_crew.agent.orchestrator import CrewOrchestrator

#: 演示用剧本素材。用 `@角色` 标记出场人物，用 `[场N]` 切分场次。
DEMO_SOURCE = """[场1] 天台·夜
@林晚 站在天台边缘，手里攥着那封旧信。@陈默 从楼梯口上来，脚步声很轻。
林晚没有回头，只是把信攥得更紧了些。
陈默开口："那封信你看了。"
林晚沉默。雨开始下，打在铁皮上。
"我以为你会问我为什么。"陈默说。
林晚终于转身，把信递回去："我不想知道了。"
陈默没有接。信被雨打湿，字迹一点点化开。

[场2] 街道·深夜·雨
@林晚 走进雨里，没有打伞。@陈默 在身后喊了一声，她没有停。
街灯把她的影子拉得很长。她走到路口，停了一下。
陈默追上来，抓住她的手腕。她甩开了。
"你欠我一个解释。"
"我欠你的不止这个。"
林晚看着他，忽然笑了，笑得很难看。她转身走进巷子，消失在雨幕里。
陈默站在原地，很久没有动。"""


#: 第二个剧本夹具 —— **普罗旺斯薰衣草传说（5 场）**。
#:
#: 与 `DEMO_SOURCE`（天台·夜，单场）的分工：
#:   · `DEMO_SOURCE`        验证**协作闭环**（首稿留缺陷 -> 返工 -> 通过）
#:   · `DEMO_SOURCE_LAVENDER` 验证**多场次调度**（5 场、多角色、跨场一致性）
#:
#: 放在这里而不是 `runtime/` 下：它是**剧本夹具数据**，与
#: `DEMO_SOURCE` 同类。之前放在 `runtime/episode_demo.py`，
#: 导致 `runtime/p3_episode.py` 必须用 `sys.path` 操控去 import 它
#: —— 那违反了架构守卫的 R3。
DEMO_SOURCE_LAVENDER = """[场1] 普罗旺斯·山中·午后
@少女 独自走在山坡上采花，竹篮里已有半篮紫色花穗。
她忽然停住——前方的岩石边，@旅人 倒在那里，腿上带着伤，血把布条浸透了。
少女蹲下，伸手探他的额头，烫得吓人。
"你不能死在这里。"她说。她把披肩撕成布条，替他扎紧伤口。
旅人睁开眼，想说什么，却又昏了过去。

[场2] 少女家·夜
@父亲 把碗重重放在桌上，声音压得很低："他是外乡人。你留他，村里人怎么看你。"
@少女 没有低头，也没有辩解。她只是把药碗端起来，转身走向里屋。
@母亲 拉住父亲的袖子，摇了摇头。
那一夜少女守在床边，一整夜没合眼。代价是她从此再没有回过村子里的集会。

[场3] 村口·春
@旅人 的伤好了。他站在村口，望着通往北方的路，很久没有说话。
"我要回去了。"他说。
@少女 从屋里出来，身上只有一个小包袱。她看着他，忽然笑了："我跟你走。"
@老奶奶 从人群中走出来，把一束刚绽开的薰衣草塞进她手里。
"孩子，这花的香气能让不洁的东西现形。"老奶奶的手很凉，"记住。"

[场4] 山路·黎明
两人并肩走在山路上，风把花穗吹得轻轻颤。
@少女 忽然停下脚步。她低头看着手里的花束，又抬头看@旅人。
"你到底是谁？"
旅人没有回答。他只是笑，那个笑容她从前没见过。
少女举起花束，朝着他掷了过去——
花瓣散开的一瞬，@旅人 化作一阵紫色的轻烟，散了。
风里只剩下很低的一句话："我就是你想远行的心啊。"

[场5] 山路·晨
@少女 站在原地，手里什么也没有了。
她没有哭。她沿着那条路继续往前走，走进了晨雾里。
后来村里人说，再没有人见过她。
也有人说，她是循着花香，去寻他去了。
"""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("小剧组 · 5 个核心职位端到端链路（规则桩）")
    print("=" * 78)

    stubs = default_crew_stubs()
    model_families = {
        "screenwriter": "deepseek",
        "storyboard_artist": "deepseek",
        "dramaturge": "qwen",
        "story_editor": "qwen",
        "review_board": "qwen",
    }
    orch = CrewOrchestrator(model_families=model_families, max_rounds=3)
    orch.bind_many(stubs)

    # ---------- 第一段：剧本闭环 ----------
    print("\n【第一段】剧本：出稿 → 并行双维审查 → 合并 → 返工 → 通过")
    print("-" * 78)
    cycle = orch.run_script_cycle({
        "source.novel": DEMO_SOURCE,
        "episodeId": "E001",
        "title": "雨夜信",
    })

    for r in cycle.rounds:
        print(f"\n  第 {r.round_index} 轮")
        for o in r.outcomes:
            mark = "v" if o.ok else "X"
            print(f"    [{mark}] {o.agent_id:20} {o.stage:10} "
                  f"{o.seconds * 1000:6.1f} ms"
                  + ("" if o.ok else f"  <- {o.error}"))
        print(f"    裁定 {r.verdict.value:9} 权威总分 {r.total:6.1f}  "
              f"意见 {len(r.issues)} 条  返工指令 {len(r.repair_hints)} 条")
        if r.invalidated_reason:
            print(f"    ⚠️ 作废：{r.invalidated_reason}")
        for i in r.issues[:3]:
            print(f"      · [{i.severity.value:7}] {i.dimension.value:12} "
                  f"{i.problem[:52]}")
        if len(r.issues) > 3:
            print(f"      · ...另 {len(r.issues) - 3} 条")

    print(f"\n  结果：{'✅ 通过' if cycle.accepted else '❌ 未通过'}"
          f"（{cycle.round_count} 轮，{cycle.total_seconds * 1000:.0f} ms）")
    print(f"  说明：{cycle.reason}")

    if not cycle.accepted:
        print("\n  剧本阶段未通过，链路终止。")
        return 1

    # 编剧确实收到了返工指令
    sw = stubs["screenwriter"]
    print(f"\n  编剧被调用 {sw.calls} 次；"
          f"收到返工指令 {len(sw.received_critiques)} 次")
    if sw.received_critiques:
        c = sw.received_critiques[0]
        print(f"    第 1 次收到的意见数：{len(c.get('issues', []))}")
        print(f"    第 1 次收到的返工指令：{len(c.get('repairHints', []))}")

    # ---------- 第二段：分镜 ----------
    print("\n【第二段】分镜：定稿剧本 → 镜头表")
    print("-" * 78)
    out = orch.run_stage(
        "storyboard_artist", "shot.list",
        {"script.final": cycle.final})
    if not out.ok:
        print(f"  ❌ 分镜失败：{out.error}")
        return 1
    sl = orch.blackboard.value("shot.list")
    bd = orch.blackboard.value("shot.breakdown")
    print(f"  [v] 分镜师产出 {sl.shot_count} 个镜头，"
          f"总时长 {sl.total_seconds:.1f} 秒")
    if isinstance(bd, dict):
        print(f"    景别分布：{bd.get('bySize')}")
        print(f"    运镜分布：{bd.get('byMove')}")
    print("\n  前 4 个镜头：")
    for s in sl.shots[:4]:
        print(f"    {s.shot_id}  {s.shot_size:13} {s.camera_movement:11} "
              f"{s.action:6} {s.duration_sec:4.1f}s  {s.description[:34]}")

    # ---------- 第三段：审片 ----------
    print("\n【第三段】审片：跨族盲读，QC-5 裁定")
    print("-" * 78)
    reviews, why = orch.review_artifact(
        "shot.image", generator_id="cinematographer",
        payload={"shot.list": sl},
        reviewers=("review_board",))
    if why:
        print(f"  ⚠️ {why}")
    for fb in reviews:
        print(f"  {fb.frm}（{fb.reviewer_model}）审 {fb.generator_model}"
              f"  → {fb.verdict.value}")
        if fb.score:
            for dim, val in sorted(fb.score.dimensions.items(),
                                   key=lambda kv: kv[1]):
                print(f"      {dim.value:12} {val:6.1f}")
            print(f"      {'权威总分':12} {fb.score.total:6.1f}"
                  f"   （跨族={fb.cross_family_ok}，"
                  f"自报可信={fb.self_report_ok}）")

    # ---------- 汇总 ----------
    print("\n" + "=" * 78)
    print("调用树（无需额外埋点 —— 调用树就是执行轨迹）")
    print("=" * 78)
    for line in orch.trace():
        print("  " + line)

    snap = orch.snapshot()
    print("\n黑板内容：")
    for k, v in sorted(snap["blackboard"].items()):
        t = type(v).__name__
        print(f"  {k:22} {t}")
    print("\n产物归属：")
    for k, v in sorted(snap["ownership"].items()):
        print(f"  {k:22} <- {v}")

    print("\n" + "=" * 78)
    ok = cycle.accepted and sl.shot_count > 0 and bool(reviews)
    print(f"链路结果：{'✅ 全链路贯通' if ok else '❌ 有环节未通过'}")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
