"""请求体模板渲染与响应取值 —— HTTP 型 ``Backend`` 的**契约实现**。

为什么在 `models` 层而不是 `runtime`
-----------------------------------
这两个函数处理的是 ``Backend.body_template`` 与 ``Backend.response_path``
**这两个字段的语义**。而 ``Backend`` 定义在 `models` ——
**契约的实现在契约旁边**。

⚠️ 踩过的坑：它们最初写在 `runtime/channels/http.py`。当时只有 `runtime`
用它，没问题。后来 `agent/llm.py` 也需要（接 LLM 时必须渲染请求体、
按路径取响应），而 `agent`(层 6) **不能** import `runtime`(层 8) ——
守卫立刻报反向依赖。

**正确的反应不是把守卫放宽，而是把东西放到对的地方。**
`runtime/channels/http.py` 现在从这里导入并**再导出**，
所以既有调用方（`render.py`、测试）无需改动。

零第三方依赖
------------
只用标准库 ``json`` 与 ``re``。
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

__all__ = ["TemplateError", "render_body_template", "extract_path"]


class TemplateError(ValueError):
    """模板渲染失败。``http.py` 会把它转成该层的 ``ChannelError``。

    为什么在这里定义一个自己的异常而不是复用 ``ChannelError``：
    那会造成 `models` -> `runtime` 的反向依赖 —— 正是本模块要解决的问题。
    """

    def __init__(self, code: str, message: str,
                 detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


#: 模板占位符。只认 ``{name}`` 形式（不匹配 JSON 的花括号，
#: 因为 JSON 的花括号后面跟的是引号或换行，不是标识符直接接右花括号）。
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_body_template(template: str,
                         values: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``body_template`` 里的占位符替换成实际值，返回可提交的 dict。

    **关键安全点 1：引号感知**
    模板里通常写成 ``"prompt": "{prompt}"``（占位符已被引号包住）。
    若此时把 ``json.dumps(v)`` 的结果（**自带外层引号**）塞进去，
    会得到 ``""值""`` —— 坏 JSON。因此本函数检测占位符两侧是否已有引号：

      - 已有引号 -> 插入**去掉外层引号**的转义内容
      - 没有引号 -> 插入完整的 JSON 字面量（字符串自带引号，数值/布尔为裸字面量）

    这样模板作者写哪种形式都正确。

    **关键安全点 2：必须用 json.dumps 转义**
    提示词里出现引号、反斜杠、换行是常态。手工拼接字符串会在第一个引号处
    破坏 JSON 结构 —— 这是手写 JSON 模板最常见的严重缺陷。

    **关键安全点 3：替换后必须重新解析校验**
    宁可在这里报错，也不要让一个坏 JSON 发出去（云端会返回难懂的 400）。

    **关键安全点 4：容器要序列化成 JSON 字面量，不能 ``str()``**
    ⚠️ 踩过：早先列表/字典走了字符串分支，于是
    ``[{"role": "user", …}]`` 变成**字符串** ``"[{'role': 'user', …}]"``
    （Python repr，单引号），服务端报 ``messages: invalid type: string``。
    这类错误极难归因 —— **模板对、值对、也是合法 JSON**，
    只是语义从"数组"变成了"字符串"。
    """
    rendered = template
    for key, val in values.items():
        token = "{" + key + "}"
        idx = rendered.find(token)
        if idx < 0:
            continue

        # 数值/布尔/空 -> 裸字面量（模板里本来就不该给它们加引号）
        if isinstance(val, bool):
            lit = "true" if val else "false"
        elif val is None:
            lit = "null"
        elif isinstance(val, (int, float)):
            lit = json.dumps(val)
        elif isinstance(val, (list, tuple, Mapping)):
            # **容器必须序列化成 JSON 字面量**，不能先 str() —— 见上面第 4 点
            lit = json.dumps(list(val) if isinstance(val, tuple) else val,
                             ensure_ascii=False)
        else:
            encoded = json.dumps(str(val), ensure_ascii=False)  # 自带外层引号
            before = rendered[idx - 1] if idx > 0 else ""
            after_pos = idx + len(token)
            after = rendered[after_pos] if after_pos < len(rendered) else ""
            if before == '"' and after == '"':
                lit = encoded[1:-1]      # 模板已加引号，只插转义后的内容
            else:
                lit = encoded            # 模板未加引号，插入完整字符串字面量

        rendered = rendered.replace(token, lit)

    # **未替换的占位符必须报错，不能静默发出去。**
    #
    # 早期版本直接进入下面的 json.loads —— 而 `{"prompt": "{prompt}"}`
    # 本身是**合法 JSON**，于是校验通过、请求发出、服务收到一个字面量
    # `"{prompt}"` 并返回一张与提示词毫无关系的图。
    #
    # 这类失败极难归因：服务不报错、我们有产物、图也"看起来像那么回事"。
    # 一个拼错的占位符（`{promt}`）就能触发它。
    leftover = _PLACEHOLDER_RE.findall(rendered)
    if leftover:
        raise TemplateError(
            "F-SUBMIT-TEMPLATE",
            f"请求体模板里有未替换的占位符 {sorted(set(leftover))} —— "
            f"模板要求的入参与实际提供的不一致（拼写错误？）",
            detail={"missing": sorted(set(leftover)),
                    "provided": sorted(values),
                    "rendered": rendered[:400]},
        )

    try:
        obj = json.loads(rendered)
    except json.JSONDecodeError as e:
        raise TemplateError(
            "F-SUBMIT-VALID",
            f"请求体模板渲染后不是合法 JSON: {e}",
            detail={"rendered": rendered[:400]},
        ) from e
    if not isinstance(obj, dict):
        raise TemplateError("F-SUBMIT-VALID", "请求体模板渲染后不是 JSON 对象")
    return obj


def extract_path(obj: Any, path: str) -> Any:
    """按点号路径取值。支持 ``data.0.url`` 这类列表下标。

    取不到返回 ``None``（由调用方判定是否致命），不抛异常 ——
    某些接口在失败时返回结构不同，取不到值本身就是要判断的信息。
    """
    if not path:
        return obj
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, Mapping):
            cur = cur.get(seg)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur
