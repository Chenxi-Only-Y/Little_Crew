"""三层调用协议自检 —— 证明 R1-R4 规则与三个原语真实可用。

零第三方依赖： python -m little_crew.agent.selfcheck
"""

from __future__ import annotations

import sys

from little_crew.agent.protocol import (
    AgentDecision,
    AgentManifest,
    Blackboard,
    BlackboardConflict,
    BoundedReasoningLoop,
    Budget,
    BudgetExceeded,
    CallEnvelope,
    CallResult,
    CapabilityRegistry,
    DepthExceeded,
    Layer,
    LayerRuntime,
    LayerViolation,
    SideEffect,
    ToolSpec,
)

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, PASS if cond else FAIL, detail))


def expect_raises(name: str, exc: type[BaseException], fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc as e:
        check(name, True, f"{exc.__name__}: {str(e)[:90]}")
    except BaseException as e:  # noqa: BLE001
        check(name, False, f"期望 {exc.__name__}，实得 {type(e).__name__}: {e}")
    else:
        check(name, False, f"期望 {exc.__name__}，但未抛错")


# ==========================================================================
# 场景搭建：一个 L1 + 两个 L2 + 四个 L3 的最小可用系统
# ==========================================================================

def build() -> tuple[CapabilityRegistry, LayerRuntime]:
    reg = CapabilityRegistry()

    # ---------- L1 决策层 ----------
    reg.register_agent(AgentManifest(
        agent_id="master_director",
        layer=Layer.DECISION,
        capabilities=("episode.direct",),
        consumes=("shot.plan", "shot.produce"),      # 只能消费 L2 能力
        max_budget=Budget(tokens=200_000, cny=50.0, wall_ms=3_600_000, steps=40),
        max_reasoning_steps=8,
    ))

    # ---------- L2 协作层 ----------
    reg.register_agent(AgentManifest(
        agent_id="shot_composer",
        layer=Layer.COLLAB,
        capabilities=("shot.plan",),
        consumes=("llm.complete", "ir.build"),        # 可消费 L3 工具
        max_budget=Budget(tokens=50_000, cny=5.0, wall_ms=600_000, steps=10),
        max_reasoning_steps=6,
    ))
    reg.register_agent(AgentManifest(
        agent_id="image_gen",
        layer=Layer.COLLAB,
        capabilities=("shot.produce",),
        consumes=("comfy.submit", "gate.identity"),
        max_budget=Budget(tokens=2_000, cny=2.0, wall_ms=900_000, steps=8),
        max_reasoning_steps=4,
    ))

    # ---------- L3 执行层（必须确定性：无 LLM、无自由判断） ----------
    reg.register_tool(ToolSpec(
        name="llm.complete",
        input_schema={"prompt": "string"}, output_schema={"text": "string"},
        side_effect=SideEffect.EXTERNAL, idempotent=True,
        est_cost_cny=0.021, est_ms=2400,
        idempotency_key_field="prompt",
    ))
    reg.register_tool(ToolSpec(
        name="ir.build",
        input_schema={"shots": "array"}, output_schema={"graph": "object"},
        side_effect=SideEffect.PURE, est_cost_cny=0.0, est_ms=15,
    ))
    reg.register_tool(ToolSpec(
        name="comfy.submit",
        input_schema={"graph": "object"}, output_schema={"prompt_id": "string"},
        side_effect=SideEffect.EXTERNAL, idempotent=True,
        est_cost_cny=0.0, est_ms=42_000, requires_gpu=True,
        idempotency_key_field="graph",
    ))
    reg.register_tool(ToolSpec(
        name="gate.identity",
        input_schema={"artifact": "string"}, output_schema={"passed": "boolean"},
        side_effect=SideEffect.PURE, est_cost_cny=0.0, est_ms=180,
    ))

    return reg, LayerRuntime(reg, Blackboard())


def bind_impls(rt: LayerRuntime) -> dict[str, int]:
    """绑定实现。返回调用计数器，便于验证次数。"""
    calls = {"llm": 0, "comfy": 0}

    # ---- L3 实现 ----
    def t_llm(prompt: str):
        calls["llm"] += 1
        return {"text": f"[剧本] {prompt[:24]}"}

    def t_ir(shots):
        return {"graph": {"irVersion": "little_crew.ir/v1", "nodes": shots}}

    def t_comfy(graph):
        calls["comfy"] += 1
        return {"prompt_id": f"pid_{calls['comfy']:04d}"}

    def t_gate(artifact):
        return {"passed": True, "artifact": artifact}

    rt.bind_tool("llm.complete", t_llm)
    rt.bind_tool("ir.build", t_ir)
    rt.bind_tool("comfy.submit", t_comfy)
    rt.bind_tool("gate.identity", t_gate)

    # ---- L2 实现：shot_composer 用有界推理循环 ----
    def a_shot_composer(envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult:
        """轴 B：有界推理循环。decide 只返回下一步，循环由运行时驱动。"""
        script: list[str] = []
        loop = BoundedReasoningLoop(
            max_steps=envelope.budget.steps or 6, budget=envelope.budget
        )

        def decide(env: CallEnvelope, steps) -> AgentDecision:
            if len(steps) == 0:
                return AgentDecision(
                    action="llm.complete", thought="先写分场剧本",
                    action_input={"prompt": str(env.payload.get("beat", ""))},
                )
            if len(steps) == 1:
                script.append(str(steps[0].observation))
                return AgentDecision(
                    action="ir.build", thought="把剧本编译成 IR",
                    action_input={"shots": list(script)},
                )
            return AgentDecision(action="finish", final_output={"ir": script})

        result, trace = loop.run(envelope, runtime, decide)
        result.evidence.append({
            "span": envelope.span_id,
            "reasoningSteps": [
                {"i": s.index, "thought": s.thought, "action": s.action,
                 "obs": str(s.observation)[:60]}
                for s in trace
            ],
        })
        return result

    # ---- L2 实现：image_gen 同样用有界循环 ----
    def a_image_gen(envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult:
        graph = envelope.payload.get("graph", {})
        loop = BoundedReasoningLoop(
            max_steps=envelope.budget.steps or 4, budget=envelope.budget
        )

        def decide(env: CallEnvelope, steps) -> AgentDecision:
            if len(steps) == 0:
                return AgentDecision(action="comfy.submit", thought="提交渲染",
                                     action_input={"graph": graph})
            if len(steps) == 1:
                pid = (steps[0].observation or {}).get("prompt_id")
                return AgentDecision(action="gate.identity", thought="硬门禁校验身份",
                                     action_input={"artifact": pid})
            return AgentDecision(action="finish",
                                 final_output={"promptId": steps[0].observation})

        result, trace = loop.run(envelope, runtime, decide)
        if result.ok and trace and isinstance(trace[-1].observation, dict):
            result.evidence.append({
                "assertion": "identity_similarity", "op": "hard",
                "passed": bool(trace[-1].observation.get("passed")),
            })
        return result

    # ---- L1 实现：MasterDirector ----
    def a_master_director(envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult:
        beat = str(envelope.payload.get("beat", ""))
        total = Budget()

        plan_env = envelope.child(
            callee="shot_composer", capability="shot.plan",
            payload={"beat": beat},
            budget=Budget(tokens=30_000, cny=3.0, wall_ms=300_000, steps=6),
        )
        plan_res = runtime.call_agent(plan_env)
        total = Budget(cny=round(total.cny + plan_res.spent.cny, 6),
                       wall_ms=total.wall_ms + plan_res.spent.wall_ms,
                       steps=total.steps + 1)
        if not plan_res.ok:
            return CallResult.fail(plan_res.error_code or "F-PLAN",
                                   plan_res.error_message or "", spent=total,
                                   escalate=True)

        prod_env = envelope.child(
            callee="image_gen", capability="shot.produce",
            payload={"graph": plan_res.output},
            budget=Budget(tokens=1_500, cny=1.5, wall_ms=800_000, steps=5),
        )
        prod_res = runtime.call_agent(prod_env)
        total = Budget(cny=round(total.cny + prod_res.spent.cny, 6),
                       wall_ms=total.wall_ms + prod_res.spent.wall_ms,
                       steps=total.steps + 1)
        if not prod_res.ok:
            return CallResult.fail(prod_res.error_code or "F-PROD",
                                   prod_res.error_message or "", spent=total,
                                   escalate=True)

        return CallResult(
            ok=True,
            output={"plan": plan_res.output, "production": prod_res.output},
            spent=total,
            evidence=plan_res.evidence + prod_res.evidence,
            children=[plan_env.span_id, prod_env.span_id],
        )

    rt.bind_agent("master_director", a_master_director)
    rt.bind_agent("shot_composer", a_shot_composer)
    rt.bind_agent("image_gen", a_image_gen)
    return calls


# ==========================================================================
# 测试
# ==========================================================================

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    reg, rt = build()
    calls = bind_impls(rt)

    print("=" * 74)
    print("三层智能体调用协议 —— 自检")
    print("=" * 74)

    # ---------- T1 静态审计 ----------
    problems = reg.audit()
    check("T1 能力注册表静态审计无问题", problems == [], str(problems))

    desc = reg.describe()
    check("T1 分层登记正确",
          desc["agents"]["DECISION"] == ["master_director"]
          and len(desc["agents"]["COLLAB"]) == 2
          and len(desc["tools"]) == 4,
          str(desc["agents"]))

    # ---------- T2 端到端正常调用 ----------
    result, env = rt.invoke(
        "episode.direct",
        {"beat": "林晚在雨中的天台回头"},
        budget=Budget(tokens=100_000, cny=20.0, wall_ms=1_800_000, steps=20),
    )
    check("T2 顶层调用成功", result.ok, f"err={result.error_code}")
    check("T2 产物含 promptId",
          isinstance(result.output, dict) and "promptId" in str(result.output),
          str(result.output)[:110])
    check("T2 证据链非空", len(result.evidence) >= 2, f"evidence={len(result.evidence)}")
    check("T2 预算累加正确", result.spent.cny > 0 and result.spent.steps == 2,
          f"spent={result.spent.as_dict()}")

    # ---------- T3 调用树可观测 ----------
    tree = rt.trace_tree()
    check("T3 调用树可渲染", len(tree) >= 5, f"{len(tree)} 行")
    check("T3 调用树深度递增",
          any("d=1" in ln for ln in tree) and any("d=2" in ln for ln in tree),
          " | ".join(tree[:3]))

    print("\n--- 调用树（由信封 span 天然构成，无需额外埋点）---")
    for ln in tree:
        print("   " + ln)

    # ---------- T4 R1：L3 不得回调上层 ----------
    reg.register_tool(ToolSpec(
        name="evil.recall", input_schema={}, output_schema={},
        side_effect=SideEffect.PURE,
    ))
    expect_raises(
        "T4 R1 违反被拒绝（L3 不得回调 L1）",
        LayerViolation,
        rt._check_transition, Layer.EXEC, Layer.DECISION, "some_tool", "master_director",
    )
    expect_raises(
        "T4 R1 违反被拒绝（L3 不得回调 L2）",
        LayerViolation,
        rt._check_transition, Layer.EXEC, Layer.COLLAB, "some_tool", "shot_composer",
    )

    # ---------- T5 R2：L1 不得跳层直调 L3 ----------
    expect_raises(
        "T5 R2 违反被拒绝（L1 不得直调 L3）",
        LayerViolation,
        rt._check_transition, Layer.DECISION, Layer.EXEC, "master_director", "comfy.submit",
    )

    # L1 Agent 在推理循环里尝试调工具 -> 也应被拒
    def bad_director(envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult:
        loop = BoundedReasoningLoop(max_steps=3, budget=envelope.budget)

        def decide(env: CallEnvelope, steps) -> AgentDecision:
            return AgentDecision(action="comfy.submit", thought="偷懒直接出图",
                                 action_input={"graph": {}})

        res, _ = loop.run(envelope, runtime, decide)
        return res

    # 注册必须先于绑定（bind_agent 会校验注册存在）
    reg.register_agent(AgentManifest(
        agent_id="bad_director", layer=Layer.DECISION,
        capabilities=("episode.direct.bad",), consumes=("comfy.submit",),
    ))
    rt.bind_agent("bad_director", bad_director)
    try:
        rt.invoke("episode.direct.bad", {}, budget=Budget.unlimited())
        check("T5 L1 推理循环内直调 L3 被拒绝", False, "未抛错")
    except LayerViolation as e:
        check("T5 L1 推理循环内直调 L3 被拒绝", True, str(e)[:90])

    # ---------- T6 R3：预算只能减 ----------
    root = CallEnvelope.root("image_gen", "shot.produce", {},
                             Budget(tokens=100, cny=1.0, wall_ms=1000, steps=2))
    expect_raises(
        "T6 R3 超预算子调用被拒绝",
        BudgetExceeded,
        root.child, callee="comfy.submit", capability="comfy.submit",
        payload={}, budget=Budget(tokens=10, cny=99.0, wall_ms=10, steps=1),
    )
    ok_child = root.child(callee="comfy.submit", capability="comfy.submit",
                          payload={}, budget=Budget(tokens=10, cny=0.2, wall_ms=10, steps=1))
    check("T6 预算内的子调用可创建", ok_child.depth == 1, f"depth={ok_child.depth}")

    res_narrow, _ = rt.invoke(
        "shot.produce", {"graph": {}},
        budget=Budget(tokens=10**9, cny=10**9, wall_ms=10**9, steps=3),
    )
    check("T6 Agent 声明的 max_budget 生效（steps 被收窄到 4）",
          res_narrow.ok or res_narrow.error_code in ("F-CHILD", "F-REASONING-BUDGET"),
          f"ok={res_narrow.ok} err={res_narrow.error_code}")

    # ---------- T7 external 工具必须带幂等键 ----------
    expect_raises(
        "T7 external 工具缺幂等键被拒绝",
        ValueError,
        rt.call_tool,
        CallEnvelope.root("comfy.submit", "comfy.submit", {"graph": {}},
                          Budget.unlimited(), callee_kind="tool"),
    )
    env_ok = CallEnvelope.root("comfy.submit", "comfy.submit", {"graph": {"a": 1}},
                               Budget.unlimited(), callee_kind="tool")
    env_ok = CallEnvelope(**{**env_ok.__dict__, "idempotency_key": "k1"})
    check("T7 带幂等键的 external 调用成功", rt.call_tool(env_ok).ok, "")

    # ---------- T8 R4：深度上限 ----------
    deep = CallEnvelope.root("image_gen", "shot.produce", {}, Budget.unlimited())
    for _ in range(7):
        deep = deep.child(callee="image_gen", capability="shot.produce",
                          payload={}, budget=Budget(steps=1))
    expect_raises("T8 R4 深度超限被拒绝", DepthExceeded,
                  rt.call_agent, deep)

    # ---------- T9 黑板失效传播 ----------
    bb = rt.blackboard
    e_char = bb.put("char.CHAR-0007.anchors", {"gen": 2}, owner="character_designer")
    bb.put("shot.SH-001.ir", {"hash": "a"}, owner="shot_composer", deps=(e_char.entry_id,))
    e_mid = bb.get("shot.SH-001.ir")
    bb.put("episode.EP-001.timeline", {"len": 60}, owner="video_editor",
           deps=(e_mid.entry_id,))
    check("T9 初始无 stale", len(bb.stale_entries()) == 0,
          str([e.key for e in bb.stale_entries()]))

    marked = bb.invalidate("char.CHAR-0007.anchors")
    stale_keys = sorted(e.key for e in bb.stale_entries())
    check("T9 失效沿依赖链传递到全部传递依赖者（源本身不标 stale）",
          len(marked) == 2 and stale_keys == ["episode.EP-001.timeline", "shot.SH-001.ir"],
          f"marked={len(marked)} stale={stale_keys}")
    check("T9 被失效的源条目保持 fresh",
          not bb.get("char.CHAR-0007.anchors").stale,
          f"source stale={bb.get('char.CHAR-0007.anchors').stale}")

    expect_raises("T9 乐观并发版本冲突被捕获", BlackboardConflict,
                  bb.put, "shot.SH-001.ir", {"hash": "b"},
                  owner="x", expect_version=99)

    # ---------- T12 黑板别名：读得到，但不能把两条并成一条 ----------
    #
    # 同一件东西有两个名字（能力名 `review.board` / 产物名 `review.verdict`）。
    # 别名让 `get` 两条都能查到，**且两条名字共用一个 entry_id**（否则按
    # entry_id 比对的失效传播又会对不上）。
    #
    # ⚠️ 但别名**不是写入口**：不同主键必须各自成条。第一版允许顺着别名
    # 找回旧条目，结果是 `shot.list` 与 `shot.breakdown` 互相覆盖 ——
    # `value("shot.list")` 变成了 breakdown 的字典。`crew_stubs_check` 的
    # G9 当场抓住。
    bb2 = Blackboard()
    ea = bb2.put("prod.key", {"v": 1}, owner="A", aliases=("cap.name",))
    # ⚠️ 取值先落到变量再断言：直接写 `bb2.get("cap.name").entry_id` 在
    # "取不到"时会抛 AttributeError（整个自检崩掉），而不是**报一条失败**。
    # 自检里也该避免这种链式取值 —— 那正是它要抓的 L-6 那类写法。
    got_alias = bb2.get("cap.name")
    check("T12 别名可读：按另一个名字取到同一条目",
          got_alias is not None and got_alias.entry_id == ea.entry_id,
          str(getattr(got_alias, "entry_id", None)))
    eb = bb2.put("other.key", {"v": 2}, owner="B", aliases=("cap.name",))
    check("T12 ★★ 不同主键不会被别名并成一条（各自独立）",
          eb.entry_id != ea.entry_id
          and bb2.value("prod.key") == {"v": 1}
          and bb2.value("other.key") == {"v": 2},
          f"{bb2.value('prod.key')} / {bb2.value('other.key')}")
    got_alias2 = bb2.get("cap.name")
    check("T12 ★ 已登记的别名不被后写抢占",
          got_alias2 is not None and got_alias2.entry_id == ea.entry_id,
          str(getattr(got_alias2, "entry_id", None)))

    # ---------- T10 推理循环有界 ----------
    def runaway(envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult:
        loop = BoundedReasoningLoop(max_steps=3, budget=envelope.budget)

        def decide(env: CallEnvelope, steps) -> AgentDecision:
            return AgentDecision(action="llm.complete", thought="停不下来",
                                 action_input={"prompt": "x"})

        res, _ = loop.run(envelope, runtime, decide)
        return res

    reg.register_agent(AgentManifest(
        agent_id="runaway", layer=Layer.COLLAB, capabilities=("runaway",),
        consumes=("llm.complete",), max_reasoning_steps=3,
    ))
    rt.bind_agent("runaway", runaway)
    res_run, _ = rt.invoke("runaway", {}, budget=Budget.unlimited())
    check("T10 推理循环硬上限生效（不无限循环）",
          (not res_run.ok) and res_run.error_code == "F-REASONING-BUDGET" and res_run.escalate,
          f"err={res_run.error_code} escalate={res_run.escalate}")

    # ---------- T11 信封指纹稳定 ----------
    # 注意：比较的必须是 **语义相同、书写不同** 的 payload。
    e1 = CallEnvelope.root("image_gen", "shot.produce",
                           {"graph": {"b": 2, "a": 1, "note": None}}, Budget.unlimited())
    e2 = CallEnvelope(**{**e1.__dict__, "payload": {"graph": {"a": 1, "b": 2}}})
    check("T11 信封指纹与嵌套键序、冗余 null 无关", e1.key() == e2.key(),
          f"{e1.key()} vs {e2.key()}")

    e_f1 = CallEnvelope(**{**e1.__dict__, "payload": {"graph": {"a": 0.1 + 0.2, "b": 2}}})
    e_f2 = CallEnvelope(**{**e1.__dict__, "payload": {"graph": {"a": 0.3, "b": 2}}})
    check("T11 信封指纹消除浮点表示噪声", e_f1.key() == e_f2.key(),
          f"{e_f1.key()} vs {e_f2.key()}")

    e3 = CallEnvelope(**{**e1.__dict__, "payload": {"graph": {"a": 1, "b": 3}}})
    check("T11 信封指纹对内容敏感", e1.key() != e3.key(), "")

    e4 = CallEnvelope(**{**e1.__dict__, "payload": {"graph": {"a": 1, "b": 2, "c": 9}}})
    check("T11 信封指纹对新增键敏感", e1.key() != e4.key(), "")

    # ---------- 汇总 ----------
    print("\n" + "=" * 74)
    npass = sum(1 for _, s, _ in _results if s == PASS)
    for name, status, detail in _results:
        mark = "v" if status == PASS else "X"
        print(f" [{mark}] {name}")
        if status == FAIL or detail:
            print(f"       {detail}")
    print("=" * 74)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
