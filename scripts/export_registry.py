"""导出注册表快照，供前端开发/离线渲染使用。

为什么要这个文件：
    新前端启动时还没有登录 token，而 `GET /api/registry` 受鉴权保护。
    让前端在开发期依赖一份"后端生成的"快照，既能立刻开发，
    又不会像手写 fixture 那样与后端漂移（本文件由注册表**直接导出**）。

用法：
    python scripts/export_registry.py            # 写入 web/src/registry.snapshot.json
    python scripts/export_registry.py --out X.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.registry import api as registry  # noqa: E402

DEFAULT_OUT = ROOT / "web" / "src" / "registry.snapshot.json"


def snapshot_json() -> str:
    """注册表快照的**规范序列化**。

    单独抽出来是为了让"导出"与"校验快照是否过期"共用同一段逻辑：
    若各写一份，两边格式一旦不同就会互相打架（而漂移守卫本身失效最危险——
    它会一直"通过"，掩盖真正的漂移）。
    """
    snap = registry.snapshot()
    return json.dumps(snap, ensure_ascii=False, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="导出注册表快照（供前端）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径")
    args = ap.parse_args()

    snap = registry.snapshot()
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(snapshot_json(), encoding="utf-8")

    print(f"已导出：{out}")
    print(f"  部门 {len(snap['departments'])} / 职业 {len(snap['roles'])} / "
          f"节点 {len(snap['nodes'])} / 产物 {len(snap['artifacts'])} / "
          f"确认点 {len(snap['checkpoints'])}")
    print(f"  执行顺序：{' → '.join(snap['execution_order'][:6])} …")
    print(f"  文件大小：{out.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
