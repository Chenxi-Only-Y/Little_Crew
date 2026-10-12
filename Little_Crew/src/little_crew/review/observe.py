"""观测值提取：把产物翻译成断言能用的观测（`review` 的输入侧）。

设计要点
--------
**提取器只做"翻译"与"测量"，不做判断。**
判断在 `gate.py`，判定规则在 `ops.py`。

**为什么必须真的去量，而不是读产物引用上的字段**：
`ArtifactRef` 上的 `width`/`height`/`bytes` 是**可选**的 ——
实测 `ComfyChannel._absorb_outputs` 只填 `kind`/`uri`/`extra`（文件名），
尺寸与字节数**全为 0**。若提取器只读这些字段，门禁在生产里会以为
"观测缺失"，于是把**所有**分辨率/大小类的约束都跳过 ——
表现是"门禁接了但什么都不检查"。

所以提取器需要一个**产物解析器**（把文件名变成真实路径），
然后从文件本身测量。`EpisodeRunner` 已经有 `resolve_artifact`，
直接复用。

**提不出的观测不编造。** 本机没有人脸/嵌入模型，`similarity` 提不出来 ——
那不是"默认通过"，而是该约束无法执行（见 `observe.plan_observations`）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from little_crew.review.gate import Observation

__all__ = [
    "ObservableSpec",
    "ObservationPlan",
    "extract_from_artifact",
    "measure_artifact",
    "plan_observations",
]


#: 每个算子需要哪些观测字段 —— 用于**能力预检**。
#:
#: ⚠️ **这张表必须与 `ops.py` 保持同步。**
#: 不同步的后果：预检说"能算"，运行时算子拿到缺失字段 ->
#: 判不通过 -> 产物被误拦。自检里有断言锁住这一点。
OPERATOR_NEEDS: Mapping[str, frozenset[str]] = {
    "identity_similarity": frozenset({"similarity"}),
    "pairwise_similarity": frozenset({"similarity"}),
    "token_diff": frozenset({"script", "produced"}),
    "count_at_least": frozenset(),      # 真正的字段由 expected.field 指定
    "count_at_most": frozenset(),
    "duration_within": frozenset({"durationSec"}),
    "resolution_matches": frozenset({"width", "height"}),
    "aspect_ratio_within": frozenset({"width", "height"}),
    "byte_size_within": frozenset({"bytes"}),
    "not_empty": frozenset(),
    # ---- L3：跨模型族盲读 ----
    #
    # ⚠️ **必须在这里登记**，否则 `plan_observations` 会认为它"不需要
    # 任何观测" -> 直接交给算子 -> 算子判"缺观测"不通过 ->
    # **每一镜都被硬拦**。
    #
    # 而那正是"预检说能算、运行时却拿不到字段"的典型形态 ——
    # 见上面 `OPERATOR_NEEDS` 的说明。本机没有 VLM key 时，
    # 正确结局是**如实跳过**，不是全部拦下。
    "blind_read_verdict": frozenset({"blindVerdict"}),
}

#: 带 `field` 参数的算子：真正需要的字段由 `expected["field"]` 决定。
FIELD_PARAM_OPS: frozenset[str] = frozenset({"count_at_least", "count_at_most",
                                             "not_empty"})

#: 图像文件的魔术字节 -> 尺寸解析器名。**只用标准库**（核心层零三方依赖）。
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_JPEG_SIG = b"\xff\xd8\xff"
_GIF_SIG = (b"GIF87a", b"GIF89a")


def _png_size(head: bytes) -> tuple[int, int] | None:
    """PNG：IHDR 在固定偏移（8 字节签名 + 4 长度 + 4 类型）。"""
    if len(head) < 24 or not head.startswith(_PNG_SIG):
        return None
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)


def _gif_size(head: bytes) -> tuple[int, int] | None:
    if len(head) < 10 or not head.startswith(_GIF_SIG):
        return None
    w, h = struct.unpack("<HH", head[6:10])
    return int(w), int(h)


def _jpeg_size(head: bytes) -> tuple[int, int] | None:
    """JPEG：扫 SOF 标记。**不能只读固定偏移** —— JPEG 头部变长。"""
    if len(head) < 4 or not head.startswith(_JPEG_SIG):
        return None
    i = 2
    n = len(head)
    while i + 9 < n:
        if head[i] != 0xFF:
            i += 1
            continue
        marker = head[i + 1]
        # SOF0..SOF15（跳过 DHT 0xC4 / JPG 0xC8 / DAC 0xCC）
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", head[i + 5:i + 9])
            return int(w), int(h)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", head[i + 2:i + 4])[0]
        i += 2 + seg
    return None


def _image_size(path: Path) -> tuple[int, int] | None:
    """从文件头读图像尺寸。**不依赖 PIL**（核心层零三方依赖）。"""
    try:
        with path.open("rb") as f:
            head = f.read(65536)
    except OSError:
        return None
    for fn in (_png_size, _gif_size, _jpeg_size):
        sz = fn(head)
        if sz:
            return sz
    return None


def measure_artifact(ref: Any, resolve: Callable[[str], Any] | None,
                     *, want_signature: bool = False) -> dict[str, Any]:
    """**量**一个产物的可测属性。

    ⚠️ **`ref` 可能是字符串（文件名），不只是 `ArtifactRef`。**
    实测 `CachingRenderer` 的 `outcome.artifacts` 里放的就是 **str** ——
    缓存层已经把产物规范化成了名字。只按对象处理的话，
    `getattr` 全部拿到默认值、`width`/`height`/`bytes` 全缺，
    门禁于是把分辨率/大小类约束**全部跳过** ——
    表现是"门禁接了但什么都不检查"，而这**不会报任何错**。

    `want_signature` 控制**是否顺手算一致性子**。
    ⚠️ 默认 `False` 是刻意的：算一张 832x480 的一致性子要 ~300ms，
    而绝大多数约束（分辨率/时长/角色数）根本用不上它。
    无条件算的话，"跑一集"会在门禁这步白花 6 秒 —— 门禁本该是廉价步骤。

    优先级：引用上已有的值 > 从文件量出的值。
    两者都没有则该字段缺席（**不编造**）。
    """
    out: dict[str, Any] = {}

    # ---- ⓪a 内存字节：**首选**。字节已在手上，既不需要解析器，
    #         也不受"文件在不在磁盘上"影响。
    #
    # 实测 `CachingRenderer` 的 `CacheOutcome.artifacts` 就是
    # `{文件名: bytes}` —— 量内存里的字节比读文件更可靠。
    if isinstance(ref, (bytes, bytearray)):
        return _measure_blob(bytes(ref), "", want_signature=want_signature)

    # ---- ⓪b (name, bytes) 二元组 ----
    if (isinstance(ref, tuple) and len(ref) == 2
            and isinstance(ref[1], (bytes, bytearray))):
        return _measure_blob(bytes(ref[1]), str(ref[0]),
                             want_signature=want_signature)

    # ---- ⓪c 纯文件名：去解析 + 从文件量 ----
    if isinstance(ref, (str, Path)):
        name = str(ref)
        path = _resolve_path(name, resolve)
        if path is not None and path.is_file():
            try:
                out["bytes"] = path.stat().st_size
            except OSError:
                pass
            sz = _image_size(path)
            if sz:
                out["width"], out["height"] = sz
            out["name"] = name
        return out

    # ---- ① 引用上已有的（有些通道会填，如本地生成的图像）----
    w = int(getattr(ref, "width", 0) or 0)
    h = int(getattr(ref, "height", 0) or 0)
    if w > 0 and h > 0:
        out["width"], out["height"] = w, h

    b = int(getattr(ref, "bytes", 0) or 0)
    if b > 0:
        out["bytes"] = b

    ms = int(getattr(ref, "duration_ms", 0) or 0)
    if ms > 0:
        out["durationSec"] = ms / 1000.0

    fps = int(getattr(ref, "fps", 0) or 0)
    if fps > 0:
        out["fps"] = fps

    sh = str(getattr(ref, "sha256", "") or "")
    if sh:
        out["sha256"] = sh

    # ---- ② 从文件量（引用字段常为空 —— 见模块 docstring）----
    name = _artifact_name(ref)
    path = _resolve_path(name, resolve)

    if path is not None and path.is_file():
        if "bytes" not in out:
            try:
                out["bytes"] = path.stat().st_size
            except OSError:
                pass
        if "width" not in out:
            sz = _image_size(path)
            if sz:
                out["width"], out["height"] = sz

    # ---- ③ 上游若给了相似度，透传（同时透传算法名，便于追溯）----
    ex = _artifact_extras(ref)
    if "similarity" in ex:
        out["similarity"] = ex["similarity"]
        out["method"] = ex.get("similarityMethod", "unspecified")
    for k in ("otherShotId", "script", "produced", "count"):
        if k in ex:
            out[k] = ex[k]

    return out


def _resolve_path(name: str,
                  resolve: Callable[[str], Any] | None) -> Path | None:
    """把产物名变成真实路径。**解析失败返回 `None`，不抛异常。**

    不抛的理由：解析失败只意味着"这个字段量不出来"，
    门禁会据此把相关约束**显式跳过**（可审计）；
    抛异常则会让整次判定中断，反而更不透明。
    """
    if not name or resolve is None:
        return None
    try:
        got = resolve(name)
    except Exception:                                         # noqa: BLE001
        return None
    if not got:
        return None
    p = Path(got)
    return p if p.is_file() else None


def _artifact_extras(ref: Any) -> Mapping[str, Any]:
    ex = getattr(ref, "extra", None)
    return ex if isinstance(ex, Mapping) else {}


def _artifact_name(ref: Any) -> str:
    """从产物引用取"可交给解析器的名字"。

    `ComfyChannel` 把文件名放在 `extra["filename"]`，而 `uri` 是
    `/view?filename=...` —— **直接拿 uri 去解析会失败**。
    与 `cached_render._name_of` 同一套判据（那里踩过"取到路由词"的坑）。
    """
    ex = _artifact_extras(ref)
    fn = ex.get("filename")
    if fn:
        return str(fn)
    uri = str(getattr(ref, "uri", "") or "")
    if "filename=" in uri:
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(uri).query)
        vals = q.get("filename") or []
        if vals:
            return str(vals[0])
    return uri.rsplit("/", 1)[-1] if uri else ""


def extract_from_artifact(ref: Any) -> dict[str, Any]:
    """兼容入口：不测量的纯字段提取（保留给不需要文件的场景）。"""
    return measure_artifact(ref, None)


def _measure_blob(blob: bytes, name: str,
                  *, want_signature: bool = False) -> dict[str, Any]:
    """从**内存字节**量可测属性。不需要文件系统。

    这是最可靠的路径：字节已在手上，既不需要解析器，
    也不受"文件还在不在磁盘上"影响。
    """
    out: dict[str, Any] = {"bytes": len(blob)}
    if name:
        out["name"] = name
    sz = _image_size_bytes(blob)
    if sz:
        out["width"], out["height"] = sz

    # ---- L2 一致性子（按需）----
    #
    # ⚠️ **解不开就什么都不加，绝不填默认值。**
    # 填一个"空描述子"会让所有相似度变成 0（或 0.5）——
    # 那是**静默的错误结论**，比"观测缺失"糟得多：
    # 观测缺失会被如实报成"跳过"，而假描述子会被当成真的算出一个数。
    if want_signature:
        try:
            from little_crew.review.embed import signature

            out["consistencySignature"] = signature(blob)
        except Exception:                                 # noqa: BLE001
            # 不支持的格式（JPEG/GIF/WebP/16 位 PNG）走这里。
            # 记下原因，便于回答"为什么这条约束没执行"。
            out["consistencyUnavailable"] = "图片格式不支持一致性描述子"
    return out


def _image_size_bytes(blob: bytes) -> tuple[int, int] | None:
    """从字节头判图像尺寸。与 `_image_size` 同一套解析器，只是不吃路径。"""
    head = blob[:65536]
    for fn in (_png_size, _gif_size, _jpeg_size):
        sz = fn(head)
        if sz:
            return sz
    return None


@dataclass(frozen=True)
class ObservableSpec:
    """能力预检的结论：某条约束**能不能**在本次运行里被执行。"""

    constraint_ref: str
    op: str
    missing: frozenset[str] = frozenset()

    @property
    def computable(self) -> bool:
        return not self.missing


@dataclass(frozen=True)
class ObservationPlan:
    """一次运行的能力预检结果。

    **存在的理由**：本机没有人脸/嵌入模型，`identity_similarity` 这类约束
    **算不出来**。若把它们当成"观测缺失"喂给门禁，门禁会对**每一镜**报
    `ESCALATE` —— 那不是"严"，那是**不可用**（全部升级人工 = 门禁等于没有）。

    但也**不能静默跳过** —— 跳过等于"约束声称被检查了，实际没有"，
    而那正是自我签收。

    所以：**算不出的约束被显式移出本次执行集，并留下可审计的记录。**
    这是"缺观测 ≠ 通过"与"可用性"之间的正解 ——
    把沉默的假通过，换成一个喧闹的真跳过。
    """

    executable: tuple[Mapping[str, Any], ...] = ()
    skipped: tuple[ObservableSpec, ...] = ()
    available_fields: frozenset[str] = frozenset()

    @property
    def skipped_refs(self) -> tuple[str, ...]:
        return tuple(s.constraint_ref for s in self.skipped)

    def audit_lines(self) -> list[str]:
        return [
            f"{s.constraint_ref}({s.op}) 未执行：缺观测 {sorted(s.missing)}"
            for s in self.skipped
        ]


def plan_observations(
    constraints: Iterable[Mapping[str, Any]],
    available_fields: Iterable[str],
    *,
    known_ops: Iterable[str] | None = None,
) -> ObservationPlan:
    """预检哪些约束在本机能力下**可执行**。

    未知算子**不在这里过滤** —— 那是门禁的职责（它判 ESCALATE）。
    在这里过滤掉会让"约束写错了"变得不可见。
    """
    avail = frozenset(available_fields)
    known = frozenset(known_ops) if known_ops is not None else None

    exe: list[Mapping[str, Any]] = []
    skip: list[ObservableSpec] = []

    for c in constraints:
        a = c.get("assertion") or {}
        op = str(a.get("op") or "")
        ref = str(c.get("ref") or "")

        if known is not None and op not in known:
            exe.append(c)
            continue

        need = set(OPERATOR_NEEDS.get(op, frozenset()))
        if op in FIELD_PARAM_OPS and a.get("field"):
            need.add(str(a["field"]))

        missing = frozenset(need - avail)
        if missing:
            skip.append(ObservableSpec(ref, op, missing))
        else:
            exe.append(c)

    return ObservationPlan(executable=tuple(exe), skipped=tuple(skip),
                           available_fields=avail)
