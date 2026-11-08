"""跨族审查：**判定的独立性**（2026-11-08）。

## 为什么要单独一个模块

`governance/model_routing.py` 负责"任务 -> 模型"的**表**（纯数据，不依赖谁）；
而"族"要从**端点域名**读，必须 import `agent.llm` —— 那是上层。
层次守卫不允许 governance 反向依赖 agent，所以这部分放到 runtime 层。

## 纪律

同族 = 模型审自己写的稿 = **自我验证盲区**；
族未知时**判不独立**（宁严不松），绝不假定跨族。
"""
from __future__ import annotations


def family_of(model_id: str) -> str:
    """模型 -> **族**（按端点域名判，不按名字猜）。

    为什么看域名：厂商会用别名路由（实测请求 `deepseek-chat` 实际服务
    `deepseek-flash`）；名字可以改，**服务端域名改不了**。

    认不出返回 `"unknown"` —— 未知时**不能假定跨族**。
    """
    try:
        from little_crew.agent.llm import model_spec_for

        spec = model_spec_for(str(model_id))
        backend = (getattr(spec, "backends", None) or [None])[0]
        url = str(getattr(backend, "endpoint", "") or "")
        host = url.split("://")[-1].split("/")[0].split(":")[0]
        if not host:
            return "unknown"
        parts = [x for x in host.split(".") if x and x not in ("www", "api")]
        # 取倒数第二段之前的主干：api.deepseek.com -> deepseek
        #                      open.bigmodel.cn -> bigmodel
        return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "unknown")
    except Exception:                                         # noqa: BLE001
        return "unknown"


def cross_family(gen_model: str, judge_model: str) -> dict:
    """判断"审查方"与"生成方"是否**跨族**。"""
    g, j = family_of(gen_model), family_of(judge_model)
    if g == "unknown" or j == "unknown":
        return {"independent": False, "genFamily": g, "judgeFamily": j,
                "reason": "族未知，无法证明独立（**不假定跨族**）"}
    if g == j:
        return {"independent": False, "genFamily": g, "judgeFamily": j,
                "reason": f"同族（{g}）：模型审自己写的稿 = 自我验证盲区"}
    return {"independent": True, "genFamily": g, "judgeFamily": j,
            "reason": f"跨族（{g} vs {j}）"}


#: 审查方的候选模型（按优先级；会在其中挑第一个**跨族且存在**的）
JUDGE_CANDIDATES = ("GLM-5.3-flash", "mimo-v2.6-flash", "deepseek-flash")


def pick_judge(gen_model: str, candidates=None) -> dict:
    """为生成模型挑一个**跨族**的审查模型。

    返回 `{ok, model, independent, reason, ...}`；**挑不到就 ok=False** ——
    宁可没有审查，也不要用同族审查假装独立。
    """
    gen_family = family_of(gen_model)
    for cand in (candidates or JUDGE_CANDIDATES):
        if str(cand) == str(gen_model):
            continue
        r = cross_family(gen_model, str(cand))
        if r["independent"]:
            return {"ok": True, "model": str(cand), **r}
    return {"ok": False, "model": "", "independent": False,
            "genFamily": gen_family, "judgeFamily": "",
            "reason": f"没有可用的跨族审查模型（生成方族={gen_family}）"}
