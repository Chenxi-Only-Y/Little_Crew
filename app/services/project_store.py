"""工程文件（导演台画布）的持久化。

一个"工程"= 用户在导演台上搭出来的那张图：放哪些节点、连了哪些线、各节点参数。
运行时设置不在这里存（那是全局偏好，属于 `runtime_settings`）。

存储沿用与 `runtime_settings` 相同的做法：**整份 JSON 加密后存一行**。
理由：工程结构会随前端演进变化，用固定列会每改一次就要迁移；
整份 JSON 让旧工程能被新版本读出来（缺失字段按默认补），不会因加字段而失效。

⚠️ 加密原因：`settings_snapshot` 里可能含用户密钥，明文入库不妥。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings
from app.services.runtime_settings import DecryptError, _decrypt, _encrypt

#: 工程文件格式版本。读取时按版本做兼容处理（见 `_migrate`）。
CURRENT_VERSION = 1

#: 单个工程的大小上限（防止塞入超大 base64 把库撑爆）
MAX_PAYLOAD_BYTES = 512 * 1024


class ProjectUnreadable(RuntimeError):
    """工程在库里，但**解不开**（密钥轮换/丢失/数据损坏）。

    ⚠️ 为什么必须显式区分"空工程"与"读不懂的工程"：
        原先 `get()` 把异常吞掉、返回 `{}`，于是前端显示成一个**空画布**。
        用户看到空白、随手保存一次，**原来那份数据就被永久覆盖了**——
        而它本来是能救回来的（只要把密钥修好）。
        "看起来是空的"和"确实是空的"必须有区别，这正是静默失效最伤人的形态。
    """


def _db_path() -> str:
    """SQLite 库路径。与 `auth.get_user_store` 用同一处配置，保证落在同一个库。"""
    return str(settings.db_path_value)


def _now() -> str:
    """时间戳（带微秒）。

    为什么需要微秒：`updated` 用于"按最近修改排序"，而秒级精度下
    同一秒内连续保存的工程顺序不稳定（实测 A、B 同秒创建时排序会颠倒）。
    """
    return time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time() * 1_000_000) % 1_000_000:06d}"


class ProjectStore:
    """工程文件的增删改查。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _db_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init_db(self) -> None:
        c = self._conn()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                name       TEXT NOT NULL,
                payload    TEXT NOT NULL,
                created    TEXT NOT NULL,
                updated    TEXT NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id, updated)")
        c.commit()
        c.close()

    # ---------------- 保存 ----------------

    def save(
        self,
        user_id: str,
        name: str,
        canvas: dict,
        settings_snapshot: Optional[dict] = None,
        project_id: Optional[str] = None,
        *,
        allow_overwrite_unreadable: bool = False,
    ) -> dict:
        """保存（新建或覆盖）。返回工程概要。

        Args:
            canvas: `{nodes: [...], edges: [...], viewport: {...}}`
            settings_snapshot: 可选的运行时设置快照（**密钥已掩码**，见 API 层）
            project_id: 传入则覆盖同 id 的工程（且必须属于同一 user）
            allow_overwrite_unreadable: 覆盖一份**解不开**的既有工程时需要显式置 True。
                默认 False：那种情况下直接抛错，避免"用户以为在改一个空工程、
                实际把能救回的数据永久覆盖掉"（见 `ProjectUnreadable`）。
                前端在用户**明确确认**后可以传 True。
        """
        now = _now()
        pid = project_id or f"prj_{uuid.uuid4().hex[:12]}"

        # 注意运算顺序：必须**先 strip 再取默认**。
        # 写成 `(name).strip()[:80] or "未命名"` 会先截断成空串，
        # `"" or "未命名"` 虽然对，但 `name.strip()[:80] or ...` 在 name 全为空白时
        # 会先得到 ""——故显式分两步，避免后人改动时又踩。
        clean_name = (name or "").strip()[:80]
        if not clean_name:
            clean_name = "未命名工程"

        doc = {
            "version": CURRENT_VERSION,
            "name": clean_name,
            "canvas": _norm_canvas(canvas),
            "settings_snapshot": settings_snapshot or {},
            "saved_at": now,
        }
        blob = json.dumps(doc, ensure_ascii=False)
        if len(blob.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"工程过大（{len(blob.encode('utf-8')) // 1024}KB，上限 "
                f"{MAX_PAYLOAD_BYTES // 1024}KB）。请减少节点数量或去掉大体积内联数据。"
            )

        c = self._conn()
        if project_id:
            row = c.execute(
                "SELECT user_id,payload FROM projects WHERE project_id=?", (pid,)
            ).fetchone()
            if row and row[0] != user_id:
                c.close()
                raise PermissionError("该工程属于其它用户，无法覆盖")
            # 覆盖前确认旧内容**读得懂**——否则这次保存会永久毁掉一份
            # 本来能救回的数据（密钥修好即可恢复）。要覆盖必须显式声明。
            if row and row[1] and not allow_overwrite_unreadable:
                try:
                    _decrypt(row[1])
                except (DecryptError, ValueError, TypeError) as exc:
                    c.close()
                    raise ProjectUnreadable(
                        "既有工程无法解密，为避免永久覆盖已中止保存。"
                        "请先修复 data/.little_crew.key；"
                        "若确认要丢弃旧内容，请显式选择覆盖。"
                    ) from exc
            c.execute(
                "UPDATE projects SET name=?, payload=?, updated=? WHERE project_id=?",
                (doc["name"], _encrypt(blob), now, pid),
            )
        else:
            c.execute(
                "INSERT INTO projects(project_id,user_id,name,payload,created,updated) "
                "VALUES(?,?,?,?,?,?)",
                (pid, user_id, doc["name"], _encrypt(blob), now, now),
            )
        c.commit()
        c.close()
        return {"project_id": pid, "name": doc["name"], "updated": now,
                "node_count": len(doc["canvas"]["nodes"])}

    # ---------------- 读取 ----------------

    def get(self, project_id: str, user_id: str) -> Optional[dict]:
        c = self._conn()
        row = c.execute(
            "SELECT user_id,name,payload,created,updated FROM projects WHERE project_id=?",
            (project_id,),
        ).fetchone()
        c.close()
        if not row:
            return None
        owner, name, payload, created, updated = row
        if owner != user_id:
            # 不区分"不存在"与"无权限"，避免探测他人工程 id
            return None
        try:
            doc = json.loads(_decrypt(payload) or "{}")
        except (DecryptError, ValueError, TypeError) as exc:
            # **不再静默返回空工程**：那会让用户以为是空的、随手保存就把
            # 能救回的数据永久覆盖。改为显式抛错，让上层给出可操作提示。
            raise ProjectUnreadable(
                f"工程「{name}」无法解密（{type(exc).__name__}）。"
                "数据仍在库中；请检查 data/.little_crew.key 是否被替换或删除，"
                "**在修好之前不要覆盖保存**。"
            ) from exc
        doc = _migrate(doc)
        doc.update({"project_id": project_id, "name": name,
                    "created": created, "updated": updated})
        return doc

    def list_by_user(self, user_id: str) -> list:
        c = self._conn()
        rows = c.execute(
            "SELECT project_id,name,payload,created,updated FROM projects "
            "WHERE user_id=? ORDER BY updated DESC",
            (user_id,),
        ).fetchall()
        c.close()
        out = []
        for pid, name, payload, created, updated in rows:
            node_count = 0
            unreadable = False
            try:
                doc = json.loads(_decrypt(payload) or "{}")
                node_count = len(((doc.get("canvas") or {}).get("nodes")) or [])
            except (DecryptError, ValueError, TypeError):
                # 列表页**不抛**（否则一个坏工程会让整个列表打不开），
                # 但必须把"这份读不懂"标出来，让前端能提示而不是显示成 0 个节点。
                unreadable = True
            out.append({"project_id": pid, "name": name, "node_count": node_count,
                        "created": created, "updated": updated,
                        "unreadable": unreadable})
        return out

    def delete(self, project_id: str, user_id: str) -> bool:
        c = self._conn()
        cur = c.execute(
            "DELETE FROM projects WHERE project_id=? AND user_id=?", (project_id, user_id)
        )
        c.commit()
        n = cur.rowcount
        c.close()
        return n > 0

    def rename(self, project_id: str, user_id: str, name: str) -> bool:
        clean = (name or "").strip()[:80] or "未命名工程"
        c = self._conn()
        cur = c.execute(
            "UPDATE projects SET name=?, updated=? WHERE project_id=? AND user_id=?",
            (clean, _now(), project_id, user_id),
        )
        c.commit()
        n = cur.rowcount
        c.close()
        return n > 0


def _norm_canvas(canvas: Any) -> dict:
    """把画布规范成固定形状，避免存入半截数据导致读出来崩。

    保留 `previs`（3D 预演场景）：那不是画布的一部分，但复用同一份工程存储。
    若在这里丢掉它，用户保存的预演场景会被**静默清空**——打开时才发现白摆了。
    """
    canvas = canvas if isinstance(canvas, dict) else {}
    nodes = canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else []
    edges = canvas.get("edges") if isinstance(canvas.get("edges"), list) else []
    viewport = canvas.get("viewport") if isinstance(canvas.get("viewport"), dict) else {}

    clean_nodes = []
    for n in nodes:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        pos = n.get("position") if isinstance(n.get("position"), dict) else {}
        clean_nodes.append({
            "id": str(n["id"]),
            "position": {"x": float(pos.get("x") or 0), "y": float(pos.get("y") or 0)},
            "config": n.get("config") if isinstance(n.get("config"), dict) else {},
            "note": str(n.get("note") or "")[:500],
        })

    clean_edges = []
    for e in edges:
        if not isinstance(e, dict) or not e.get("source") or not e.get("target"):
            continue
        clean_edges.append({
            "source": str(e["source"]),
            "target": str(e["target"]),
            "sourceHandle": e.get("sourceHandle"),
            "targetHandle": e.get("targetHandle"),
        })

    out = {"nodes": clean_nodes, "edges": clean_edges, "viewport": viewport}

    # 3D 预演场景：只做类型与体积的基本校验，结构交给前端自描述
    # （预演的数据模型会演进，后端不该跟着改一次就迁移一次）
    previs = canvas.get("previs")
    if isinstance(previs, dict) and isinstance(previs.get("actors"), list):
        out["previs"] = previs

    return out


def _migrate(doc: dict) -> dict:
    """按版本补齐缺失字段（向前兼容：旧工程也能读）。

    原则：**只补不删**。新版本新增字段时，旧工程读出来是空值，由前端按默认处理，
    而不是因为缺字段直接报错。
    """
    doc = doc if isinstance(doc, dict) else {}
    doc.setdefault("version", 1)
    doc["canvas"] = _norm_canvas(doc.get("canvas"))
    doc.setdefault("settings_snapshot", {})
    doc.setdefault("name", "未命名工程")
    return doc


_store: Optional[ProjectStore] = None


def get_project_store() -> ProjectStore:
    global _store
    if _store is None:
        _store = ProjectStore()
    return _store
