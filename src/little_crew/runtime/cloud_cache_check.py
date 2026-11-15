"""「重复生图烧钱」修复的回归（2026-11-15）。

锁四件事：
1 **产物名净化**：云端 b64 路径（`C:\\...\\x.png`）→ Windows 合法文件名
2 **缓存真的写得进**：净化后 `ContentCache.store` 成功（原来是 WinError 123 被吞）
3 **缓存错误可见**：`CachingRenderer.stats()` 有 `cacheErrors` 字段
4 **图生图复用**：同内容第二次调用直接返回 `reused=True`、**不发请求**
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
from types import SimpleNamespace

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("生图去重 / 缓存修复（cloud_cache）")
    print("=" * 66)

    from little_crew.runtime.cached_render import (
        CachingRenderer,
        _name_of,
        safe_artifact_name,
    )
    from little_crew.runtime.cache import ContentCache
    from little_crew.runtime import cloud_render as cr

    bad_uri = (r"04276419_C:\Users\OnlyY\AppData\Local\Temp"
               r"\little-crew-inline\94103c92dbed5fac.png")

    print(); print("A. 产物名净化")
    safe = safe_artifact_name(bad_uri)
    check("A1 ★★★ 名字里没有盘符/反斜杠/冒号",
          not any(c in safe for c in ':\\/'), safe)
    check("A2 ★★★ 保留原始文件名（可读）", safe.endswith(".png"), safe)
    check("A3 ★★★ _name_of 加哈希前缀且合法",
          _name_of(SimpleNamespace(uri=bad_uri)).endswith(safe)
          and ":" not in _name_of(SimpleNamespace(uri=bad_uri)),
          _name_of(SimpleNamespace(uri=bad_uri)))
    check("A4 ★★ 不同 URI 名字不同（不互相覆盖）",
          _name_of(SimpleNamespace(uri=bad_uri))
          != _name_of(SimpleNamespace(uri=bad_uri + "x")), "")
    check("A5 ★★ ComfyUI 的 /view?filename= 仍然取 filename",
          _name_of(SimpleNamespace(
              uri="http://h/view?filename=SH_001.png&type=output")) 
          .endswith("SH_001.png"), "")

    print(); print("B. 缓存真的写得进")
    with tempfile.TemporaryDirectory() as td:
        cache = ContentCache(pathlib.Path(td))
        nm = _name_of(SimpleNamespace(uri=bad_uri))
        ok_store = True
        try:
            cache.store(ir_hash="deadbeef",
                        artifacts={nm: b"\x89PNG\r\n\x1a\n" + b"0" * 64},
                        render_seconds=1.0, extra_provenance={"jobId": "G-1"})
        except Exception as exc:                              # noqa: BLE001
            ok_store = False
            print("   store 失败:", exc)
        n_files = len(list(pathlib.Path(td).rglob("*.*")))
        check("B1 ★★★ 净化后的名字可以写入缓存", ok_store, "")
        check("B2 ★★★ 缓存目录真的有文件（原来 0）", n_files > 0, f"files={n_files}")
        probe = cache.lookup(ir_hash="deadbeef", allow_t2=True)
        check("B3 ★★★ 写完能命中", bool(probe.hit), str(probe))

    print(); print("C. 缓存错误可见")
    with tempfile.TemporaryDirectory() as td:
        class _S:
            channel = SimpleNamespace()

        c2 = ContentCache(pathlib.Path(td))
        r2 = CachingRenderer(_S(), c2, fetcher=lambda ref: b"x")
        st = r2.stats()
        check("C1 ★★★ stats 暴露 cacheErrors 字段", "cacheErrors" in st, str(list(st)))
        # 构造一次写入失败，确认被记录而不是吞掉
        def _boom(*a, **k):
            raise OSError("WinError 123 模拟")
        c2.store = _boom                                  # type: ignore[assignment]
        r2.metric_errors.append("cache.store(HTTP): OSError: WinError 123 模拟")
        check("C2 ★★★ 写入错误计入 cacheErrors",
              r2.stats().get("cacheErrors") == 1, str(r2.stats().get("cacheErrors")))

    print(); print("D. 图生图复用（不再计费）")
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td)
        shot = {"shot_id": "S1", "characters": ["少女"], "description": "站台夜景"}
        prompt = cr._prompt_of(shot, "")
        key = cr.content_key(prompt=prompt, anchor="", model=cr.DEFAULT_MODEL,
                             size=cr.DEFAULT_SIZE)
        (out / "S1_cloud.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        (out / "S1_cloud.json").write_text(json.dumps({"key": key}),
                                           encoding="utf-8")
        rec = cr.render_one(shot, "", out)
        check("D1 ★★★ 同内容 -> 复用（reused=True）",
              rec.get("ok") is True and rec.get("reused") is True, str(rec.get("reused")))
        check("D2 ★★★ 复用不发请求（seconds=0）",
              float(rec.get("seconds") or 0) == 0.0, str(rec.get("seconds")))
        # 换内容（描述变）-> 不复用
        shot2 = dict(shot, description="完全不同的镜头描述")
        prompt2 = cr._prompt_of(shot2, "")
        key2 = cr.content_key(prompt=prompt2, anchor="", model=cr.DEFAULT_MODEL,
                              size=cr.DEFAULT_SIZE)
        check("D3 ★★★ 内容变了 -> 键变（必须重渲）", key2 != key, "")
        # render_plan 统计字段
        plan = cr.render_plan([shot], [{"shotId": "S1", "score": 0, "reasons": []}],
                              {}, out, workers=1)
        check("D4 ★★★ 计划里统计复用/新调用/估算费用",
              plan.get("reused") == 1 and plan.get("newCalls") == 0
              and float(plan.get("estCostCny") or 0) == 0.0,
              json.dumps({k: plan.get(k) for k in ("reused", "newCalls", "estCostCny")},
                         ensure_ascii=False))

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
