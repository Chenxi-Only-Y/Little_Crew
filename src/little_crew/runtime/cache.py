"""三级内容寻址缓存 —— 兑现"改一句 IR 只重跑受影响的镜头"。

这一层解决什么
--------------
一集 60 镜、每镜约 124 帧、本机 H3 实测 **442~518 秒/镜** → 一集 **4~7.5 小时 GPU 时间**。
在这个量级上，"改一个参数就全量重跑"是不可接受的。

因此缓存不是优化，而是**可作性前提**。

三级键的分工（与 `ir.canonical.cache_key` 对齐）
-----------------------------------------------
| 级别 | 键 | 语义 | 何时用 |
|---|---|---|---|
| **T1** | `t1:sha256(irHash‖catalogHash‖ruleVersion)` | **精确复用** | 同语义 + 同环境 + 同规则 |
| **T2** | `t2:irHash` | **跨环境语义复用** | 换了 ComfyUI 版本/降级后仍是同一语义 —— **但产物必须在位** |
| **T3** | 条件指纹 | **按条件复用** | 放宽/收紧约束时复用同一素材 |

T1 与 T2 的差别很关键：T2 是"语义相同但环境不同"，
因此**命中 T2 不等于产出可用** —— 必须校验产物文件仍在位，否则退回实算。

存储布局：内容寻址
------------------
```
<cacheRoot>/
  t1/ab/abcdef.../        <- 前 2 位/次 2 位/全哈希 分片（与 git 同思路）
      meta.json           条目元数据（溯源、产物清单、依赖）
      shot_0001.png       产物
      ...
  t2/...
  t3/...
```

**为什么分片**：一集 60 镜 × 124 帧 ≈ **7,440 个文件**。
平铺在一个目录里，NTFS 的目录遍历会显著变慢。两级分片把单目录条目数压到可控范围。

⚠️ 命中统计必须分得清
--------------------
**我们的三级缓存**与 **ComfyUI 自身的节点级缓存**是两回事：

| 来源 | 含义 | 记在哪 |
|---|---|---|
| `cache_hit`（T1/T2/T3） | **我们的**语义缓存命中，直接复用产物，**零 GPU** | `CacheStats.hits` |
| ComfyUI 的 `execution_cached` | 它内部跳过了若干节点，**仍要走一次提交**，且**不跨实例** | `CacheStats.comfyNodeCacheEvents` |

**混在一起会让指标虚高且无法解释** —— 会把 ComfyUI 的功劳记成我们的。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from little_crew.ir.canonical import cache_key

__all__ = [
    "CacheTier",
    "CacheEntry",
    "CacheStats",
    "LookupResult",
    "ContentCache",
    "invalidate_by_prefix",
    "render_hash",
]


# ==========================================================================
# 枚举与数据结构
# ==========================================================================

class CacheTier(str, Enum):
    """缓存级别。"""

    T1 = "t1"    # 精确：同 IR + 同环境 + 同规则
    T2 = "t2"    # 语义：同 IR，跨环境
    T3 = "t3"    # 条件：按约束条件指纹

    @property
    def zero_gpu(self) -> bool:
        """命中该级别是否意味着**零 GPU 成本**。

        T1/T2/T3 都是直接复用磁盘上的产物，因此都是零 GPU。
        这与 ComfyUI 的节点级缓存不同 —— 后者仍要提交一次、仍会跑未命中的节点。
        """
        return True


@dataclass
class CacheEntry:
    """一条缓存记录。**携带完整溯源** —— 否则无法判断"能不能复用"。"""

    key: str
    tier: CacheTier
    created_at: float
    #: 产物文件名清单（相对条目目录）
    artifacts: tuple[str, ...] = ()
    #: 溯源：IR 哈希 / 图哈希 / 环境哈希 / 规则版本
    provenance: Mapping[str, Any] = field(default_factory=dict)
    #: 该条目依赖的上游键（用于失效传播）
    deps: tuple[str, ...] = ()
    #: 渲染耗时（秒）—— 用于计算"省了多少"
    render_seconds: float = 0.0
    #: 命中的 T2 在**别的环境**产生，使用时需重校验质量断言
    cross_environment: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "tier": self.tier.value,
            "createdAt": self.created_at,
            "artifacts": list(self.artifacts),
            "provenance": dict(self.provenance),
            "deps": list(self.deps),
            "renderSeconds": self.render_seconds,
            "crossEnvironment": self.cross_environment,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CacheEntry":
        return cls(
            key=str(d["key"]),
            tier=CacheTier(d["tier"]),
            created_at=float(d.get("createdAt", 0.0)),
            artifacts=tuple(d.get("artifacts", ())),
            provenance=dict(d.get("provenance", {})),
            deps=tuple(d.get("deps", ())),
            render_seconds=float(d.get("renderSeconds", 0.0)),
            cross_environment=bool(d.get("crossEnvironment", False)),
        )


@dataclass
class LookupResult:
    """一次查找的结果。**未命中也要说明查过哪些级别** —— 否则无法诊断。"""

    hit: bool
    tier: CacheTier | None = None
    entry: CacheEntry | None = None
    dir: Path | None = None
    #: 查过的级别（按顺序）
    tried: tuple[CacheTier, ...] = ()
    #: 未命中的原因（可读）
    reason: str = ""

    @property
    def zero_gpu(self) -> bool:
        return self.hit and self.tier is not None and self.tier.zero_gpu

    def as_dict(self) -> dict[str, Any]:
        return {
            "hit": self.hit,
            "tier": self.tier.value if self.tier else None,
            "tried": [t.value for t in self.tried],
            "reason": self.reason,
            "artifacts": list(self.entry.artifacts) if self.entry else [],
            "crossEnvironment": bool(self.entry and self.entry.cross_environment),
        }


@dataclass
class CacheStats:
    """命中统计。**我们的缓存与 ComfyUI 的缓存严格分开。**"""

    #: 各级别命中次数
    hits_by_tier: dict[str, int] = field(default_factory=dict)
    #: 未命中而实算的次数
    misses: int = 0
    #: 命中时**省下的**渲染时间（秒）—— 由条目的 renderSeconds 累加
    seconds_saved: float = 0.0
    #: 实算耗费的总时间（秒）
    seconds_spent: float = 0.0
    #: ComfyUI 自身的节点级缓存事件数（**不计入我们的命中**）
    comfy_node_cache_events: int = 0

    @property
    def hits(self) -> int:
        return sum(self.hits_by_tier.values())

    @property
    def total_lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return (self.hits / self.total_lookups) if self.total_lookups else 0.0

    @property
    def speedup(self) -> float:
        """有缓存 vs 无缓存 的时间比。

        这是"成本可控"这一论点的**核心指标** ——
        没有它，"只重跑受影响的镜头"就只是一句设计意图。
        """
        if self.seconds_spent <= 0:
            return float("inf") if self.seconds_saved > 0 else 1.0
        return (self.seconds_saved + self.seconds_spent) / self.seconds_spent

    def record_hit(self, tier: CacheTier, seconds_saved: float = 0.0) -> None:
        self.hits_by_tier[tier.value] = self.hits_by_tier.get(tier.value, 0) + 1
        self.seconds_saved += max(0.0, seconds_saved)

    def record_miss(self, seconds_spent: float = 0.0) -> None:
        self.misses += 1
        self.seconds_spent += max(0.0, seconds_spent)

    def record_spent(self, seconds: float) -> None:
        """补记一次**实算**花掉的秒数（`misses` 已经在查缓存时计过）。

        ## 为什么需要单独一个入口（2026-10-23 审计 M-12）

        `record_miss()` 是在**查缓存未命中时**调用的 —— 那时还不知道这次渲染
        要花多久；实耗时间只有渲染完才知道。而两个生产调用点都没传时间，
        于是 `seconds_spent` **恒为 0**：

        * 有过命中 -> `speedup` 走 `inf` 分支（序列化成 `None`）
        * 没命中   -> `speedup` 恒为 `1.0`

        也就是说「提速比」这个被 `speedup` 自己称为"成本可控的核心指标"的数字，
        **从不反映真实收益**。而自检里直接调 `record_miss(seconds_spent=…)`
        验的是**公式**，绕过了接线 —— 典型的"测公式不测接线"。
        """
        self.seconds_spent += max(0.0, float(seconds))

    def record_comfy_cache(self, n: int = 1) -> None:
        """记录 ComfyUI 的节点级缓存事件。

        **刻意不计入 hits** —— 那是 ComfyUI 的缓存，不是我们的语义缓存。
        混记会让"节省比例"虚高，且无法向评审解释来源。
        """
        self.comfy_node_cache_events += n

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hitRate": round(self.hit_rate, 4),
            "hitsByTier": dict(self.hits_by_tier),
            "secondsSaved": round(self.seconds_saved, 2),
            "secondsSpent": round(self.seconds_spent, 2),
            "speedup": (round(self.speedup, 2)
                        if self.speedup != float("inf") else None),
            "comfyNodeCacheEvents": self.comfy_node_cache_events,
            "note": ("comfyNodeCacheEvents 是 ComfyUI 自身的节点级缓存，"
                     "不计入 hits —— 二者来源不同，混记会让指标虚高"),
        }


# ==========================================================================
# 工具
# ==========================================================================

def render_hash(data: bytes) -> str:
    """产物的内容哈希（用于校验"缓存里的文件没被改过"）。"""
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()


def _shard(key: str) -> tuple[str, str, str]:
    """把键拆成分片路径。

    键形如 ``t1:sha256:abcdef...`` —— 取摘要部分的字符分片。
    """
    digest = key.rsplit(":", 1)[-1]
    return digest[:2], digest[2:4], digest


# ==========================================================================
# 缓存本体
# ==========================================================================

class ContentCache:
    """内容寻址的三级缓存。

    **只做存取与溯源，不做渲染决策** —— 那是编排层的职责。
    本类不知道 ComfyUI、不知道模型、不知道 IR 结构。
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.stats = CacheStats()
        for tier in CacheTier:
            (self.root / tier.value).mkdir(parents=True, exist_ok=True)

    # ---- 键合成 --------------------------------------------------------

    @staticmethod
    def key_t1(ir_hash: str, catalog_hash: str, rule_version: str) -> str:
        return cache_key(ir_hash, catalog_hash, rule_version)

    @staticmethod
    def key_t2(ir_hash: str) -> str:
        return cache_key(ir_hash)

    @staticmethod
    def key_t3(condition_fingerprint: str) -> str:
        return f"t3:{condition_fingerprint}"

    # ---- 路径 ----------------------------------------------------------

    def dir_for(self, key: str) -> Path:
        tier = key.split(":", 1)[0]
        a, b, digest = _shard(key)
        return self.root / tier / a / b / digest

    # ---- 查找 ----------------------------------------------------------

    def lookup(
        self,
        *,
        ir_hash: str,
        catalog_hash: str | None = None,
        rule_version: str | None = None,
        condition_fingerprint: str | None = None,
        allow_t2: bool = True,
        current_catalog_hash: str | None = None,
    ) -> LookupResult:
        """按 T1 → T2 → T3 顺序查找。

        Args:
            allow_t2: 是否允许跨环境复用。**关闭它 = 只信同环境产物**。
            current_catalog_hash: 当前环境哈希。用于给 T2 命中打
                ``cross_environment`` 标记 —— T2 的定义就是"跨环境"，
                因此命中后**必须重校验质量断言**才能用。
        """
        tried: list[CacheTier] = []
        candidates: list[tuple[CacheTier, str]] = []

        if catalog_hash is not None and rule_version is not None:
            candidates.append((CacheTier.T1, self.key_t1(ir_hash, catalog_hash, rule_version)))
        if allow_t2:
            candidates.append((CacheTier.T2, self.key_t2(ir_hash)))
        if condition_fingerprint:
            candidates.append((CacheTier.T3, self.key_t3(condition_fingerprint)))

        for tier, key in candidates:
            tried.append(tier)
            d = self.dir_for(key)
            meta = d / "meta.json"
            if not meta.is_file():
                continue
            try:
                entry = CacheEntry.from_dict(
                    json.loads(meta.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError):
                # 元数据损坏 → 视为未命中，但**不删除**（留待人工排查）
                continue

            # **产物在位性必须校验**：磁盘可能被清理过
            missing = [a for a in entry.artifacts if not (d / a).is_file()]
            if missing:
                continue

            if tier is CacheTier.T2 and current_catalog_hash is not None:
                entry.cross_environment = (
                    entry.provenance.get("catalogHash") != current_catalog_hash)
            self.stats.record_hit(tier, entry.render_seconds)
            return LookupResult(hit=True, tier=tier, entry=entry, dir=d,
                                tried=tuple(tried),
                                reason=f"命中 {tier.value}")

        self.stats.record_miss()
        return LookupResult(hit=False, tried=tuple(tried),
                            reason="T1/T2/T3 均未命中或产物不在位")

    # ---- 写入 ----------------------------------------------------------

    def store(
        self,
        *,
        ir_hash: str,
        artifacts: Mapping[str, bytes],
        catalog_hash: str | None = None,
        rule_version: str | None = None,
        graph_hash: str = "",
        render_seconds: float = 0.0,
        deps: Sequence[str] = (),
        extra_provenance: Mapping[str, Any] | None = None,
        condition_fingerprint: str | None = None,
    ) -> CacheEntry:
        """写入条目。同时写 T1 与 T2（若给了环境参数），使跨环境复用成为可能。

        **原子写入**：先写临时目录再改名。
        否则中断会留下"有 meta 但产物不全"的条目 —— 那种条目会让
        ``lookup`` 的产物在位性校验失效（或更糟：被当成有效命中）。
        """
        if not artifacts:
            raise ValueError("拒绝写入空条目 —— 无产物的缓存没有意义")

        tier = CacheTier.T3 if (condition_fingerprint and not catalog_hash) else CacheTier.T1
        key = (self.key_t3(condition_fingerprint) if tier is CacheTier.T3
               else self.key_t1(ir_hash, catalog_hash or "", rule_version or ""))
        d = self.dir_for(key)
        tmp = d.with_name(d.name + f".tmp{os.getpid()}")

        prov: dict[str, Any] = {"irHash": ir_hash, "graphHash": graph_hash}
        if catalog_hash:
            prov["catalogHash"] = catalog_hash
        if rule_version:
            prov["ruleVersion"] = rule_version
        if extra_provenance:
            prov.update(dict(extra_provenance))

        entry = CacheEntry(
            key=key, tier=tier, created_at=time.time(),
            artifacts=tuple(sorted(artifacts)),
            provenance=prov, deps=tuple(deps),
            render_seconds=float(render_seconds),
        )

        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            for name, blob in artifacts.items():
                p = tmp / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(blob)
            (tmp / "meta.json").write_text(
                json.dumps(entry.as_dict(), ensure_ascii=False, indent=1),
                encoding="utf-8")
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            tmp.replace(d)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        # 同步写一份 T2 别名。
        #
        # ⚠️ 这里的条件是 ``catalog_hash is not None`` **曾经是个缺陷**：
        # 调用方若只传 ``ir_hash``（常见于"还没编译、只有语义指纹"的场景），
        # ``store`` 会走 ``key_t1(ir_hash, "", "")`` 写出一条 T1 条目 ——
        # 而 ``lookup(ir_hash=…)`` 查的是 **T2** 键，两者**永远碰不上**。
        # 表现就是"明明写进去了，重跑却从不命中"。
        #
        # 修正：只要写的是 T1，就同时写 T2 别名（硬链接，不额外占盘）。
        # **写入与查找必须用同一套键约定** —— 这是缓存最基本的自洽性。
        if tier is CacheTier.T1:
            self._write_t2_alias(ir_hash, entry)
        return entry

    def _write_t2_alias(self, ir_hash: str, entry: CacheEntry) -> None:
        """写 T2 别名条目（产物用硬链接，避免翻倍占盘）。"""
        key2 = self.key_t2(ir_hash)
        d2 = self.dir_for(key2)
        if d2.exists():
            return
        tmp = d2.with_name(d2.name + f".tmp{os.getpid()}")
        src = self.dir_for(entry.key)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            for name in entry.artifacts:
                s, t = src / name, tmp / name
                t.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(s, t)          # 同卷硬链接，零额外占用
                except OSError:
                    shutil.copy2(s, t)     # 跨卷则退回复制
            e2 = CacheEntry(
                key=key2, tier=CacheTier.T2, created_at=entry.created_at,
                artifacts=entry.artifacts, provenance=dict(entry.provenance),
                deps=entry.deps, render_seconds=entry.render_seconds,
                cross_environment=True,
            )
            (tmp / "meta.json").write_text(
                json.dumps(e2.as_dict(), ensure_ascii=False, indent=1),
                encoding="utf-8")
            tmp.replace(d2)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- 读取产物 ------------------------------------------------------

    def read_artifacts(self, res: LookupResult) -> dict[str, bytes]:
        if not res.hit or res.dir is None or res.entry is None:
            raise ValueError("未命中的查找结果不能读取产物")
        out: dict[str, bytes] = {}
        for name in res.entry.artifacts:
            p = res.dir / name
            if p.is_file():
                out[name] = p.read_bytes()
        return out

    # ---- 失效 ----------------------------------------------------------

    def invalidate(self, key: str) -> bool:
        d = self.dir_for(key)
        if not d.exists():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True

    def entries(self, tier: CacheTier | None = None) -> list[CacheEntry]:
        """枚举现有条目（用于审计与失效传播）。"""
        out: list[CacheEntry] = []
        tiers = [tier] if tier else list(CacheTier)
        for t in tiers:
            base = self.root / t.value
            if not base.is_dir():
                continue
            for meta in base.rglob("meta.json"):
                try:
                    out.append(CacheEntry.from_dict(
                        json.loads(meta.read_text(encoding="utf-8"))))
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(out, key=lambda e: e.created_at)

    def invalidate_by_ir(self, ir_hash: str) -> int:
        """按 IR 哈希失效全部相关条目（含 T2 别名）。

        **这是"改一句 IR，只让受影响的镜头失效"的机制基础** ——
        编排层只需对受影响的镜头调用本方法，其余条目保持有效。
        """
        n = 0
        for e in self.entries():
            if e.provenance.get("irHash") == ir_hash:
                d = self.dir_for(e.key)
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
        return n


def invalidate_by_prefix(cache: ContentCache, ir_hash_prefix: str) -> int:
    """按 IR 哈希**前缀**批量失效（用于"整场/整集作废"这类粗粒度操作）。"""
    n = 0
    for e in cache.entries():
        h = str(e.provenance.get("irHash", ""))
        if h.startswith(ir_hash_prefix):
            d = cache.dir_for(e.key)
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n
