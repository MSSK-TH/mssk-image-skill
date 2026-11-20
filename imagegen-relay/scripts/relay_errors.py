"""把上游 / 中转站的报错分类，好让调用方（agent 或人）知道"下一步能做什么"。

来源与维护约定
--------------
本文件是 image-web 后端 ``lib/errors.mjs`` 的 Python 对应物。
**两份要同步维护**：改动任何一条规则，两个仓库都要改。
以本文件（Python）为准 —— skill 是主战场，image-web 已冻结。

三个函数
--------
``humanize_moderation(stage, categories, request_id)``
    内容安全拒绝 → 一句中文（含判定阶段与请求 ID）

``translate_upstream(text)``
    上游偶尔回英文原话 → 一句中文摘要；认不出就返回空串

``classify_error(text)``
    归类 → ``{"code": ..., "hint": ..., "retryable": bool}``
    ``retryable`` 是给 agent 的决策信号：True 才值得再发一次

两条过载必须分清（实测结论）
----------------------------
* ``server_is_overloaded`` → **上游（OpenAI）** 过载：HTTP 200，但流里带 error 事件，
  且 ``x-retry-metadata: NO_MORE_RETRY``（上游在说"别再重试了"）
* ``system cpu overloaded`` → **中转站自己**的 CPU 打满（1 核），HTTP 503 直接被拒

口诀：HTTP 200 但流里报错 = 上游；HTTP 503 直接被拒 = 我们的 CPU。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

__all__ = [
    "humanize_moderation",
    "translate_upstream",
    "classify_error",
    "describe_failure",
]

_STAGE_ZH = {
    "input": "提示词/参考图阶段就被拦下",
    "output": "图已经画好，只是被判掉",
}

_CATEGORY_ZH = {
    "sexual": "性化/暴露",
    "violence": "暴力",
    "self-harm": "自残",
    "harassment": "骚扰",
    "hate": "仇恨",
    "minors": "未成年人相关",
    "sexual/minors": "未成年人性化",
}


def humanize_moderation(
    stage: str = "",
    categories: str = "",
    request_id: str = "",
) -> str:
    """内容安全拒绝 → 一句中文（含可选的请求 ID）。"""
    s = _STAGE_ZH.get(str(stage or "").lower(), str(stage or ""))
    cats = [
        _CATEGORY_ZH.get(c.lower(), c)
        for c in re.split(r"[,，\s]+", str(categories or ""))
        if c
    ]
    cat = "、".join(cats)
    parts = [
        f"图片被上游安全系统判定为「{cat}」内容，没有出图"
        if cat
        else "图片被上游安全系统拒绝，没有出图",
        f"（{s}）" if s else "",
        f" · 请求 ID：{request_id}" if request_id else "",
    ]
    return "".join(p for p in parts if p)


_TRANSLATE_RULES = [
    (
        re.compile(
            r"couldn'?t generate this image|unable to generate this image|"
            r"was flagged by the image safety system|rejected by the image",
            re.I,
        ),
        "上游图像安全系统判定这张图/这条提示词不合规，没有生成",
    ),
    (re.compile(r"server_is_overloaded|currently overloaded", re.I), "上游（OpenAI）正在过载"),
    (re.compile(r"system cpu overloaded", re.I), "我们中转站自己的 CPU 打满了"),
    (re.compile(r"token_revoked|invalidated oauth", re.I), "中转站的授权凭据被踢了"),
    (re.compile(r"no access to model", re.I), "这个令牌没有该模型的权限"),
]


def translate_upstream(text: str = "") -> str:
    """上游偶尔回英文原话 → 给一句中文摘要；认不出返回空串。"""
    t = str(text or "")
    if not t:
        return ""
    for pattern, zh in _TRANSLATE_RULES:
        if pattern.search(t):
            return zh
    return ""


# (code, 正则, 是否值得重试, 给用户/agent 的下一步提示)
_RULES = [
    (
        "token_invalid",
        re.compile(r"invalid token|invalid api key|rejected the token|authentication_error", re.I),
        False,
        "中转站不认这个令牌：检查 config.json 里的 api_key 是否为有效的 sk- 令牌（不是管理员密码），"
        "以及令牌是否被禁用、是否绑定到了别的分组。",
    ),
    (
        "content_safety",
        re.compile(r"moderation_blocked|内容安全系统拒绝（原生端点）|判定阶段="),
        False,
        "这不是故障：图片已经渲染完成，被上游的图像安全系统判成了不允许的内容（多为「性化/暴露」类）。"
        "判定是随机的，再发一次仍有机会（实测约 1/3），但通常换参考图更快 —— "
        "同一角色换一张穿得更保守的图，通过率明显更高。",
    ),
    (
        "model_refusal",
        re.compile(r"抱歉[，,]\s*我(?:无法|不能)|我无法(?:直接)?(?:基于|按)这张|I (?:can'?t|cannot|am unable to)", re.I),
        False,
        "这次是对话模型自己拒绝（不是内容安全系统）：它不愿意基于这张参考图做合成，还给了替代方案。"
        "重试没有意义（同一段拒绝是确定的）。两个办法：① 换个说法（明确写「把参考图1的角色合成进新场景、保持人物不变」）；"
        "② 走原生图像端点（--model codex-gpt-image-2）—— 那条路没有对话模型，这类拒绝会消失。",
    ),
    (
        "content_refusal",
        re.compile(r"内容安全|安全系统|safety system|image safety|flagged by the image|rejected by the image", re.I),
        True,
        "不是故障，是上游的图像安全系统把这个提示词/参考图判成了不允许的内容"
        "（常见触发：人物服装暴露或性化、真人肖像、暴力）。"
        "判定发生在出图之后且是随机的（实测同一张图重试约 1/3 能过）—— 所以再发一次有意义，但换一张更保守的参考图见效更快。",
    ),
    (
        "no_image_data",
        re.compile(r"没有返回图片数据|no image data", re.I),
        True,
        "上游这次 HTTP 200 但流里没有图（实测是「成簇」发生：同一个请求上一次成功、这一次就空手而归）。"
        "再发一次通常第二次就出来了。",
    ),
    (
        "upstream_overloaded",
        re.compile(r"server_is_overloaded|currently overloaded", re.I),
        False,
        "上游（OpenAI）过载，不是你或中转站的问题。上游明确回了 NO_MORE_RETRY —— 猛点重试会更糟，等 1～2 分钟再试。"
        "参考图会让请求更重，过载时更容易撞上。",
    ),
    (
        "relay_cpu",
        re.compile(r"system cpu overloaded", re.I),
        False,
        "这是中转站自己的 CPU 打满了（1 核机器），和上游无关。等一会儿再试；如果经常发生，该升级到 2 核了。",
    ),
    (
        "token_revoked",
        re.compile(r"token_revoked|invalidated oauth", re.I),
        False,
        "中转站凭据被踢了 —— 需要在服务器上重跑「重新播种凭据」脚本，或让 owner 重新登录上游账号。",
    ),
    (
        "no_model_access",
        re.compile(r"no access to model", re.I),
        False,
        "这个令牌没有该模型的权限，去 new-api 后台的令牌限制里把它加上。",
    ),
    (
        "bad_request",
        re.compile(r"Invalid value|Unsupported value|not supported", re.I),
        True,
        "请求参数被上游拒绝（比如不支持的推理档位/工具参数）—— 降一档 effort 再试。",
    ),
]


def classify_error(text: str = "") -> Dict[str, Any]:
    """归类错误 → ``{"code", "hint", "retryable"}``。"""
    t = str(text or "")
    for code, pattern, retryable, hint in _RULES:
        if pattern.search(t):
            return {"code": code, "hint": hint, "retryable": retryable}
    return {
        "code": "upstream_error",
        "hint": "上游返回了没有归类的错误。可以原样重试一次；连续出现就把原始报错贴出来。",
        "retryable": True,
    }


def describe_failure(
    text: str = "",
    *,
    http_status: Optional[int] = None,
    stage: str = "",
    categories: str = "",
    request_id: str = "",
) -> Dict[str, Any]:
    """一次性产出一份可打印、可写进 report-json 的失败描述。

    返回::

        {
          "code": "content_refusal",
          "hint": "...",
          "retryable": False,
          "zh": "一句中文摘要",
          "http_status": 200,
          "raw": "原始报错（截断）"
        }
    """
    raw = str(text or "")
    cls = classify_error(raw)
    zh = translate_upstream(raw)
    if not zh and stage:
        zh = humanize_moderation(stage=stage, categories=categories, request_id=request_id)
    if not zh:
        if http_status:
            zh = f"中转站返回 HTTP {http_status}，没有出图"
        else:
            zh = "出图失败"
    return {
        "code": cls["code"],
        "hint": cls["hint"],
        "retryable": cls["retryable"],
        "zh": zh,
        "http_status": http_status,
        "raw": raw[:600],
    }
