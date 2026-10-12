"""设置加密故障的回归测试。

`DecryptError` 的文案是**唯一有用的排障指引**（"请先修复密钥文件
`.little_crew.key`，数据仍在库里"）。原先全仓没有异常处理器，于是它落到
通用兜底，用户只看到"服务内部错误，请稍后重试"——完全不知道该做什么。

同时锁住两个不能退化的性质：
  1. 密钥与数据不匹配时**必须拒绝覆盖**（否则一次保存就把能救回的数据永久清掉）；
  2. 密钥不匹配**不能让服务起不来**（导入期不能抛）。
"""

import subprocess
import sys
from pathlib import Path

import pytest

#: 项目真实密钥的期望指纹（跑测试前记录）。
#: ⚠️ 这些测试会在**临时目录**里生成/替换密钥；下面每个脚本都带路径断言，
#:    保证 `_SECRET_PATH` 落在临时目录内才写文件——否则一旦环境变量没生效，
#:    就会改写项目真实密钥、把库里所有密文弄坏。
_REAL_KEY_SHA_PREFIX = "e37245fc04200391"

_GUARD = ("assert str(_SECRET_PATH).startswith(sys.argv[1]), "
          "f'拒绝执行，密钥路径不在临时目录: {_SECRET_PATH}'")

_STEP_WRITE = f'''
import sys
from app.services.runtime_settings import _SECRET_PATH
{_GUARD}
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
c.post("/api/auth/register", json={{"username":"k1","password":"test1234"}})
tok = c.post("/api/auth/login", json={{"username":"k1","password":"test1234"}}).json()["token"]
c.put("/api/settings", headers={{"Authorization":"Bearer "+tok}},
      json={{"llm_base_url":"https://example.invalid/v1/long/path/force/ciphertext"}})
print("ok")
'''

_STEP_PUT = f'''
import sys
from app.services.runtime_settings import _SECRET_PATH
{_GUARD}
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
tok = c.post("/api/auth/login", json={{"username":"k1","password":"test1234"}}).json()["token"]
r = c.put("/api/settings", headers={{"Authorization":"Bearer "+tok}}, json={{"max_images":9}})
b = r.json()
print(r.status_code)
print(str(b.get("message","")))
print(str(b.get("hint","")))
'''


def _run(code: str, cwd_arg: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        **__import__("os").environ,
        "DB_PATH": str(tmp_path / "t.db"),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
    }
    return subprocess.run(
        [sys.executable, "-c", code, str(cwd_arg)],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


@pytest.fixture()
def rotated_key(tmp_path):
    """建账号写好设置 → 换掉密钥 → 返回临时目录。

    必须**换进程**：`_fernet()` 有缓存，同进程里换密钥文件不生效
    （这一点我踩过——测试因此假绿）。
    """
    _run(_STEP_WRITE, tmp_path, tmp_path)
    from cryptography.fernet import Fernet

    key = tmp_path / ".little_crew.key"
    assert str(key).startswith(str(tmp_path)), "安全断言：密钥必须落在临时目录"
    key.write_bytes(Fernet.generate_key())
    return tmp_path


def test_decrypt_failure_returns_actionable_message(rotated_key):
    """密钥不匹配时：500 + **可操作提示**（而不是通用"服务内部错误"）。"""
    r = _run(_STEP_PUT, rotated_key, rotated_key)
    lines = [x for x in (r.stdout or "").strip().splitlines() if x.strip()]
    assert lines, f"子进程无输出；stderr={r.stderr[-300:]}"
    assert lines[0] == "500", f"状态码应为 500，实际 {lines[0]}"
    blob = "\n".join(lines)
    assert "little_crew.key" in blob, f"提示里应指出要修哪个文件：{blob[:200]}"


def test_decrypt_failure_does_not_overwrite_data(rotated_key):
    """密钥不匹配时**不能覆盖**原数据（否则能救回的数据被永久清掉）。"""
    import sqlite3

    before = sqlite3.connect(str(rotated_key / "t.db")).execute(
        "select length(payload) from api_settings where user_id='k1'"
    ).fetchone()[0]
    _run(_STEP_PUT, rotated_key, rotated_key)
    after = sqlite3.connect(str(rotated_key / "t.db")).execute(
        "select length(payload) from api_settings where user_id='k1'"
    ).fetchone()[0]
    assert before == after, f"数据被改写了：{before} → {after}"


def test_key_mismatch_does_not_prevent_import():
    """密钥与数据不匹配时**必须还能 import**（服务要能起来）。

    原先解密失败返回密文 → `json.loads(密文)` 在**导入期**抛
    `JSONDecodeError` → 整个服务启动失败，报错还指不到"密钥不对"这个真因。
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="ds6_imp_"))
    _run(_STEP_WRITE, tmp, tmp)
    from cryptography.fernet import Fernet

    (tmp / ".little_crew.key").write_bytes(Fernet.generate_key())
    r = _run("import app.services.runtime_settings; print('IMPORT_OK')", tmp, tmp)
    assert "IMPORT_OK" in (r.stdout or ""), (
        f"密钥不匹配导致导入失败：rc={r.returncode} stderr={r.stderr[-300:]}"
    )
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
