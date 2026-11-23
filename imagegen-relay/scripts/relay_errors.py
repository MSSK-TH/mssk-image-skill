"""把上游 / 中转站的报错分类，好让调用方（agent 或人）知道"下一步能做什么"。

来源与维护约定
--------------
本文件是错误分类、人话化与脱敏的**唯一真相源**（也是 image-web 后端 ``lib/errors.mjs`` 的
Python 对应物；以本文件为准，image_gen.py 不再重复维护同类正则或映射表）。

核心函数
--------
``humanize_moderation(stage, categories, request_id)``
    内容安全拒绝字段 → 一句中文（含判定阶段与请求 ID）

``humanize_moderation_text(message)``
    从服务端原文提取 ``判定阶段/类别/请求 ID`` 并转人话（不再误切 ``sexual/minors``）

``translate_upstream(text)``
    上游偶尔回英文原话 → 一句中文摘要；认不出就返回空串

``redact_secrets(text, extra_secrets=())``
    把报错文本中可能夹带的 ``sk-...`` 令牌 / Bearer 头 / 指定凭据替换为 ``[REDACTED]``

``classify_error(text, *, http_status=None, code=None)``
    归类 → ``{"code": ..., "hint": ..., "retryable": bool}``
    支持按已知 ``code`` 精确取元信息，也支持用 ``http_status`` 兜底（避免裸 401/429 落入未知错误）

``describe_failure(text, *, http_status=None, code=None, stage="", categories="", request_id="", secrets=())``
    一次性产出可打印、可落 ``--report-json``、且已脱敏的失败描述

两条过载必须分清（实测结论）
----------------------------
* ``server_is_overloaded`` → **上游（OpenAI）** 过载：HTTP 200，但流里带 error 事件，
  且 ``x-retry-metadata: NO_MORE_RETRY``（上游在说"别再重试了"）
* ``system cpu overloaded`` → **中转站自己**的 CPU 打满（1 核），HTTP 503 直接被拒

口诀：HTTP 200 但流里报错 = 上游；HTTP 503 直接被拒 = 我们的 CPU。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional, Tuple

__all__ = [
    "humanize_moderation",
    "humanize_moderation_text",
    "translate_upstream",
    "redact_secrets",
    "is_refusal_code",
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

_BEARER_RE = re.compile(r"(Authorization\s*:\s*Bearer\s+|Bearer\s+)([^\s\"',;]+)", re.I)
_SK_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}")


def redact_secrets(text: str = "", extra_secrets: Iterable[str] = ()) -> str:
    """脱敏报错或日志文本中的令牌，防止写入 stderr 或 --report-json。"""
    out = str(text or "")
    if not out:
        return ""
    for secret in extra_secrets:
        s = str(secret or "").strip()
        if len(s) >= 6 and s in out:
            out = out.replace(s, "[REDACTED]")
    out = _BEARER_RE.sub(r"\1[REDACTED]", out)
    out = _SK_TOKEN_RE.sub("[REDACTED]", out)
    return out


def humanize_moderation(
    stage: str = "",
    categories: str = "",
    request_id: str = "",
) -> str:
    """内容安全拒绝 → 一句中文（含可选的请求 ID）。"""
    s = _STAGE_ZH.get(str(stage or "").lower(), str(stage or ""))
    cats = [
        _CATEGORY_ZH.get(c.strip().lower(), c.strip())
        for c in re.split(r"[,，\s]+", str(categories or ""))
        if c.strip()
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


def humanize_moderation_text(message: str = "") -> str:
    """从上游原文提取 ``判定阶段/类别/请求 ID`` 并转成统一中文；无字段时原样返回。"""
    text = str(message or "")
    if "判定阶段" not in text and "类别" not in text:
        return text
    stage_m = re.search(r"判定阶段[=:：]\s*([A-Za-z_-]+)", text)
    cats_m = re.search(r"类别[=:：]\s*([A-Za-z0-9_/-，,\s]+?)(?:，|,|请求|$)", text)
    rid_m = re.search(r"请求\s*ID[=:：]\s*([0-9a-fA-F-]{8,})", text)
    return humanize_moderation(
        stage=stage_m.group(1) if stage_m else "",
        categories=cats_m.group(1) if cats_m else "",
        request_id=rid_m.group(1) if rid_m else "",
    )


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
    (re.compile(r"no available channel|model_not_found|has no channel", re.I), "中转站没有该模型的可用渠道"),
    (
        re.compile(r"prepare\s*没返回\s*conduit_token|conduit_token", re.I),
        "网页管线会话准备失败（多为 model slug 不存在或与当前 chat/work 模式不匹配）",
    ),
    (
        re.compile(r"422\s+Invalid conversation body|Invalid conversation body", re.I),
        "网页管线请求体被上游 422 拒绝（多为 thinking_effort 档位不被该模型/模式接受）",
    ),
    (
        re.compile(r"webpipe\s*403|cloudflare|cf_chl|just a moment|sentinel|challenge_required", re.I),
        "网页管线触发上游风控/人机校验或会话 403",
    ),
    (re.compile(r"--deadline|deadline budget|overall deadline", re.I), "单任务总时间预算（--deadline）已耗尽，已安全中止"),
    (re.compile(r"timed out|read operation timed out|\btimeout\b", re.I), "等待中转站/上游响应超时"),
]


def translate_upstream(text: str = "") -> str:
    """上游偶尔回英文原话 → 给一句中文摘要；认不出返回空串。"""
    t = str(text or "")
    if not t:
        return ""
    if "判定阶段" in t or "类别" in t:
        return humanize_moderation_text(t)
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
        "no_channel",
        re.compile(r"no available channel|model_not_found|has no channel", re.I),
        False,
        "中转站没有该模型的可用渠道：图像路由只提供 gpt-image-2（默认，网页管线优先）和 codex-gpt-image-2（原生端点）。",
    ),
    (
        "deadline_exceeded",
        re.compile(r"--deadline|deadline budget|overall deadline", re.I),
        False,
        "单任务总时间预算（--deadline）已耗尽，已中止且未写盘。可增大 --deadline、加 --new-conv 换新会话，或切 --model codex-gpt-image-2。",
    ),
    (
        "content_safety",
        re.compile(r"moderation_blocked|内容安全系统拒绝（原生端点）|判定阶段=", re.I),
        False,
        "这不是故障，是上游安全系统的审核判定（原生端点常见于「性化/暴露」类）。"
        "直接原样重发成功率低且可能命中缓存；优先改写为紧凑中性简报（加一句服装覆盖约束）或换一张更保守的参考图。",
    ),
    (
        "model_refusal",
        re.compile(
            r"抱歉[，,]\s*我(?:无法|不能)|我无法(?:直接)?(?:基于|按)这张|I (?:can'?t|cannot|am unable to)",
            re.I,
        ),
        False,
        "这次是对话模型自己拒绝（不是内容安全系统）：它不愿意基于这张参考图做合成，还给了替代方案。"
        "重试没有意义（同一段拒绝是确定的）。两个办法：① 换个说法（明确写「把参考图1的角色合成进新场景、保持人物不变」）；"
        "② 走原生图像端点（--model codex-gpt-image-2）—— 那条路没有对话模型，这类拒绝会消失。",
    ),
    (
        "content_refusal",
        re.compile(
            r"内容安全|安全系统|safety system|image safety|flagged by the image|"
            r"rejected by the image|safety_violations",
            re.I,
        ),
        True,
        "不是故障，是上游图像安全系统在输出层把这张图判掉了（常见触发：人物服装暴露/性化、真人肖像、暴力）。"
        "输出层判定带随机性：可用紧凑中性简报 + 覆盖约束重发一次（或用 --retry-refusal 1），"
        "但换一张服装更保守的参考图见效更快。",
    ),
    (
        "no_image_data",
        re.compile(r"没有返回图片数据|no image data|returned no images", re.I),
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
        "这是中转站自己的 CPU 打满了（1 核机器），和上游无关。不要连续重试，等一会儿再试；如果经常发生，该升级到 2 核了。",
    ),
    (
        "rate_limited",
        re.compile(r"rate limit|too many requests", re.I),
        True,
        "触发限流（HTTP 429）。稍等片刻再试；如果是频繁新建会话触发限速，去掉 --new-conv 续接当前会话即可绕开。",
    ),
    (
        "timeout",
        re.compile(r"timed out|read operation timed out|\btimeout\b", re.I),
        True,
        "等待中转站/上游响应超时（可能后端排队或当前会话卡死）。"
        "可重试一次；如果再次超时，建议加 --new-conv 换新会话或切 --model codex-gpt-image-2。",
    ),
    (
        "network_error",
        re.compile(
            r"network error talking to|connection refused|connection reset|"
            r"temporary failure in name resolution|name or service not known|ssl:",
            re.I,
        ),
        True,
        "连接中转站失败（网络中断、DNS 解析失败或沙盒未放行出站）。检查网络与 config.json 的 api_url 后重试一次。",
    ),
    (
        "webpipe_invalid_request",
        re.compile(
            r"prepare\s*没返回\s*conduit_token|conduit_token|"
            r"422\s+Invalid conversation body|Invalid conversation body|invalid\s+model\s+slug",
            re.I,
        ),
        False,
        "网页管线在会话准备或请求体校验阶段被上游拒绝（常见原因：model slug 不存在或与当前 chat/work 模式不匹配，"
        "如把 chat 的 gpt-5-6-thinking 写成 gpt-5.6-sol-thinking；或传入了不支持的 thinking_effort，"
        "如把 work 的 min 写成了 minimal）。运行 tiers --mode <chat|work> 核对实测清单后再试。",
    ),
    (
        "webpipe_blocked",
        re.compile(
            r"webpipe\s*403|cloudflare|cf_chl|just a moment|sentinel|challenge_required",
            re.I,
        ),
        False,
        "网页管线触发了上游风控/人机校验或会话 403（非提示词或参数问题）。"
        "不要连续重试；稍后再试、切 --model codex-gpt-image-2，或通知运维刷新号池 Cookie / 出口 IP。",
    ),
    (
        "bad_request",
        re.compile(r"Invalid value|Unsupported value|not supported|invalid json", re.I),
        True,
        "请求参数被上游拒绝（常见原因：当前账号/模式不支持该推理档位或模型 slug）—— 降一档 --effort 或换模型再试。",
    ),
]

_DEFAULT_UNKNOWN = (
    "upstream_error",
    True,
    "上游返回了没有归类的错误。可以原样重试一次；连续出现就把原始报错贴出来。",
)

_CODE_META: Dict[str, Tuple[bool, str]] = {
    code: (retryable, hint) for code, _pat, retryable, hint in _RULES
}
_CODE_META[_DEFAULT_UNKNOWN[0]] = (_DEFAULT_UNKNOWN[1], _DEFAULT_UNKNOWN[2])

_STATUS_FALLBACK: Dict[int, str] = {
    401: "token_invalid",
    408: "timeout",
    429: "rate_limited",
}


def is_refusal_code(code: str) -> bool:
    return code in ("content_safety", "content_refusal")


def classify_error(
    text: str = "",
    *,
    http_status: Optional[int] = None,
    code: Optional[str] = None,
) -> Dict[str, Any]:
    """归类错误 → ``{"code", "hint", "retryable"}``。

    若调用方已明确给出已知 ``code``（如 ``no_image_data`` / ``deadline_exceeded``），
    直接返回对应的 ``hint`` 与 ``retryable``，避免 ``CODE`` 与 ``HINT`` 错配。
    """
    if code and code in _CODE_META:
        retryable, hint = _CODE_META[code]
        return {"code": code, "hint": hint, "retryable": retryable}

    t = str(text or "")
    for rule_code, pattern, retryable, hint in _RULES:
        if pattern.search(t):
            return {"code": rule_code, "hint": hint, "retryable": retryable}

    if http_status in _STATUS_FALLBACK:
        fb_code = _STATUS_FALLBACK[http_status]
        retryable, hint = _CODE_META[fb_code]
        return {"code": fb_code, "hint": hint, "retryable": retryable}

    return {
        "code": _DEFAULT_UNKNOWN[0],
        "hint": _DEFAULT_UNKNOWN[2],
        "retryable": _DEFAULT_UNKNOWN[1],
    }


def describe_failure(
    text: str = "",
    *,
    http_status: Optional[int] = None,
    code: Optional[str] = None,
    stage: str = "",
    categories: str = "",
    request_id: str = "",
    secrets: Iterable[str] = (),
) -> Dict[str, Any]:
    """一次性产出一份可打印、可写进 report-json、且已脱敏的失败描述。"""
    raw = redact_secrets(str(text or ""), extra_secrets=secrets)
    cls = classify_error(raw, http_status=http_status, code=code)
    zh = translate_upstream(raw)
    if not zh and stage:
        zh = humanize_moderation(stage=stage, categories=categories, request_id=request_id)
    if not zh:
        if http_status:
            zh = f"中转站返回 HTTP {http_status}，没有出图"
        else:
            zh = "出图失败"
    zh = redact_secrets(zh, extra_secrets=secrets)
    return {
        "code": cls["code"],
        "hint": cls["hint"],
        "retryable": cls["retryable"],
        "zh": zh,
        "http_status": http_status,
        "raw": raw[:600],
    }
