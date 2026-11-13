"""自定义模型 API（第三方供应商）自检（2026-11-13）。

锁住六件事：
1 配置能解析成 `ModelSpec`，**坏条目跳过并报原因**（不静默）
2 **冲突不覆盖**内置模型
3 **用途限定**由代码强制（主链路渲染被拒）
4 key 缺失能看出来（不假装可用）
5 未核实授权默认拦下，**显式接受风险**才放行且留痕
6 关掉开关（`LITTLE_CREW_CUSTOM_PROVIDERS=0`）时注册表回到内置
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _write(path: Path, providers) -> None:
    path.write_text(json.dumps({"version": 1, "providers": providers},
                               ensure_ascii=False), encoding="utf-8")


def _ok_provider(**over):
    base = {
        "id": "demo-vendor",
        "task": "image.t2i",
        "endpoint": "https://api.example.com",
        "path": "/v1/images",
        "bodyTemplate": '{"prompt": "{prompt}"}',
        "responsePath": "data.0.url",
        "apiKeyEnv": "DEMO_KEY",
        "allowedAgents": ["character_designer"],
        "license": {"id": "demo-vendor", "commercialUse": True, "verified": False},
    }
    base.update(over)
    return base


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("自定义模型 API（第三方供应商）—— 自检")
    print("=" * 66)

    from little_crew.models.custom_providers import (
        ENV_ALLOW_UNVERIFIED,
        ENV_PATH,
        describe,
        load_custom_providers,
    )
    from little_crew.runtime.backend_guard import (
        BackendNotAllowed,
        assert_allowed,
        filter_allowed,
        is_allowed,
    )

    print(); print("A. 解析")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "providers.json"
        _write(p, [_ok_provider()])
        specs, problems = load_custom_providers(p)
        check("A1 ★★★ 一条合法配置 -> 一个 ModelSpec（无问题）",
              len(specs) == 1 and problems == [], str(problems)[:60])
        s = specs[0]
        check("A2 ★★★ 任务/端点/模板都解析进去了",
              s.task.name == "IMAGE_T2I" and s.backends[0].endpoint == "https://api.example.com"
              and "prompt" in s.backends[0].body_template, str(s.task))
        check("A3 ★★★ 自定义供应商一律 EXPERIMENTAL（没经过本项目验证）",
              s.maturity.name == "EXPERIMENTAL", s.maturity.name)

        print(); print("B. 坏条目跳过并说明")
        _write(p, [_ok_provider(id="good"), _ok_provider(id="bad", task="要饭")])
        specs2, probs2 = load_custom_providers(p)
        check("B1 ★★★ 未知任务 -> 跳过该条且给出原因",
              len(specs2) == 1 and any("未知任务" in x for x in probs2), str(probs2)[:80])
        _write(p, [{"id": "x", "task": "image.t2i"}])
        specs3, probs3 = load_custom_providers(p)
        check("B2 ★★★ 缺必填字段 -> 跳过并点名缺什么",
              specs3 == [] and any("缺字段" in x for x in probs3), str(probs3)[:80])
        _write(p, [_ok_provider(id="dup"), _ok_provider(id="dup")])
        specs4, probs4 = load_custom_providers(p)
        check("B3 ★★ id 重复 -> 只留第一条",
              len(specs4) == 1 and any("重复" in x for x in probs4), str(probs4)[:60])
        p.write_text("{bad json", encoding="utf-8")
        specs5, probs5 = load_custom_providers(p)
        check("B4 ★★★ 文件坏 JSON -> 空列表 + 原因（不抛）",
              specs5 == [] and bool(probs5), str(probs5)[:60])

        print(); print("C. 用途限定（代码强制）")
        _write(p, [_ok_provider()])
        specs6, _ = load_custom_providers(p)
        be = specs6[0].backends[0]
        check("C1 ★★★ 允许的职位通过", is_allowed(be, "character_designer"), "")
        check("C2 ★★★ 主链路渲染被拒（不是文档约定）",
              not is_allowed(be, "render"), "")
        raised = False
        try:
            assert_allowed(be, "render", what="主链路渲染")
        except BackendNotAllowed as e:
            raised = "限定给" in str(e) and "render" in str(e)
        check("C3 ★★★ 拒绝消息写清「谁请求/允许谁」（可定位）", raised, "")
        check("C4 ★★ filter_allowed 只留允许的",
              len(filter_allowed([be], "render")) == 0
              and len(filter_allowed([be], "character_designer")) == 1, "")
        _write(p, [_ok_provider(allowedAgents=[])])
        specs7, _ = load_custom_providers(p)
        be7 = specs7[0].backends[0]
        check("C5 ★★ allowedAgents 为空 = 不限", is_allowed(be7, "render"), "")

        print(); print("D. 注册表装配与冲突")
        os.environ[ENV_PATH] = str(p)
        try:
            from little_crew.models.registry import build_default_registry

            reg_custom = build_default_registry()
            check("D1 ★★★ 环境变量指定路径后能进注册表",
                  reg_custom.get("demo-vendor") is not None, "")
            _write(p, [_ok_provider(id="z-image-turbo")])
            reg_conflict = build_default_registry()
            builtin = reg_conflict.get("z-image-turbo")
            check("D2 ★★★ 与内置同名 -> 不覆盖内置定义",
                  builtin is not None
                  and builtin.maturity.name != "EXPERIMENTAL",
                  str(builtin.maturity.name if builtin else None))
            os.environ["LITTLE_CREW_CUSTOM_PROVIDERS"] = "0"
            reg_off = build_default_registry()
            check("D3 ★★★ 开关关掉 -> 自定义供应商不加载",
                  reg_off.get("demo-vendor") is None, "")
        finally:
            os.environ.pop("LITTLE_CREW_CUSTOM_PROVIDERS", None)
            os.environ.pop(ENV_PATH, None)

    print(); print("E. 授权：未核实默认拦、显式接受才放行")
    from little_crew.governance.license import LicenseGate

    os.environ.pop(ENV_ALLOW_UNVERIFIED, None)
    v1 = LicenseGate().check("demo-vendor")
    check("E1 ★★★ 默认 -> 不放行", not v1.allowed, v1.reason[:50])
    os.environ[ENV_ALLOW_UNVERIFIED] = "1"
    try:
        v2 = LicenseGate().check("demo-vendor")
        check("E2 ★★★ 显式接受风险 -> 放行且理由写明「未核实」",
              v2.allowed and "未核实" in v2.reason or "无授权记录" in v2.reason,
              v2.reason[:70])
    finally:
        os.environ.pop(ENV_ALLOW_UNVERIFIED, None)

    print(); print("F. doctor 描述")
    with tempfile.TemporaryDirectory() as td2:
        p2 = Path(td2) / "providers.json"
        _write(p2, [_ok_provider()])
        info = describe(p2)
        check("F1 ★★★ 能列出供应商与「key 是否在」",
              info["count"] == 1 and "keyPresent" in info["providers"][0],
              str(info["providers"])[:80])
        check("F2 ★★ 列出用途限定", info["providers"][0]["allowedAgents"] == ["character_designer"], "")

    print(); print("G. 凭据与 TLS（2026-11-13 真机踩到的两个坑）")
    from little_crew.models.registry import build_default_registry
    from little_crew.runtime.channels.http import HttpChannel

    with tempfile.TemporaryDirectory() as td3:
        p3 = Path(td3) / "providers.json"
        _write(p3, [_ok_provider(id="env-key", apiKeyEnv="DEMO_UNSET_KEY",
                                 allowedAgents=[])])
        os.environ[ENV_PATH] = str(p3)
        try:
            reg = build_default_registry()
            spec = reg.get("env-key")
            be = spec.backends[0]
            os.environ["DEMO_UNSET_KEY"] = "secret-123"
            try:
                ch = HttpChannel(be)
                check("G1 ★★★ api_key 留空时**从环境变量读**（否则云端永远 401）",
                      ch.api_key == "secret-123", repr(ch.api_key)[:24])
            finally:
                os.environ.pop("DEMO_UNSET_KEY", None)
            ch2 = HttpChannel(be)
            check("G2 ★★ 环境里没有该变量时 api_key 为空（不编造）",
                  ch2.api_key == "", repr(ch2.api_key)[:24])
            check("G3 ★★★ 云端后端默认**不预探活**（根路径 401 不等于不可用）",
                  getattr(be, "is_local_by_default", False) is False, "")
        finally:
            os.environ.pop(ENV_PATH, None)

    with tempfile.TemporaryDirectory() as td4:
        p4 = Path(td4) / "providers.json"
        _write(p4, [_ok_provider(id="tls", insecureTls=True)])
        specs_t, _ = load_custom_providers(p4)
        check("G4 ★★★ insecureTls 解析进 Backend（按供应商声明，不全局）",
              specs_t[0].backends[0].insecure_tls is True, "")
        _write(p4, [_ok_provider(id="tls2")])
        specs_t2, _ = load_custom_providers(p4)
        check("G5 ★★★ 默认 False（安全默认值）",
              specs_t2[0].backends[0].insecure_tls is False, "")

    from little_crew import net

    check("G6 ★★ insecure_context 确实关掉校验",
          net.insecure_context().verify_mode.name == "CERT_NONE", "")
    check("G7 ★★ 默认上下文仍严格（不静默放松）",
          net.ssl_context() is None, "环境没设 NO_VERIFY 时应为 None")

    print(); print("H. UA 与内联 base64（2026-11-13 真机踩到的两个坑）")
    with tempfile.TemporaryDirectory() as td5:
        p5 = Path(td5) / "providers.json"
        _write(p5, [_ok_provider(id="ua-vendor", userAgent="OpenAI/Python 1.0")])
        specs_ua, _ = load_custom_providers(p5)
        check("H1 ★★★ userAgent 解析进 Backend（网关会拒默认 UA）",
              specs_ua[0].backends[0].user_agent == "OpenAI/Python 1.0", "")
        _write(p5, [_ok_provider(id="b64-vendor", responseB64Path="data.0.b64_json")])
        specs_b, _ = load_custom_providers(p5)
        check("H2 ★★★ responseB64Path 解析进 Backend（有的接口 url 为空）",
              specs_b[0].backends[0].response_b64_path == "data.0.b64_json", "")

        # base64 -> 本地文件
        import base64
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
        from little_crew.runtime.channels.http import HttpChannel

        ch = HttpChannel(specs_b[0].backends[0])
        path = ch._materialize_b64(base64.b64encode(png).decode())
        from pathlib import Path as _P

        check("H3 ★★★ 内联 base64 能落成真实图片文件（魔数正确）",
              bool(path) and _P(path).is_file()
              and _P(path).read_bytes()[:8] == png[:8], str(path)[:60])
        check("H4 ★★ 同一内容重复落盘不重复写（按内容哈希命名）",
              ch._materialize_b64(base64.b64encode(png).decode()) == path, "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
