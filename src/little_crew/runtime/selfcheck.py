"""三级内容寻址缓存 —— 测试。

覆盖：三级键的语义分工 · 命中统计的分离 · 原子写入 · 失效传播 ·
      以及"只重跑受影响镜头"这一核心命题的可验证形式。

运行： python -m little_crew.runtime.selfcheck
零第三方依赖（用标准库 tempfile）。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from little_crew.ir.canonical import cache_key, content_hash
from little_crew.runtime.cache import (
    CacheEntry,
    CacheStats,
    CacheTier,
    ContentCache,
    LookupResult,
    invalidate_by_prefix,
    render_hash,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond, detail))


#: 两个"语义不同"的最小 IR，用于制造不同的 irHash
def _ir(shot: int, *, costume: str = "beige", **extra) -> dict:
    g = {
        "irVersion": "little_crew.ir/v1",
        "graphId": f"G-ep001-sh{shot:03d}",
        "scope": "shot",
        "bindings": {"CHAR-0007": {"anchorSet": {"front": "char/a.png"}}},
        "nodes": [
            {"id": "n_p", "kind": "prompt.text",
             "params": {"text": f"雨夜天台, {costume} 风衣, 回头, 近景"}},
        ],
        "edges": [], "constraints": [],
    }
    g.update(extra)
    return g


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("三级内容寻址缓存 —— 自检")
    print("=" * 78)

    tmp = Path(tempfile.mkdtemp(prefix="lc_cache_"))
    try:
        _run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


def _run(tmp: Path) -> None:
    cache = ContentCache(tmp / "cache")

    ir_a = _ir(1)
    ir_b = _ir(2)
    h_a = content_hash(ir_a)
    h_b = content_hash(ir_b)

    CAT = "sha256:aaa111"
    RULE = "little_crew.compile/v1"
    CAT2 = "sha256:bbb222"      # 换了环境（如 ComfyUI 升级 / 降级后）

    # ---------- A. 三级键的语义分工 ----------
    k_t1 = cache.key_t1(h_a, CAT, RULE)
    k_t2 = cache.key_t2(h_a)
    k_t3 = cache.key_t3("cond:identity>=0.74")

    check("A1 T1 键含环境与规则版本（精确复用）",
          k_t1.startswith("t1:") and k_t1 == cache_key(h_a, CAT, RULE), k_t1[:28])
    check("A2 T2 键只含 IR 哈希（跨环境语义复用）",
          k_t2.startswith("t2:") and k_t2 == cache_key(h_a), k_t2[:28])
    check("A3 T3 键为条件指纹", k_t3.startswith("t3:"), k_t3)
    check("A4 三级键互不相同", len({k_t1, k_t2, k_t3}) == 3)
    check("A5 不同 IR 得到不同 T1 键",
          cache.key_t1(h_a, CAT, RULE) != cache.key_t1(h_b, CAT, RULE))
    check("A6 同 IR 不同环境得到不同 T1 键（改环境即失效）",
          cache.key_t1(h_a, CAT, RULE) != cache.key_t1(h_a, CAT2, RULE))
    check("A7 同 IR 跨环境共享 T2 键（语义复用）",
          cache.key_t2(h_a) == cache.key_t2(h_a))

    # ---------- B. 未命中 ----------
    miss = cache.lookup(ir_hash=h_a, catalog_hash=CAT, rule_version=RULE)
    check("B1 空缓存未命中，且报告查过哪些级别",
          (not miss.hit) and CacheTier.T1 in miss.tried
          and CacheTier.T2 in miss.tried and miss.tried[0] is CacheTier.T1,
          f"tried={[t.value for t in miss.tried]}")
    check("B2 未命中计入 misses，不计入 hits",
          cache.stats.misses == 1 and cache.stats.hits == 0,
          json.dumps(cache.stats.as_dict(), ensure_ascii=False)[:110])

    # ---------- C. 写入与精确命中 ----------
    arts = {f"f{i:05d}.png": f"PNG-DATA-{i}".encode() for i in range(1, 6)}
    e1 = cache.store(ir_hash=h_a, artifacts=arts, catalog_hash=CAT,
                     rule_version=RULE, graph_hash="sha256:g1",
                     render_seconds=480.0,
                     extra_provenance={"model": "minimax-h3"})
    check("C1 写入返回条目且含产物清单",
          len(e1.artifacts) == 5 and e1.tier is CacheTier.T1,
          f"tier={e1.tier.value} artifacts={len(e1.artifacts)}")
    check("C2 条目携带溯源（IR/图/环境/规则）",
          set(e1.provenance) >= {"irHash", "graphHash", "catalogHash", "ruleVersion"},
          json.dumps(e1.provenance, ensure_ascii=False)[:110])
    check("C3 空产物被拒绝（无产物的缓存没有意义）", _raises(ValueError,
          lambda: cache.store(ir_hash=h_a, artifacts={}, catalog_hash=CAT,
                              rule_version=RULE)))

    hit1 = cache.lookup(ir_hash=h_a, catalog_hash=CAT, rule_version=RULE)
    check("C4 同环境同语义 → 精确命中 T1",
          hit1.hit and hit1.tier is CacheTier.T1 and hit1.zero_gpu,
          f"tier={hit1.tier.value if hit1.tier else None}")
    check("C5 命中时回到同一档 T1（不退化到 T2）",
          hit1.tried == (CacheTier.T1,), str([t.value for t in hit1.tried]))

    got = cache.read_artifacts(hit1)
    check("C6 产物可读回且内容一致", got == arts,
          f"{len(got)} 个文件")

    check("C7 命中累加节省时间（用于算加速比）",
          cache.stats.seconds_saved == 480.0 and cache.stats.hits == 1,
          f"saved={cache.stats.seconds_saved}s hits={cache.stats.hits}")

    # ---------- D. 跨环境语义复用（T2）----------
    hit2 = cache.lookup(ir_hash=h_a, catalog_hash=CAT2, rule_version=RULE,
                        current_catalog_hash=CAT2)
    check("D1 换环境后 T1 失效、但 T2 命中（语义复用）",
          hit2.hit and hit2.tier is CacheTier.T2,
          f"tier={hit2.tier.value if hit2.tier else None} tried={[t.value for t in hit2.tried]}")
    check("D2 T2 命中被标记为跨环境（须重校验质量断言）",
          hit2.entry is not None and hit2.entry.cross_environment is True)
    check("D3 跨环境命中仍为零 GPU（直接复用磁盘产物）",
          hit2.zero_gpu)

    # allow_t2=False 时应退回未命中
    cache2 = ContentCache(tmp / "cache")
    hit_no_t2 = cache2.lookup(ir_hash=h_a, catalog_hash=CAT2, rule_version=RULE,
                              allow_t2=False)
    check("D4 关闭 T2 后，跨环境不再复用（保守模式）",
          not hit_no_t2.hit and CacheTier.T2 not in hit_no_t2.tried,
          f"tried={[t.value for t in hit_no_t2.tried]}")

    # ---------- E. 产物不在位 → 视为未命中 ----------
    d = cache.dir_for(k_t1)
    victim = d / "f00003.png"
    blob = victim.read_bytes()
    victim.unlink()
    hit_missing = cache.lookup(ir_hash=h_a, catalog_hash=CAT, rule_version=RULE)
    check("E1 产物被删后不再命中 T1（在位性校验）",
          not (hit_missing.hit and hit_missing.tier is CacheTier.T1),
          f"tier={hit_missing.tier.value if hit_missing.tier else '未命中'}")
    check("E2 但 T2 别名仍有产物 → 命中 T2（不浪费）",
          hit_missing.hit and hit_missing.tier is CacheTier.T2,
          f"tier={hit_missing.tier.value if hit_missing.tier else '未命中'}")
    (d / "f00003.png").write_bytes(blob)   # 复原

    # ---------- F. 原子写入 ----------
    tmp_dirs = list((tmp / "cache").rglob("*.tmp*"))
    check("F1 无残留临时目录（原子写入）", tmp_dirs == [], str(tmp_dirs[:2]))

    # ---------- G. 只重跑受影响镜头（核心命题）----------
    #
    # 这是"改一句 IR 只重跑受影响的镜头"的可验证形式。
    # 场景：一集 8 镜，全部已渲染并缓存；现在只改第 3 镜的服装。
    ep_shots = {i: _ir(i) for i in range(1, 9)}
    ep_hashes = {i: content_hash(g) for i, g in ep_shots.items()}
    for i, g in ep_shots.items():
        cache.store(ir_hash=ep_hashes[i],
                    artifacts={f"f{i:03d}_{k:03d}.png": b"x" * 16
                               for k in range(1, 4)},
                    catalog_hash=CAT, rule_version=RULE, render_seconds=480.0)

    before = cache.stats.hits
    # 只改第 3 镜
    ep_shots[3] = _ir(3, costume="深灰")
    ep_hashes[3] = content_hash(ep_shots[3])

    hits, misses = 0, 0
    for i in range(1, 9):
        r = cache.lookup(ir_hash=ep_hashes[i], catalog_hash=CAT, rule_version=RULE)
        if r.hit:
            hits += 1
        else:
            misses += 1

    check("G1 改一镜后：7 镜命中、1 镜未命中",
          hits == 7 and misses == 1, f"hits={hits} misses={misses}")
    check("G2 未命中的正是被改动的那一镜",
          not cache.lookup(ir_hash=ep_hashes[3], catalog_hash=CAT,
                           rule_version=RULE).hit)
    check("G3 其余 7 镜仍精确命中（未受牵连）",
          all(cache.lookup(ir_hash=ep_hashes[i], catalog_hash=CAT,
                           rule_version=RULE).hit for i in (1, 2, 4, 5, 6, 7, 8)))

    # ---------- H. 加速比（成本可控的核心指标）----------
    s = cache.stats
    check("H1 加速比可算且 > 1（有缓存比无缓存快）",
          s.speedup > 1.0, f"speedup={s.speedup:.2f}")
    check("H2 命中率在 [0,1] 且与计数自洽",
          0.0 <= s.hit_rate <= 1.0
          and s.total_lookups == s.hits + s.misses,
          f"hitRate={s.hit_rate:.3f} total={s.total_lookups}")

    # ---------- I. ComfyUI 的缓存与我们严格分开 ----------
    s.record_comfy_cache(3)
    check("I1 ComfyUI 节点级缓存事件单独记，不计入 hits",
          s.comfy_node_cache_events == 3
          and s.hits == sum(s.hits_by_tier.values())
          and "comfyNodeCacheEvents" in s.as_dict(),
          f"comfy={s.comfy_node_cache_events} our_hits={s.hits}")
    check("I2 统计字典明确标注二者不可混记",
          "不计入 hits" in str(s.as_dict().get("note", "")),
          str(s.as_dict().get("note", ""))[:70])

    # ---------- J. 失效（"改动 → 失效 → 重渲 → 再命中"的完整工作流）----------
    #
    # ⚠️ 这里的顺序很关键，且曾经写错过：
    #   在 G 段里第 3 镜**改了参数后从未命中**，因此它**从未被 store 过**；
    #   对一个不存在的键调用失效，返回 0 是**正确的**（数的是"删掉了几条"）。
    #   所以"失效 → 未命中"这种断言毫无信息量 —— 它本来就没缓存。
    #
    #   真正该验证的是：**已缓存的镜头被作废后，重渲能再次命中**。
    n_before = cache.invalidate_by_ir(ep_hashes[3])
    check("J1 对未缓存的键失效返回 0（数的是实际删掉的条数）", n_before == 0,
          f"返回 {n_before}")

    cache.store(ir_hash=ep_hashes[3],
                artifacts={"f003_001.png": b"y" * 16},
                catalog_hash=CAT, rule_version=RULE, render_seconds=480.0)
    check("J2 重渲后可命中（工作流闭环）",
          cache.lookup(ir_hash=ep_hashes[3], catalog_hash=CAT,
                       rule_version=RULE).hit)

    n_inv = cache.invalidate_by_ir(ep_hashes[3])
    check("J3 按 IR 哈希失效命中目标条目", n_inv >= 1, f"失效 {n_inv} 条")
    check("J4 失效后该镜未命中",
          not cache.lookup(ir_hash=ep_hashes[3], catalog_hash=CAT,
                           rule_version=RULE).hit)
    check("J5 失效不影响其他镜（这是「只重跑受影响镜头」的另一半）",
          cache.lookup(ir_hash=ep_hashes[1], catalog_hash=CAT,
                       rule_version=RULE).hit)

    n_pref = invalidate_by_prefix(cache, "sha256:")
    check("J6 按前缀批量失效（整集作废这类粗粒度操作）", n_pref >= 1,
          f"失效 {n_pref} 条")

    # ---------- K. 条目枚举与溯源 ----------
    cache.store(ir_hash=h_a, artifacts={"a.png": b"1"}, catalog_hash=CAT,
                rule_version=RULE, render_seconds=1.0)
    ents = cache.entries()
    check("K1 可枚举全部条目（供审计）", len(ents) >= 1, f"{len(ents)} 条")
    check("K2 条目可往返序列化（json → 对象 → json）",
          CacheEntry.from_dict(ents[0].as_dict()).key == ents[0].key)
    check("K3 目录按哈希分片（避免单目录数千文件）",
          len(cache.dir_for(k_t1).parts) >= 4,
          "/".join(cache.dir_for(k_t1).parts[-4:]))

    # ---------- L. 防护 ----------
    check("L1 未命中结果不能读产物（防静默拿到空数据）",
          _raises(ValueError, lambda: cache.read_artifacts(
              LookupResult(hit=False))))
    check("L2 缺环境参数时 T1 键合成报错（拒绝含糊的键）",
          _raises(ValueError, lambda: cache_key(h_a, CAT, None)))

    # ---------- M. 渲染哈希 ----------
    check("M1 render_hash 稳定可复现",
          render_hash(b"abc") == render_hash(b"abc")
          and render_hash(b"abc").startswith("sha256:")
          and render_hash(b"abc") != render_hash(b"abd"))


def _raises(exc: type[BaseException], fn) -> bool:
    try:
        fn()
    except exc:
        return True
    except BaseException:  # noqa: BLE001
        return False
    return False


if __name__ == "__main__":
    raise SystemExit(main())
