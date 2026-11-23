#!/usr/bin/env python3
"""Relay CLI for explicit image generation or editing with gpt-image-2.

This is the relay counterpart of the official ``imagegen`` skill's
``scripts/image_gen.py`` (v1.5.0).

Kept aligned with the official implementation:
  * subcommands ``generate`` / ``edit`` / ``generate-batch`` (plus relay ``reference`` /
    ``models`` / ``tiers`` / ``setup``)
  * argparse flag names, including the 11 prompt-augmentation fields
  * prompt-augmentation engine and ``--augment`` / ``--no-augment``
  * ``--dry-run`` zero-side-effect preview, output-path rules, ``--force``, ``--downscale-*``
  * batch JSONL semantics (per-job overrides + nested ``fields``), bounded retries

Relay architecture facts:
  * Direct stdlib HTTP client (Python 3.9+, Pillow optional for local resample/re-encode).
  * Two image model slugs:
      - ``gpt-image-2`` (default): web session pipeline first, auto-fallback to native
        subscription endpoint (response includes ``degraded: true`` + ``degraded_reason``).
      - ``codex-gpt-image-2``: native subscription endpoint only (faster, stricter gate).
  * Custom JSON parameters travel inside ``style`` (``json.RawMessage`` passes through gateway);
    multipart ``/v1/images/edits`` uses top-level ``web_*`` / ``gif_speed`` fields.
  * Error classification, humanization, and secret redaction live in ``relay_errors.py``
    (single source of truth).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

try:  # [relay] 错误分类、人话化与脱敏唯一真相源
    from relay_errors import (
        classify_error,
        describe_failure,
        humanize_moderation_text,
        is_refusal_code,
        redact_secrets,
    )
except Exception:  # 单文件运行时兜底不炸
    def redact_secrets(text: str = "", extra_secrets: Iterable[str] = ()) -> str:  # type: ignore
        out = str(text or "")
        for s in extra_secrets:
            if len(str(s or "")) >= 6:
                out = out.replace(str(s), "[REDACTED]")
        return re.sub(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", out)

    def humanize_moderation_text(message: str = "") -> str:  # type: ignore
        return str(message or "")

    def is_refusal_code(code: str) -> bool:  # type: ignore
        return code in ("content_safety", "content_refusal")

    def classify_error(text: str = "", *, http_status=None, code=None) -> Dict[str, Any]:  # type: ignore
        return {
            "code": code or "upstream_error",
            "hint": "错误分类模块缺失（relay_errors.py 不在同目录）",
            "retryable": True,
        }

    def describe_failure(  # type: ignore
        text: str = "", *, http_status=None, code=None, secrets=(), **_kw
    ) -> Dict[str, Any]:
        return {
            "code": code or "upstream_error",
            "hint": "错误分类模块缺失（relay_errors.py 不在同目录）",
            "retryable": True,
            "zh": "出图失败",
            "http_status": http_status,
            "raw": redact_secrets(str(text or ""), extra_secrets=secrets)[:600],
        }


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_MODEL = "gpt-image-2"
MODEL_CHOICES = ("gpt-image-2", "codex-gpt-image-2")
DEFAULT_RESPONSES_MODEL = "gpt-6-astra"  # [relay] chat model that drives the image tool

DEFAULT_SIZE = "auto"
DEFAULT_QUALITY = "medium"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_CONCURRENCY = 2  # [relay] official default is 5; calibrated for this relay
DEFAULT_DOWNSCALE_SUFFIX = "-web"
DEFAULT_OUTPUT_PATH = "output/imagegen/output.png"

DEFAULT_RELAY_URL = ""  # 由 config.json 的 api_url 或环境变量提供
DEFAULT_REFERENCE_MAX_DIM = 1400  # [relay] downscale references before upload

# [relay] 分层默认超时：常规单图实测 20-60s，卡死时不必白等 600s；重任务自动放宽。
DEFAULT_STANDARD_TIMEOUT_SECONDS = 150.0
DEFAULT_STANDARD_DEADLINE_SECONDS = 240.0
DEFAULT_HEAVY_TIMEOUT_SECONDS = 300.0
DEFAULT_HEAVY_DEADLINE_SECONDS = 480.0
DEFAULT_TIMEOUT_SECONDS = DEFAULT_STANDARD_TIMEOUT_SECONDS
DEFAULT_DEADLINE_SECONDS = DEFAULT_STANDARD_DEADLINE_SECONDS
HEAVY_EFFORTS = frozenset({"extended", "xhigh", "max"})

USER_AGENT = "imagegen-relay/1.5 (stdlib)"
MAX_ERROR_TEXT = 400
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_BATCH_JOBS = 500

WEB_PIPELINE_COMMANDS = frozenset({"generate", "edit", "generate-batch"})

# [relay] style.mssk_* ↔ multipart 顶层字段 唯一权威映射表
_STYLE_TO_MULTIPART: Tuple[Tuple[str, str], ...] = (
    ("mssk_conv", "web_conv_key"),
    ("mssk_mode", "web_mode"),
    ("mssk_model", "web_model"),
    ("mssk_effort", "web_effort"),
    ("mssk_gif_speed", "gif_speed"),
)

ALLOWED_LEGACY_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}

# [relay] Native ratio pairs measured against this relay -- they never warn.
RELAY_NATIVE_SIZES = {
    "1254x1254",
    "1672x941",
    "941x1672",
    "2206x713",
    "1536x1024",
    "1448x1086",
    "1024x1024",
    "1024x1536",
    "auto",
}

ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}
ALLOWED_BACKGROUNDS = {"transparent", "opaque", "auto", None}
ALLOWED_INPUT_FIDELITIES = {"low", "high", None}
ALLOWED_MODERATION = {"auto", "low", None}

GPT_IMAGE_2_MODEL = "gpt-image-2"
GPT_IMAGE_2_MIN_PIXELS = 655_360
GPT_IMAGE_2_MAX_PIXELS = 8_294_400
GPT_IMAGE_2_MAX_EDGE = 3840
GPT_IMAGE_2_MAX_RATIO = 3.0

# [relay] prompt lines used to emulate controls this relay does not expose.
QUALITY_HINTS = {"high": "high quality, rich detail", "low": "keep it simple"}
TRANSPARENT_PROMPT_LINE = (
    "Isolate the subject on a fully transparent background: no backdrop, no floor, "
    "no shadow, no halo; keep the subject's edges clean and preserve an alpha channel."
)

# --------------------------------------------------------------------------
# [relay] Web-pipeline surface / model / effort definitions (Single Source of Truth)
# --------------------------------------------------------------------------
# chat surface：3 档（即时/中/高），每档独立 preset slug；
# work surface：4 档 UI 滑杆（轻度/中/高/极高），slug 带 -wm 后缀，只变 thinking_effort。
# CLI 查不到账号真实模型清单（/web/models 是顶层路由，网关只转发 /v1/*），以下为常见参考值。

# ⚠️ chat 与 work 的模型范围不同，两份独立清单。
#
# chat（2026-11-23 按网页版下拉框实测，只有两个）：
#   - GPT-6
#   - GPT-5.6 Sol
#   slug 是 preset 级（-thinking / -instant），由模型 + 档位共同决定。
#
# work（来自 shim 源码注释：models[] 里 slug 带 `-wm` 后缀的那批）：
#   GPT-6 Astra / GPT-6 Sol / GPT-6 Luna / GPT-6.1 Sol / GPT-5.6 Sol / Terra / Luna
#   ⚠️ 这份是从代码注释推的，没能对着网页版下拉框逐个核对。
#
# 真值来源：shim 的 /web/models（顶层路由，经网关查不到；直连时若号池有空 Cookie 会 401）。
# 选错会返回 bad_request，换一个；完全不确定就传 --agent-model auto。
WEB_CHAT_MODELS = [
    {"name": "GPT-6", "value": "gpt-6-thinking"},
    {"name": "GPT-5.6 Sol", "value": "gpt-5.6-sol-thinking"},
]
WEB_WORK_MODELS = [
    {"name": "GPT-6 Astra", "value": "gpt-6-astra-wm"},
    {"name": "GPT-6 Sol", "value": "gpt-6-sol-wm"},
    {"name": "GPT-6 Luna", "value": "gpt-6-luna-wm"},
    {"name": "GPT-6.1 Sol", "value": "gpt-6.1-sol-wm"},
    {"name": "GPT-5.6 Sol", "value": "gpt-5.6-sol-wm"},
    {"name": "GPT-5.6 Terra", "value": "gpt-5.6-terra-wm"},
    {"name": "GPT-5.6 Luna", "value": "gpt-5.6-luna-wm"},
]


def _models_for(mode: str) -> list:
    """两种 surface 的模型范围不同，各取各的清单。"""
    return WEB_CHAT_MODELS if mode == "chat" else WEB_WORK_MODELS


WEB_CHAT_TIERS = [
    {"name": "即时", "effort": "none"},
    {"name": "中", "effort": "standard"},
    {"name": "高", "effort": "extended"},
]
WEB_WORK_TIERS = [
    {"name": "轻度", "effort": "minimal"},
    {"name": "中", "effort": "low"},
    {"name": "高", "effort": "standard"},
    {"name": "极高", "effort": "extended"},
]
WEB_MODE_CHOICES = [
    {"name": "聊天", "value": "chat"},
    {"name": "工作", "value": "work"},
]

# 合法 --effort 值：同时兼容 work 滑杆（minimal/low/standard/extended/max）
# 与 chat 账号预设可能声明的值（medium/high/xhigh），以及 none（即时，不传 thinking_effort）。
EFFORT_ORDER = (
    "none",
    "minimal",
    "low",
    "standard",
    "medium",
    "high",
    "extended",
    "xhigh",
    "max",
)

# --------------------------------------------------------------------------
# [relay] Coverage preflight & Refusal retry
# --------------------------------------------------------------------------

COVERAGE_LINE_ZH = (
    "Clothing: 角色服装与参考图一致，为覆盖完整、不透明的运动套装；衣着保守得体，画面中不出现暴露或暗示性表达。"
)
COVERAGE_LINE_EN = (
    "Clothing: keep the outfit consistent with the reference image and render it as fully "
    "covering, opaque sportswear and jacket; modest, no exposed or suggestive elements."
)

_PERSON_CJK_RE = re.compile(
    r"(人物|角色|女性|男性|女人|男人|女孩|男孩|少女|少年|女士|男士|女生|男生|人像|肖像|写真|立绘|"
    r"模特|主角|女主角|男主角|美女|演员|歌手|运动员|选手|明星|偶像|人脸|人体)"
)
_PERSON_LATIN_RE = re.compile(
    r"\b(person|people|woman|women|man|men|girl|boy|female|male|portrait|character|"
    r"selfie|face|lady|guy|heroine|idol|athlete|singer|actress|actor|human)\b",
    re.IGNORECASE,
)
_PERSON_MODEL_RE = re.compile(
    r"(?<!\b3d )(?<!\b3-d )(?<!\bpoly )(?<!\blow-poly )\bmodel\b", re.IGNORECASE
)
_NEGATED_PERSON_RE = re.compile(
    r"(?:no|without|avoid|exclude|excluding|other|其他|避免|排除|不含|没有|无人|不要|禁止|杜绝)"
    r"\s*"
    r"(?:extra\s+|any\s+|other\s+|additional\s+)?"
    r"(?:people|person|persons|humans?|characters?|women|men|girls?|boys?|figures?|"
    r"人物|角色|人像|人体|人)",
    re.IGNORECASE,
)
_COVERAGE_RE = re.compile(
    r"(覆盖完整|不透明|保守得体|衣着保守|不出现暴露|fully cover|opaque|modest|"
    r"no revealing|non-?revealing)",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

_LABELED_SPEC_RE = re.compile(
    r"(?im)^\s*(use case|asset type|primary request|input images?|scene/backdrop|subject|"
    r"style/medium|composition/framing|lighting/mood|color palette|materials/textures|"
    r"text \(verbatim\)|constraints|clothing|avoid|quality|background)\s*:"
)

_OUTFIT_LOCK_RE = re.compile(
    r"(?:服装|衣着|穿搭|outfit|clothing|attire)[^。；\n]{0,12}?"
    r"(?:保持不变|不变|锁定|不改|unchanged|the same|identical)",
    re.IGNORECASE,
)
OUTFIT_LOCK_LINE_ZH = (
    "Clothing: 保持参考图中的既有服装与既覆盖度不变；画面自然得体，不强调身体曲线，"
    "不出现暴露或暗示性表达。"
)
OUTFIT_LOCK_LINE_EN = (
    "Clothing: keep the reference outfit and its existing coverage exactly as it is; natural, "
    "tasteful framing, no emphasis on body curves, no revealing or suggestive elements."
)

_REFUSAL_VARIATIONS = (
    "",
    "\nRender: clean composition, natural lighting.",
    "\nRender: crisp edges, balanced lighting, tidy composition.",
    "\nRender: clean framing, natural light, sharp detail.",
)

_NEUTRALIZATIONS: List[Tuple[re.Pattern, str, str]] = [
    (
        re.compile(
            r"(?:锁定|保持|维持|不改变|不要改变|保留)[^。；\n]{0,12}?"
            r"(?:服装|衣着|穿搭|穿搭造型|outfit|clothing|attire)[^。；\n]{0,14}?"
            r"(?:保持不变|基本不变|不变动|不变|unchanged)"
        ),
        "面貌、发型与姿势保持不变；服装与参考图一致，为覆盖完整、不透明的运动套装",
        "服装保持不变 -> 服装为覆盖完整的运动装",
    ),
    (
        re.compile(
            r"(?:keep|preserve|maintain)[^.\n]{0,20}?(?:outfit|clothing|attire)[^.\n]{0,20}?"
            r"(?:unchanged|the same|identical)",
            re.IGNORECASE,
        ),
        "render the outfit as fully covering, opaque sportswear",
        "keep-outfit-unchanged -> reframed",
    ),
    (
        re.compile(r"(黄色|白色|黑色|红色|蓝色|粉色|紫色)?\s*(运动内衣|内衣裤|内衣|胸衣|文胸)"),
        "上衣",
        "贴身衣物名 -> 上衣",
    ),
    (re.compile(r"\b(brassiere|bra|lingerie|underwear)\b", re.IGNORECASE), "top", "underwear -> top"),
    (re.compile(r"(身体比例|身材比例|身材曲线|身体曲线|三围)"), "", "身体度量词 -> 删除"),
    (re.compile(r"\b(body proportions|body shape|body curves)\b", re.IGNORECASE), "", "body metrics -> removed"),
    (re.compile(r"(不要改变|保持|维持)?\s*服装覆盖范围"), "", "覆盖范围提法 -> 删除"),
    (re.compile(r"\b(clothing coverage|coverage of the outfit)\b", re.IGNORECASE), "", "coverage phrasing -> removed"),
    (re.compile(r"(肤色|裸露的?皮肤)"), "", "肤色/裸露提法 -> 删除"),
    (re.compile(r"\b(skin tone|bare skin)\b", re.IGNORECASE), "", "skin phrasing -> removed"),
    (re.compile(r"(暴露|露骨|性感)"), "", "暴露/性感提法 -> 删除"),
    (re.compile(r"\b(revealing|provocative|sexy)\b", re.IGNORECASE), "", "revealing/sexy -> removed"),
    (re.compile(r"\b(cleavage)\b", re.IGNORECASE), "", "cleavage -> removed"),
]

_SEPARATOR_TIDY = [
    (re.compile(r"、{2,}"), "、"),
    (re.compile(r"、\s*(?=[、。；;，,])"), ""),
    (re.compile(r"[,，]\s*[,，]"), "，"),
    (re.compile(r"[、，,]\s*$", re.MULTILINE), ""),
    (re.compile(r"\n{3,}"), "\n\n"),
]

# --------------------------------------------------------------------------
# [relay] Aspect-ratio presets (--preset)
# --------------------------------------------------------------------------

_PRESETS: Dict[str, Dict[str, Tuple[int, int]]] = {
    "1:1": {"2k": (2048, 2048), "4k": (2880, 2880)},
    "3:2": {"2k": (2048, 1360), "4k": (3520, 2352)},
    "2:3": {"2k": (1360, 2048), "4k": (2352, 3520)},
    "4:3": {"2k": (2048, 1536), "4k": (3264, 2448)},
    "3:4": {"2k": (1536, 2048), "4k": (2448, 3264)},
    "16:9": {"2k": (2048, 1152), "4k": (3840, 2160)},
    "9:16": {"2k": (1152, 2048), "4k": (2160, 3840)},
    "2:1": {"2k": (2048, 1024), "4k": (3840, 1920)},
    "1:2": {"2k": (1024, 2048), "4k": (1920, 3840)},
    "7:4": {"2k": (2208, 1264), "4k": (3808, 2176)},
    "4:7": {"2k": (1264, 2208), "4k": (2176, 3808)},
}

_CANONICAL_RENDER: Dict[str, Tuple[int, int]] = {
    "1:1": (1254, 1254),
    "16:9": (1672, 941),
    "9:16": (941, 1672),
}


# --------------------------------------------------------------------------
# Console & Error Plumbing
# --------------------------------------------------------------------------


def _force_utf8_stdio() -> None:
    """[relay] The relay returns Chinese error text; keep Windows consoles safe."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def _info(message: str) -> None:
    print(message, file=sys.stderr)


def _skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


class RelayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        retryable: bool = False,
        raw: Optional[str] = None,
        code: Optional[str] = None,
        retry_after: Optional[float] = None,
        is_timeout: bool = False,
        elapsed_s: float = 0.0,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.raw = raw
        self.code = code
        self.retry_after = retry_after
        self.is_timeout = is_timeout
        self.elapsed_s = elapsed_s


def _is_refusal_error(exc: BaseException) -> bool:
    """判定是否属于“可换措辞重试的输出层内容安全拒绝”。

    使用 relay_errors 唯一分类源（同时检查 raw 与人话后的 str(exc)），
    避免人话化后匹配不到；同时跳过确定性的输入层拒绝（判定阶段=input）。
    """
    raw = str(getattr(exc, "raw", "") or "")
    msg = str(exc)
    combined = f"{raw} {msg}".strip()
    if "判定阶段=input" in combined or "提示词/参考图阶段" in combined:
        return False
    code = getattr(exc, "code", None)
    if code and is_refusal_code(code):
        return True
    cls = classify_error(combined, http_status=getattr(exc, "status", None))
    return is_refusal_code(cls["code"])


def _failure_dict(
    exc: RelayError,
    *,
    secrets: Iterable[str] = (),
) -> Dict[str, Any]:
    """生成统一的脱敏失败描述（以 relay_errors 为唯一分类与重试信号源）。"""
    combined = ((exc.raw or "") + " " + str(exc)).strip()
    return describe_failure(
        combined,
        http_status=exc.status,
        code=exc.code,
        secrets=secrets,
    )


def _print_failure_lines(desc: Dict[str, Any], raw_msg: str, *, secrets: Iterable[str] = ()) -> None:
    safe_msg = redact_secrets(raw_msg, extra_secrets=secrets)
    print(f"Error: {desc['zh']}", file=sys.stderr)
    print(f"CODE={desc['code']}   RETRYABLE={'yes' if desc['retryable'] else 'no'}", file=sys.stderr)
    print(f"HINT={desc['hint']}", file=sys.stderr)
    if safe_msg and safe_msg not in desc["zh"]:
        print(f"RAW={safe_msg[:600]}", file=sys.stderr)


def _emit_failure(
    exc: RelayError,
    args: argparse.Namespace,
    *,
    secrets: Iterable[str] = (),
) -> None:
    """失败统一出口：打印脱敏机器可读三行，并按需落 --report-json。"""
    desc = _failure_dict(exc, secrets=secrets)
    _print_failure_lines(desc, str(exc), secrets=secrets)

    report_path = getattr(args, "report_json", None)
    if report_path:
        try:
            _atomic_write_text(
                Path(report_path),
                json.dumps(
                    {
                        "ok": False,
                        "command": getattr(args, "command", None),
                        "model": getattr(args, "model", None),
                        "error": desc,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass

    raise SystemExit(1)


# --------------------------------------------------------------------------
# Config, Tiers & Setup  [relay]
# --------------------------------------------------------------------------


def _tiers_for(mode: str) -> List[Dict[str, str]]:
    return WEB_CHAT_TIERS if mode == "chat" else WEB_WORK_TIERS


def _load_config_json() -> Dict[str, Any]:
    cfg_path = _skill_root() / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        _die(f"config.json is not valid JSON: {cfg_path} ({exc})")
    return {}


def _config_defaults() -> Dict[str, str]:
    """从 config.json 读「上次选好的模型/档位/surface」，没有就返回空串。"""
    cfg = _load_config_json()
    return {
        "agent_model": str(cfg.get("web_agent_model") or ""),
        "effort": str(cfg.get("web_effort") or ""),
        "web_mode": str(cfg.get("web_mode") or ""),
    }


def _validate_web_controls(
    web_mode: Optional[str],
    effort: Optional[str],
    agent_model: Optional[str] = None,
    *,
    warn_slug_mismatch: bool = False,
) -> None:
    eff = str(effort or "").strip().lower()
    if web_mode == "work" and eff == "none":
        _die("work 模式没有即时档（--effort none）；请选 minimal/low/standard/extended，或改用 --web-mode chat。")
    if warn_slug_mismatch and agent_model and agent_model not in ("", "auto"):
        if web_mode == "work" and not agent_model.endswith("-wm"):
            _warn(f"work 模式通常使用 -wm 后缀模型（如 gpt-6-astra-wm），当前指定为 {agent_model}")
        elif web_mode == "chat" and agent_model.endswith("-wm"):
            _warn(f"chat 模式通常不使用 -wm 后缀模型，当前指定为 {agent_model}")


def _cmd_tiers(args: argparse.Namespace) -> int:
    """两步提问：
      1) tiers                → 模式：1)聊天 2)工作
      2) tiers --mode chat    → 模型 + 档位（一次列完）
    """
    mode = getattr(args, "mode", None)

    if not mode:
        if getattr(args, "efforts", False):
            _die("--efforts 需要配合 --mode 一起用（先选模式）")
        if getattr(args, "json", False):
            print(json.dumps({"modes": WEB_MODE_CHOICES}, ensure_ascii=False, indent=2))
            return 0
        print("模式：")
        print("  " + "   ".join("%d)%s" % (i, m["name"]) for i, m in enumerate(WEB_MODE_CHOICES, 1)))
        return 0

    model_list = _models_for(mode)
    tiers = _tiers_for(mode)
    models = model_list
    label = "聊天" if mode == "chat" else "工作"

    if getattr(args, "json", False):
        data: Dict[str, Any] = {
            "mode": mode,
            "models": models,
            "efforts": [{"name": t["name"], "value": t["effort"]} for t in tiers],
        }
        if getattr(args, "efforts", False):
            data.pop("models")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if not getattr(args, "efforts", False):
        print("模型（%s）：" % label)
        for i, m in enumerate(models, 1):
            print("  %d) %s" % (i, m["name"]))
    print("档位（%s）：" % label)
    print("  " + "   ".join("%d)%s" % (i, t["name"]) for i, t in enumerate(tiers, 1)))
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    """查看或固化「驱动模型 / 推理档 / surface」到 config.json。

    --show [--json]：安全只读检查配置状态（绝不打印 api_key，避免 agent 读 config.json 泄露令牌）。
    """
    path = _skill_root() / "config.json"
    cfg = _load_config_json() if path.exists() else {}

    if getattr(args, "show", False):
        creds = _resolve_credentials(args)
        has_key = bool(creds.api_key and not creds.api_key.startswith("PUT-YOUR"))
        web_mode = cfg.get("web_mode") or None
        web_effort = cfg.get("web_effort") or None
        web_model = cfg.get("web_agent_model") or None
        configured_web = any(v is not None for v in (web_mode, web_effort, web_model))
        status = {
            "config_path": str(path),
            "config_exists": path.exists(),
            "has_api_key": has_key,
            "api_url": creds.base_url or None,
            "credentials_source": creds.source,
            "configured_web": configured_web,
            "web_mode": web_mode,
            "web_effort": web_effort,
            "web_agent_model": web_model,
            "edit_contract": cfg.get("edit_contract") or "auto",
        }
        if getattr(args, "json", False):
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0
        print(f"配置文件：{path}（{'存在' if path.exists() else '未创建'}）")
        print(f"中转地址：{status['api_url'] or '未配置'}  ·  令牌：{'已配置' if has_key else '未配置'}")
        print(
            "网页管线默认："
            f"surface={web_mode or '未设置(默认 chat)'}  ·  "
            f"effort={web_effort or '未设置(即时档)'}  ·  "
            f"agent_model={web_model or 'auto'}"
        )
        return 0

    changed: List[str] = []
    effective_mode = args.web_mode if args.web_mode is not None else cfg.get("web_mode")
    effective_effort = args.effort if args.effort is not None else cfg.get("web_effort")
    effective_model = args.agent_model if args.agent_model is not None else cfg.get("web_agent_model")
    _validate_web_controls(
        effective_mode,
        effective_effort,
        effective_model,
        warn_slug_mismatch=True,
    )

    if args.agent_model is not None:
        if args.agent_model in ("", "auto"):
            cfg.pop("web_agent_model", None)
            changed.append("web_agent_model=<清除，回到 auto>")
        else:
            cfg["web_agent_model"] = args.agent_model
            changed.append(f"web_agent_model={args.agent_model}")
    if args.effort is not None:
        if args.effort in ("", "none"):
            cfg.pop("web_effort", None)
            changed.append("web_effort=<清除，即时档>")
        else:
            cfg["web_effort"] = args.effort
            changed.append(f"web_effort={args.effort}")
    if args.web_mode is not None:
        cfg["web_mode"] = args.web_mode
        changed.append(f"web_mode={args.web_mode}")

    if not changed:
        _die("什么都没改：至少给一个 --agent-model / --effort / --web-mode（或用 --show 查看）")

    _atomic_write_text(path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    try:
        path.chmod(0o600)
    except Exception:
        pass
    print(f"已写入 {path}")
    for c in changed:
        print("  ·", c)
    print("之后各命令不传这三个参数时自动沿用；要临时覆盖就在命令行加。")
    return 0


# --------------------------------------------------------------------------
# Conversation Key & Unified WireStyle  [relay]
# --------------------------------------------------------------------------


def _conv_key_path(*, create_dir: bool = True) -> Path:
    """会话钥匙按「用户 + 当前项目目录」隔离（对齐 image-web 的 sha256(user)+conv.id）。"""
    try:
        user = getpass.getuser()
    except Exception:
        user = "anon"
    scope = f"{user}:{Path.cwd()}"
    tag = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
    d = Path.cwd() / ".imagegen"
    if create_dir:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            return _skill_root() / f".conv-{tag}"
    return d / f"conv-{tag}"


def _new_conv_key() -> str:
    return "skill-" + uuid.uuid4().hex[:12]


def _resolve_conv_key_plan(
    args: argparse.Namespace,
    *,
    persist: bool,
) -> Tuple[str, Optional[Path]]:
    """计算本次请求的会话钥匙，返回 ``(key, path_to_persist_or_None)``。

    ``persist=False``（如 ``--dry-run``）时保证零磁盘副作用；
    非 dry-run 时也可把返回的路径交给 ``WireStyle.commit_conv()`` 在请求成功后落盘，
    避免参数校验/401 失败就覆盖原有活跃会话钥匙。
    """
    explicit = getattr(args, "conv_key", None)
    if explicit:
        return str(explicit), None

    want_new = bool(getattr(args, "new_conv", False))
    path = _conv_key_path(create_dir=False)
    if not want_new and path.exists():
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing, None
        except Exception:
            pass

    key = _new_conv_key()
    if persist:
        _persist_conv_key(key)
        return key, None
    return key, path


def _persist_conv_key(key: str) -> None:
    target = _conv_key_path(create_dir=True)
    try:
        _atomic_write_text(target, key)
    except Exception:
        pass


def _resolve_conv_key(args: argparse.Namespace) -> str:
    key, _pending = _resolve_conv_key_plan(args, persist=not getattr(args, "dry_run", False))
    return key


@dataclass
class WireStyle:
    """统一的自定义透传参数容器（同时导出 JSON ``style`` 与 multipart 顶层字段）。"""

    mssk_conv: Optional[str] = None
    mssk_mode: Optional[str] = None
    mssk_model: Optional[str] = None
    mssk_effort: Optional[str] = None
    mssk_gif_speed: Optional[str] = None
    _pending_conv_persist: bool = False

    def as_json_style(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.mssk_conv:
            out["mssk_conv"] = self.mssk_conv
        if self.mssk_gif_speed:
            out["mssk_gif_speed"] = self.mssk_gif_speed
        if self.mssk_mode:
            out["mssk_mode"] = self.mssk_mode
        if self.mssk_model:
            out["mssk_model"] = self.mssk_model
        if self.mssk_effort:
            out["mssk_effort"] = self.mssk_effort
        return out

    def as_multipart_fields(self) -> Dict[str, str]:
        style = self.as_json_style()
        return {
            mp_key: str(style[json_key])
            for json_key, mp_key in _STYLE_TO_MULTIPART
            if json_key in style and style[json_key]
        }

    def commit_conv(self) -> None:
        if self._pending_conv_persist and self.mssk_conv:
            _persist_conv_key(self.mssk_conv)
            self._pending_conv_persist = False


def _build_wire_style(
    args: argparse.Namespace,
    *,
    include_conv: bool = True,
    defer_conv_persist: bool = True,
) -> WireStyle:
    conv_key: Optional[str] = None
    pending_persist = False
    if include_conv:
        is_dry = bool(getattr(args, "dry_run", False))
        conv_key, pending_path = _resolve_conv_key_plan(
            args,
            persist=(not is_dry and not defer_conv_persist),
        )
        pending_persist = (not is_dry) and (pending_path is not None)

    gif_speed = getattr(args, "gif_speed", None)
    web_mode = getattr(args, "web_mode", None)

    raw_model = getattr(args, "agent_model", None)
    agent_model: Optional[str] = None
    if raw_model and str(raw_model).strip().lower() not in ("", "auto"):
        agent_model = str(raw_model).strip()

    raw_effort = getattr(args, "effort", None)
    effort: Optional[str] = None
    # none 表示「即时」= 不传 thinking_effort（上游收到字符串 "none" 会报 400）。
    if raw_effort and str(raw_effort).strip().lower() not in ("", "none", "undefined", "null", "auto"):
        effort = str(raw_effort).strip()

    return WireStyle(
        mssk_conv=conv_key,
        mssk_mode=str(web_mode) if web_mode else None,
        mssk_model=agent_model,
        mssk_effort=effort,
        mssk_gif_speed=str(gif_speed) if gif_speed else None,
        _pending_conv_persist=pending_persist,
    )


def _style_payload(args: argparse.Namespace) -> Dict[str, Any]:
    """兼容入口：返回 JSON ``style`` 透传 dict（dry-run 下零磁盘副作用）。"""
    return _build_wire_style(args, include_conv=True, defer_conv_persist=True).as_json_style()


# --------------------------------------------------------------------------
# Credentials & Endpoint Resolution  [relay]
# --------------------------------------------------------------------------


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not re.search(r"/v\d+$", url):
        url = url + "/v1"
    return url


@dataclass
class RelayCredentials:
    base_url: str
    api_key: str
    source: str


def _resolve_credentials(args: argparse.Namespace) -> RelayCredentials:
    cfg = _load_config_json()
    base = getattr(args, "base_url", None) or cfg.get("api_url") or ""
    key = cfg.get("api_key") or ""
    source = "config.json" if (cfg.get("api_url") or cfg.get("api_key")) else ""

    if not base:
        for env_name in ("IMAGE_RELAY_URL", "CODEX_API_URL", "OPENAI_BASE_URL"):
            if os.getenv(env_name):
                base = os.getenv(env_name, "")
                source = source or f"env:{env_name}"
                break
    if not key:
        for env_name in ("IMAGE_RELAY_KEY", "CODEX_API_KEY", "OPENAI_API_KEY"):
            if os.getenv(env_name):
                key = os.getenv(env_name, "")
                source = source or f"env:{env_name}"
                break

    return RelayCredentials(
        base_url=_normalize_base_url(base) or DEFAULT_RELAY_URL,
        api_key=(key or "").strip(),
        source=source or "default",
    )


def _ensure_credentials(creds: RelayCredentials, dry_run: bool, *, announce: bool = True) -> None:
    has_key = bool(creds.api_key and not creds.api_key.startswith("PUT-YOUR"))
    has_url = bool(creds.base_url and re.match(r"^https?://", creds.base_url, re.I))

    if has_key and has_url:
        if announce:
            _info(f"Relay endpoint: {creds.base_url} (credentials: {creds.source}).")
        return

    if dry_run:
        if not has_key:
            _warn("Relay token is not configured; dry-run only.")
        if not has_url:
            _warn("Relay api_url is not configured; dry-run only.")
        return

    if not has_key:
        _die(
            "Relay token is not configured. Open "
            f"{_skill_root() / 'config.json'} and put the relay token into the \"api_key\" "
            "field, save, then retry."
        )
    _die(
        "Relay api_url is not configured (or missing http:// / https://). Open "
        f"{_skill_root() / 'config.json'} and set \"api_url\" (e.g. https://relay.example.com:9443/v1), "
        "or pass --base-url."
    )


# --------------------------------------------------------------------------
# Edit Contract Resolution  [relay]
# --------------------------------------------------------------------------

_EDIT_CONTRACT_BY_URL: Dict[str, str] = {}


def _load_edit_contract(base_url: str) -> Optional[str]:
    cfg = _load_config_json()
    pinned = str(cfg.get("edit_contract") or "").strip().lower()
    if pinned in ("codex-json", "openai-multipart"):
        return pinned
    return _EDIT_CONTRACT_BY_URL.get(base_url)


def _save_edit_contract(base_url: str, contract: str) -> None:
    if base_url and contract in ("codex-json", "openai-multipart"):
        _EDIT_CONTRACT_BY_URL[base_url] = contract


# --------------------------------------------------------------------------
# HTTP Transport, Heartbeat & Retries  [relay]
# --------------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    body: bytes
    queue_wait_s: float  # request sent -> response headers (relay queue + upstream render)
    download_s: float  # response headers -> body fully read


def _decode_body(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return repr(raw[:MAX_ERROR_TEXT])


def _extract_error_text(text: str) -> str:
    try:
        payload = json.loads(text)
    except Exception:
        return text.strip()[:MAX_ERROR_TEXT]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "").strip()
            code = str(err.get("code") or "").strip()
            if message and code:
                return f"[{code}] {message}"
            if message:
                return message
        for key in ("message", "msg"):
            if payload.get(key):
                return str(payload[key])
    return json.dumps(payload)[:MAX_ERROR_TEXT]


def _parse_retry_after_header(headers: Any) -> Optional[float]:
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After")
    except Exception:
        return None
    if not raw:
        return None
    try:
        val = float(str(raw).strip())
        return max(0.0, min(val, 120.0))
    except Exception:
        return None


_NON_RETRYABLE_TRANSPORT_CODES = frozenset({
    "token_invalid",
    "token_revoked",
    "no_model_access",
    "no_channel",
    "deadline_exceeded",
    "content_safety",
    "content_refusal",
    "model_refusal",
    "upstream_overloaded",
    "relay_cpu",
    "bad_request",
})


def _relay_error_from_http(
    status: int,
    raw: bytes,
    *,
    headers: Any = None,
) -> RelayError:
    """将 HTTP 响应转为 RelayError，分类与重试信号以 relay_errors 为唯一真相源。"""
    text = _decode_body(raw)
    message = _extract_error_text(text)
    retry_after = _parse_retry_after_header(headers)

    cls = classify_error(f"{message} {text}", http_status=status)
    code = cls["code"]

    # 识别出的不可重试码直接覆盖 HTTP 5xx/429，绝不盲目猛撞（如 503 system cpu overloaded）
    if code in _NON_RETRYABLE_TRANSPORT_CODES:
        transport_retryable = False
    else:
        transport_retryable = status in (408, 429) or status >= 500

    if code == "token_invalid":
        pretty = (
            f"relay rejected the token (HTTP {status}): {message}. "
            "Update \"api_key\" in config.json with a fresh relay token."
        )
    elif code == "no_channel":
        pretty = (
            f"relay has no channel for this model (HTTP {status}): {message}. "
            f"Valid image models: {', '.join(MODEL_CHOICES)}."
        )
    elif is_refusal_code(code):
        pretty = humanize_moderation_text(message)
    elif "invalid json" in message.lower():
        pretty = f"relay rejected the request body (HTTP {status}): {message}"
    else:
        pretty = f"relay error (HTTP {status}): {message}"

    return RelayError(
        pretty,
        status=status,
        retryable=transport_retryable,
        raw=text,
        code=code,
        retry_after=retry_after,
    )


class _Heartbeat:
    """[relay] Prints a progress line while a long render is in flight; escalates to [slow] if stalled."""

    def __init__(self, label: str, interval: float = 20.0, slow_after: float = 90.0):
        self.label = label
        self.interval = interval
        self.slow_after = slow_after
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "_Heartbeat":
        started = time.monotonic()

        def loop() -> None:
            while not self._stop.wait(self.interval):
                elapsed = time.monotonic() - started
                if elapsed >= self.slow_after:
                    _info(
                        f"[slow] {self.label} {elapsed:.0f}s elapsed -- "
                        "taking longer than usual (possible queue/session stall); waiting up to timeout."
                    )
                else:
                    _info(
                        f"[waiting] {self.label} {elapsed:.0f}s elapsed -- "
                        "the relay is still rendering; do not start another run."
                    )

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)
        return False


def _is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(exc).lower()


def _http_request(
    url: str,
    *,
    api_key: str,
    data: Optional[bytes] = None,
    content_type: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    method: str = "POST",
    label: str = "request",
    slow_after: float = 90.0,
) -> HttpResponse:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if content_type:
        headers["Content-Type"] = content_type
    request = urlrequest.Request(url, data=data, headers=headers, method=method)

    started = time.time()
    mono_start = time.monotonic()
    try:
        with _Heartbeat(label, slow_after=slow_after):
            with urlrequest.urlopen(request, timeout=timeout) as response:
                headers_at = time.time()
                body = response.read()
                finished = time.time()
                return HttpResponse(
                    status=getattr(response, "status", 200),
                    body=body,
                    queue_wait_s=headers_at - started,
                    download_s=finished - headers_at,
                )
    except urlerror.HTTPError as exc:
        raw = exc.read()
        raise _relay_error_from_http(exc.code, raw, headers=exc.headers)
    except (urlerror.URLError, TimeoutError, socket.timeout, OSError) as exc:
        elapsed = time.monotonic() - mono_start
        reason = getattr(exc, "reason", exc)
        if _is_timeout_exception(exc):
            raise RelayError(
                f"request timed out after {elapsed:.0f}s talking to {url}: {reason}",
                retryable=True,
                code="timeout",
                is_timeout=True,
                elapsed_s=elapsed,
            )
        raise RelayError(
            f"network error talking to {url}: {reason}",
            retryable=True,
            code="network_error",
            elapsed_s=elapsed,
        )


def _encode_multipart(
    fields: Dict[str, str],
    files: List[Tuple[str, str, bytes, str]],
) -> Tuple[bytes, str]:
    boundary = "----imagegenrelay" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for name, filename, payload, content_type in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(payload)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "image/png"


def _effective_timeout(timeout: float, deadline: Optional[float]) -> float:
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RelayError(
            "aborted: the overall --deadline budget was used up. Nothing was written.",
            retryable=False,
            code="deadline_exceeded",
        )
    return max(1.0, min(timeout, remaining))


def _resolve_timeouts(
    args: argparse.Namespace,
    *,
    command: str,
    n: int,
    web_mode: Optional[str],
    effort: Optional[str],
) -> Tuple[float, float, float]:
    """分层默认超时：常规单图 (150s/240s)，重任务 (300s/480s)；显式参数始终优先。"""
    heavy = (
        n > 1
        or web_mode == "work"
        or str(effort or "").lower() in HEAVY_EFFORTS
        or command == "reference"
    )
    user_timeout = getattr(args, "timeout", None)
    user_deadline = getattr(args, "deadline", None)

    timeout_s = float(user_timeout) if user_timeout is not None else (
        DEFAULT_HEAVY_TIMEOUT_SECONDS if heavy else DEFAULT_STANDARD_TIMEOUT_SECONDS
    )
    if user_deadline is not None:
        deadline_s = float(user_deadline)
    else:
        default_dl = DEFAULT_HEAVY_DEADLINE_SECONDS if heavy else DEFAULT_STANDARD_DEADLINE_SECONDS
        deadline_s = max(default_dl, timeout_s)
    slow_after = 180.0 if heavy else 90.0
    return timeout_s, deadline_s, slow_after


def _extract_retry_after_seconds(exc: RelayError) -> Optional[float]:
    if exc.retry_after is not None:
        return exc.retry_after
    message = str(exc)
    match = re.search(r"retry[- ]after[:= ]+([0-9]+(?:\.[0-9]+)?)", message, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except Exception:
            return None
    return None


def _with_retries(
    attempt_fn: Callable[[], Any],
    *,
    attempts: int,
    label: str,
    deadline: Optional[float] = None,
) -> Any:
    last_exc: Optional[RelayError] = None
    for attempt in range(1, attempts + 1):
        if deadline is not None and deadline - time.monotonic() <= 0:
            raise RelayError(
                f"{label} aborted after {attempt - 1} attempt(s): the overall --deadline "
                "budget was used up. Nothing was written.",
                retryable=False,
                code="deadline_exceeded",
            )
        try:
            return attempt_fn()
        except RelayError as exc:
            last_exc = exc
            if not exc.retryable or attempt >= attempts:
                raise
            # 长耗时渲染超时（>=60s）大概率是后端排队或会话卡死，最多自动重试 1 次
            if exc.is_timeout and exc.elapsed_s >= 60.0:
                if attempt >= 2 or (deadline is not None and deadline - time.monotonic() < 60.0):
                    raise
            delay = _extract_retry_after_seconds(exc) or min(60.0, 2.0**attempt)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 1.0:
                    raise RelayError(
                        f"{label} aborted after {attempt} attempt(s): the overall --deadline "
                        "budget was used up. Nothing was written.",
                        retryable=False,
                        code="deadline_exceeded",
                    )
                delay = min(delay, max(0.0, remaining - 1.0))
            _info(
                f"{label} attempt {attempt}/{attempts} failed "
                f"({exc.code or exc.__class__.__name__}); retrying in {delay:.1f}s"
            )
            if delay > 0:
                time.sleep(delay)
    raise last_exc or RelayError("unknown relay failure")


def _post_with_retries(
    creds: RelayCredentials,
    endpoint: str,
    *,
    body: bytes,
    content_type: str,
    attempts: int,
    timeout: float,
    label: str,
    deadline: Optional[float] = None,
    slow_after: float = 90.0,
) -> Tuple[HttpResponse, Dict[str, Any]]:
    url = f"{creds.base_url}/{endpoint.lstrip('/')}"

    def once() -> Tuple[HttpResponse, Dict[str, Any]]:
        response = _http_request(
            url,
            api_key=creds.api_key,
            data=body,
            content_type=content_type,
            timeout=_effective_timeout(timeout, deadline),
            method="POST",
            label=label,
            slow_after=slow_after,
        )
        try:
            decoded = json.loads(_decode_body(response.body))
        except Exception as exc:
            raise RelayError(f"relay returned invalid JSON: {exc}")
        if isinstance(decoded, dict) and "error" in decoded and "data" not in decoded:
            raise _relay_error_from_http(response.status, response.body)
        return response, decoded

    return _with_retries(once, attempts=attempts, label=label, deadline=deadline)


def _post_json_with_retries(
    creds: RelayCredentials,
    endpoint: str,
    payload: Dict[str, Any],
    *,
    attempts: int,
    timeout: float,
    label: str,
    deadline: Optional[float] = None,
    slow_after: float = 90.0,
) -> Tuple[HttpResponse, Dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _post_with_retries(
        creds,
        endpoint,
        body=body,
        content_type="application/json",
        attempts=attempts,
        timeout=timeout,
        label=label,
        deadline=deadline,
        slow_after=slow_after,
    )


def _post_multipart_with_retries(
    creds: RelayCredentials,
    endpoint: str,
    fields: Dict[str, str],
    files: List[Tuple[str, str, bytes, str]],
    *,
    attempts: int,
    timeout: float,
    label: str,
    deadline: Optional[float] = None,
    slow_after: float = 90.0,
) -> Tuple[HttpResponse, Dict[str, Any]]:
    body, content_type = _encode_multipart(fields, files)
    return _post_with_retries(
        creds,
        endpoint,
        body=body,
        content_type=content_type,
        attempts=attempts,
        timeout=timeout,
        label=label,
        deadline=deadline,
        slow_after=slow_after,
    )


def _post_edit_with_retries(
    creds: RelayCredentials,
    *,
    json_payload: Dict[str, Any],
    fields: Dict[str, str],
    files: List[Tuple[str, str, bytes, str]],
    attempts: int,
    timeout: float,
    label: str,
    deadline: Optional[float] = None,
    slow_after: float = 90.0,
    cached_contract: Optional[str] = None,
) -> Tuple[Tuple[HttpResponse, Dict[str, Any]], str]:
    """[relay] Speak whichever edits contract the relay implements (configurable via config.json edit_contract)."""
    contract = cached_contract or _load_edit_contract(creds.base_url)

    if contract != "openai-multipart":
        try:
            result = _post_json_with_retries(
                creds,
                "images/edits",
                json_payload,
                attempts=attempts,
                timeout=timeout,
                label=label,
                deadline=deadline,
                slow_after=slow_after,
            )
            _save_edit_contract(creds.base_url, "codex-json")
            return result, "codex-json"
        except RelayError as exc:
            message = str(exc).lower()
            if "image is required" not in message and "images is required" not in message:
                raise
            _save_edit_contract(creds.base_url, "openai-multipart")
            _info(
                "Relay uses the OpenAI Images contract (multipart image); falling back now "
                '(set "edit_contract": "openai-multipart" in config.json to skip this probe).'
            )

    result = _post_multipart_with_retries(
        creds,
        "images/edits",
        fields,
        files,
        attempts=attempts,
        timeout=timeout,
        label=label,
        deadline=deadline,
        slow_after=slow_after,
    )
    return result, "openai-multipart"


# --------------------------------------------------------------------------
# Prompt Preflight, Validation & Unified Prompt Pipeline
# --------------------------------------------------------------------------


def _looks_like_person_prompt(prompt: str) -> bool:
    text = _NEGATED_PERSON_RE.sub(" ", prompt or "")
    return bool(
        _PERSON_CJK_RE.search(text)
        or _PERSON_LATIN_RE.search(text)
        or _PERSON_MODEL_RE.search(text)
    )


def _already_has_coverage_clause(prompt: str) -> bool:
    return bool(_COVERAGE_RE.search(prompt or ""))


def _neutralize_risky_phrases(prompt: str) -> Tuple[str, List[str]]:
    changes: List[str] = []
    result = prompt
    for pattern, replacement, label in _NEUTRALIZATIONS:
        if pattern.search(result):
            result = pattern.sub(replacement, result)
            changes.append(label)
    if changes:
        for pattern, replacement in _SEPARATOR_TIDY:
            result = pattern.sub(replacement, result)
    return result, changes


def _apply_coverage_preflight(
    prompt: str,
    *,
    mode: str,
    has_reference: bool,
    enabled: bool = True,
) -> Tuple[str, bool, List[str]]:
    """Prepare a person/character prompt for the upstream output-side safety gate."""
    del has_reference
    if not enabled or mode == "off":
        return prompt, False, []
    if mode == "auto" and not _looks_like_person_prompt(prompt):
        return prompt, False, []

    if _OUTFIT_LOCK_RE.search(prompt):
        if "既覆盖度不变" in prompt or "existing coverage exactly as it is" in prompt:
            return prompt, False, []
        line = OUTFIT_LOCK_LINE_ZH if _CJK_RE.search(prompt) else OUTFIT_LOCK_LINE_EN
        return prompt + "\n" + line, True, ["服装已锁定 -> 追加中性约束（不改服装、不要求覆盖）"]

    result, changes = _neutralize_risky_phrases(prompt)
    if not _already_has_coverage_clause(result):
        line = COVERAGE_LINE_ZH if _CJK_RE.search(result) else COVERAGE_LINE_EN
        result = result + "\n" + line
        changes.append("追加服装覆盖约束")
    if not changes:
        return prompt, False, []
    return result, True, changes


def _dependency_hint(package: str, *, upgrade: bool = False) -> str:
    command = f"uv pip install {'-U ' if upgrade else ''}{package}"
    return (
        "Install it into the active environment, or run this script through uv, "
        f"for example `uv run --with {package} python image_gen.py ...` "
        f"(equivalent to `{command}`)."
    )


def _read_prompt(prompt: Optional[str], prompt_file: Optional[str]) -> str:
    if prompt and prompt_file:
        _die("Use --prompt or --prompt-file, not both.")
    if prompt_file:
        path = Path(prompt_file)
        if not path.exists():
            _die(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8-sig").strip()
    if prompt:
        return prompt.strip()
    _die("Missing prompt. Use --prompt or --prompt-file.")
    return ""  # unreachable


def _check_image_paths(paths: Iterable[str]) -> List[Path]:
    resolved: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            _die(f"Image file not found: {path}")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Image exceeds 50MB limit: {path}")
        resolved.append(path)
    return resolved


def _normalize_output_format(fmt: Optional[str]) -> str:
    if not fmt:
        return DEFAULT_OUTPUT_FORMAT
    fmt = fmt.lower()
    if fmt not in {"png", "jpeg", "jpg", "webp"}:
        _die("output-format must be png, jpeg, jpg, or webp.")
    return "jpeg" if fmt == "jpg" else fmt


def _parse_size(size: str) -> Optional[Tuple[int, int]]:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", size)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _gpt_image_2_size_problem(size: str) -> Optional[str]:
    if size == "auto":
        return None
    parsed = _parse_size(size)
    if parsed is None:
        return None  # [relay] non WxH tokens (e.g. 1K/2K/4K) pass through

    width, height = parsed
    max_edge = max(width, height)
    min_edge = min(width, height)
    total_pixels = width * height

    if max_edge > GPT_IMAGE_2_MAX_EDGE:
        return "gpt-image-2 size maximum edge length must be less than or equal to 3840px."
    if width % 16 != 0 or height % 16 != 0:
        return "gpt-image-2 size width and height must be multiples of 16px."
    if max_edge / min_edge > GPT_IMAGE_2_MAX_RATIO:
        return "gpt-image-2 size long edge to short edge ratio must not exceed 3:1."
    if total_pixels < GPT_IMAGE_2_MIN_PIXELS or total_pixels > GPT_IMAGE_2_MAX_PIXELS:
        return (
            "gpt-image-2 size total pixels must be at least 655,360 and no more than 8,294,400."
        )
    return None


def _validate_size(size: str, model: str, *, strict: bool) -> None:
    if not strict and size in RELAY_NATIVE_SIZES:
        return
    if model in MODEL_CHOICES:
        problem = _gpt_image_2_size_problem(size)
    elif size not in ALLOWED_LEGACY_SIZES:
        problem = (
            "size must be one of 1024x1024, 1536x1024, 1024x1536, or auto for this GPT Image model."
        )
    else:
        problem = None

    if not problem:
        return
    if strict:
        _die(problem)
    _warn(
        problem
        + " [relay] continuing anyway: the upstream renders at roughly 1.57 MP and folds "
        "`size` into an aspect ratio, so these constraints do not describe the output here."
    )


def _validate_quality(quality: str) -> None:
    if quality not in ALLOWED_QUALITIES:
        _die("quality must be one of low, medium, high, or auto.")


def _validate_background(background: Optional[str]) -> None:
    if background not in ALLOWED_BACKGROUNDS:
        _die("background must be one of transparent, opaque, or auto.")


def _validate_input_fidelity(input_fidelity: Optional[str]) -> None:
    if input_fidelity not in ALLOWED_INPUT_FIDELITIES:
        _die("input-fidelity must be one of low or high.")


def _validate_moderation(moderation: Optional[str]) -> None:
    if moderation not in ALLOWED_MODERATION:
        _die("moderation must be auto or low.")


def _validate_model(model: str) -> None:
    if model in MODEL_CHOICES:
        return
    _die(
        "model must be one of: "
        + ", ".join(MODEL_CHOICES)
        + " (gpt-image-2 = web pipeline first with native fallback; "
        "codex-gpt-image-2 = native subscription endpoint only)."
    )


def _validate_transparency(background: Optional[str], output_format: str) -> None:
    if background == "transparent" and output_format not in {"png", "webp"}:
        _die("transparent background requires output-format png or webp.")


def _validate_model_specific_options(
    *,
    model: str,
    background: Optional[str],
    input_fidelity: Optional[str] = None,
) -> None:
    if model not in MODEL_CHOICES:
        return
    if background == "transparent":
        _warn(
            f"{model} does not accept background=transparent; this relay emulates it with a "
            "prompt requirement plus a local PNG alpha channel (measured ~46-52% transparent "
            "pixels). Use scripts/remove_chroma_key.py if the subject is complex."
        )
    if input_fidelity is not None:
        _warn(
            "input_fidelity is not supported by gpt-image-2 or by this relay; the flag is ignored."
        )


def _validate_image_job_options(
    *,
    model: str,
    n: int,
    size: str,
    quality: str,
    background: Optional[str],
    output_format: str,
    output_compression: Optional[int],
    moderation: Optional[str],
    input_fidelity: Optional[str] = None,
    strict_size: bool = False,
    is_responses_route: bool = False,
) -> None:
    """单次统一参数校验入口（彻底消除 main + command 双重校验）。"""
    if n < 1 or n > 10:
        _die("n must be between 1 and 10")
    if output_compression is not None and not (0 <= int(output_compression) <= 100):
        _die("output_compression must be between 0 and 100")
    _validate_quality(quality)
    _validate_background(background)
    _validate_transparency(background, output_format)
    if not is_responses_route:
        _validate_model(model)
        _validate_size(size, model, strict=strict_size)
        _validate_moderation(moderation)
        _validate_input_fidelity(input_fidelity)
        _validate_model_specific_options(
            model=model, background=background, input_fidelity=input_fidelity
        )


def _fields_from_args(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    return {
        "use_case": getattr(args, "use_case", None),
        "scene": getattr(args, "scene", None),
        "subject": getattr(args, "subject", None),
        "style": getattr(args, "style", None),
        "composition": getattr(args, "composition", None),
        "lighting": getattr(args, "lighting", None),
        "palette": getattr(args, "palette", None),
        "materials": getattr(args, "materials", None),
        "text": getattr(args, "text", None),
        "constraints": getattr(args, "constraints", None),
        "negative": getattr(args, "negative", None),
    }


def _augment_prompt(args: argparse.Namespace, prompt: str) -> str:
    fields = _fields_from_args(args)
    return _augment_prompt_fields(args.augment, prompt, fields)


def _augment_prompt_fields(
    augment: bool, prompt: str, fields: Dict[str, Optional[str]]
) -> str:
    if not augment:
        return prompt
    if _LABELED_SPEC_RE.search(prompt) and not any(fields.values()):
        _info("[prompt] input already looks like a labeled spec -- augmentation wrapper skipped")
        return prompt

    sections: List[str] = []
    if fields.get("use_case"):
        sections.append(f"Use case: {fields['use_case']}")
    sections.append(f"Primary request: {prompt}")
    if fields.get("scene"):
        sections.append(f"Scene/background: {fields['scene']}")
    if fields.get("subject"):
        sections.append(f"Subject: {fields['subject']}")
    if fields.get("style"):
        sections.append(f"Style/medium: {fields['style']}")
    if fields.get("composition"):
        sections.append(f"Composition/framing: {fields['composition']}")
    if fields.get("lighting"):
        sections.append(f"Lighting/mood: {fields['lighting']}")
    if fields.get("palette"):
        sections.append(f"Color palette: {fields['palette']}")
    if fields.get("materials"):
        sections.append(f"Materials/textures: {fields['materials']}")
    if fields.get("text"):
        sections.append(f'Text (verbatim): "{fields["text"]}"')
    if fields.get("constraints"):
        sections.append(f"Constraints: {fields['constraints']}")
    if fields.get("negative"):
        sections.append(f"Avoid: {fields['negative']}")
    return "\n".join(sections)


def _apply_relay_prompt_extras(
    prompt: str, *, quality: Optional[str], background: Optional[str]
) -> str:
    extras: List[str] = []
    hint = QUALITY_HINTS.get((quality or "").lower())
    if hint:
        extras.append(f"Quality: {hint}")
    if (background or "").lower() == "transparent":
        extras.append(f"Background: {TRANSPARENT_PROMPT_LINE}")
    if not extras:
        return prompt
    return prompt + "\n" + "\n".join(extras)


def _prepare_prompt(
    raw_prompt: str,
    *,
    augment: bool,
    fields: Dict[str, Optional[str]],
    quality: Optional[str],
    background: Optional[str],
    preflight_mode: str,
    has_reference: bool,
    log_preflight: bool = True,
    log_prefix: str = "[preflight]",
) -> Tuple[str, bool, List[str]]:
    """五条路由共享的唯一提示词组装管线（augment → relay extras → coverage preflight）。"""
    prompt = _augment_prompt_fields(augment, raw_prompt, fields)
    prompt = _apply_relay_prompt_extras(prompt, quality=quality, background=background)
    prompt, applied, changes = _apply_coverage_preflight(
        prompt, mode=preflight_mode, has_reference=has_reference
    )
    if applied and log_preflight:
        _info(f"{log_prefix} " + "; ".join(changes) + "  (--no-preflight disables)")
    return prompt, applied, changes


def _wire_payload(model: str, prompt: str, n: int, size: Optional[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"model": model, "prompt": prompt, "n": n}
    if size and size != "auto":
        payload["size"] = size
    return payload


def _print_request(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


# --------------------------------------------------------------------------
# Image Parsing, Atomic Disk I/O & Post-Processing
# --------------------------------------------------------------------------


@dataclass
class ImageResult:
    raw: bytes
    actual_size: Optional[str]
    revised_prompt: Optional[str]
    source: str
    mime: Optional[str] = None
    sprite: Optional[bytes] = None
    animation: Optional[Dict[str, Any]] = None


@dataclass
class WrittenArtifact:
    path: str
    downscaled: Optional[str] = None
    sprite_path: Optional[str] = None
    actual_size: Optional[str] = None
    pixels: Optional[Tuple[int, int]] = None
    revised_prompt: Optional[str] = None
    source: str = "b64_json"
    mime: Optional[str] = None


def _looks_like_gif(raw: bytes) -> bool:
    return raw[:6] in (b"GIF87a", b"GIF89a")


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _atomic_write_text(target: Path, text: str) -> None:
    _atomic_write_bytes(target, text.encode("utf-8"))


def _image_size_of(raw: bytes) -> Optional[Tuple[int, int]]:
    """纯 stdlib 解析 PNG / GIF / JPEG / WebP 真实像素尺寸。"""
    # PNG
    if len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    # GIF
    if len(raw) >= 10 and raw[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", raw[6:10])
        return w, h
    # WebP
    if len(raw) >= 30 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        chunk = raw[12:16]
        if chunk == b"VP8X" and len(raw) >= 30:
            w = 1 + int.from_bytes(raw[24:27], "little")
            h = 1 + int.from_bytes(raw[27:30], "little")
            return w, h
        if chunk == b"VP8L" and len(raw) >= 25 and raw[20] == 0x2F:
            bits = int.from_bytes(raw[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and len(raw) >= 30 and raw[23:26] == b"\x9d\x01\x2a":
            w, h = struct.unpack("<HH", raw[26:30])
            return w & 0x3FFF, h & 0x3FFF
    # JPEG
    if len(raw) >= 4 and raw[:2] == b"\xff\xd8":
        i = 2
        n = len(raw)
        while i + 4 <= n:
            while i < n and raw[i] == 0xFF:
                i += 1
            if i >= n:
                break
            marker = raw[i]
            i += 1
            if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
                continue
            if i + 2 > n:
                break
            seg_len = int.from_bytes(raw[i : i + 2], "big")
            if seg_len < 2 or i + seg_len > n:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if seg_len >= 7:
                    h, w = struct.unpack(">HH", raw[i + 3 : i + 7])
                    return w, h
                break
            i += seg_len
    return None


def _result_extra(item: Dict[str, Any]) -> Dict[str, Any]:
    mime = item.get("mime_type")
    sprite_b64 = item.get("sprite_sheet")
    sprite: Optional[bytes] = None
    if isinstance(sprite_b64, str) and sprite_b64:
        try:
            sprite = base64.b64decode(sprite_b64)
        except Exception:
            sprite = None
    anim = item.get("animation")
    return {
        "mime": str(mime) if mime else None,
        "sprite": sprite,
        "animation": anim if isinstance(anim, dict) else None,
    }


def _report_pipeline(
    payload: Any,
    model: str,
    web_mode: Optional[str] = None,
    effort: Optional[str] = None,
    agent_model: Optional[str] = None,
    *,
    web_sources: Optional[Dict[str, str]] = None,
    route: str = "images",
) -> Dict[str, Any]:
    """打印并返回本次实际生效的管线信息（纯返回值，无全局可变状态，线程安全）。"""
    if route == "responses":
        note = f"Responses 图像工具路由（model={model}）"
        info = {
            "pipeline": "responses",
            "note": note,
            "degraded_reason": None,
            "surface": None,
            "surface_source": None,
            "effort": None,
            "agent_model": None,
        }
        _info(f"[pipeline] {note}")
        return info

    payload_dict = payload if isinstance(payload, dict) else {}
    degraded = bool(payload_dict.get("degraded"))
    reason = str(payload_dict.get("degraded_reason") or "")
    forced_native = str(model or "").lower().startswith("codex-")

    if forced_native and not degraded:
        via, note = "native", "按 --model 指定，走原生端点（不经网页管线）"
    elif degraded:
        via, note = "degraded", "网页管线失败 → 自动降级到原生（原因：%s）" % (reason or "未给")
    else:
        via, note = "web", "网页管线（Cookie 会话）"

    src_labels = {"flag": "指定", "config": "配置", "default": "默认"}
    sources = web_sources or {}

    surface: Optional[str] = None
    surface_src: Optional[str] = None
    effort_effective: Optional[str] = None
    agent_effective: Optional[str] = None
    detail = ""

    if via == "web":
        surface = str(web_mode or "chat")
        surface_src = sources.get("web_mode") or ("flag" if web_mode else "default")
        surface_note = src_labels.get(surface_src, "默认")

        eff_raw = str(effort or "").strip()
        eff_src = sources.get("effort") or ("flag" if eff_raw else "default")
        if not eff_raw or eff_raw.lower() in ("none", "auto"):
            effort_effective = None
            effort_shown = "即时档（指定）" if (eff_raw.lower() == "none" and eff_src == "flag") else "默认（即时档）"
        else:
            effort_effective = eff_raw
            effort_shown = f"{eff_raw}（{src_labels.get(eff_src, '指定')}）" if sources else eff_raw

        raw_agent = str(agent_model or "").strip()
        if raw_agent and raw_agent.lower() != "auto":
            agent_effective = raw_agent

        detail = f" · surface={surface}（{surface_note}） · effort={effort_shown}"
        if agent_effective:
            detail += f" · agent_model={agent_effective}"
    elif via == "degraded":
        detail = " · surface/effort 已失效（没走网页管线）"

    info = {
        "pipeline": via,
        "note": note,
        "degraded_reason": reason or None,
        "surface": surface,
        "surface_source": surface_src,
        "effort": effort_effective,
        "agent_model": agent_effective,
    }
    if degraded:
        _warn(f"本次图片来自备用通道（{note}）—— 不是网页管线出的图{detail}")
    else:
        _info(f"[pipeline] {note}{detail}")
    return info


def _parse_images(
    payload: Dict[str, Any],
    *,
    creds: RelayCredentials,
    timeout: float,
    deadline: Optional[float] = None,
) -> List[ImageResult]:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise RelayError(
            f"relay returned no images: {json.dumps(payload)[:MAX_ERROR_TEXT]}",
            code="no_image_data",
        )

    results: List[ImageResult] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise RelayError(f"unexpected image entry at data[{index}]: {item!r}")
        b64 = item.get("b64_json")
        if b64:
            try:
                raw = base64.b64decode(b64)
            except Exception as exc:
                raise RelayError(f"could not decode data[{index}].b64_json: {exc}")
            results.append(
                ImageResult(
                    raw=raw,
                    actual_size=item.get("size"),
                    revised_prompt=item.get("revised_prompt"),
                    source="b64_json",
                    **_result_extra(item),
                )
            )
            continue
        url = item.get("url")
        if url:
            response = _http_request(
                str(url),
                api_key=creds.api_key,
                timeout=_effective_timeout(timeout, deadline),
                method="GET",
                label="[download]",
            )
            results.append(
                ImageResult(
                    raw=response.body,
                    actual_size=item.get("size"),
                    revised_prompt=item.get("revised_prompt"),
                    source="url",
                    **_result_extra(item),
                )
            )
            continue
        raise RelayError("API image entry contains neither b64_json nor url")
    return results


def _build_output_paths(
    out: str,
    output_format: str,
    count: int,
    out_dir: Optional[str],
) -> List[Path]:
    ext = "." + output_format

    if out_dir:
        out_base = Path(out_dir)
        return [out_base / f"image_{i}{ext}" for i in range(1, count + 1)]

    out_path = Path(out)
    if out_path.exists() and out_path.is_dir():
        return [out_path / f"image_{i}{ext}" for i in range(1, count + 1)]

    if out_path.suffix == "":
        out_path = out_path.with_suffix(ext)
    elif output_format and out_path.suffix.lstrip(".").lower() != output_format:
        _warn(f"Output extension {out_path.suffix} does not match output-format {output_format}.")

    if count == 1:
        return [out_path]
    return [
        out_path.with_name(f"{out_path.stem}-{i}{out_path.suffix}")
        for i in range(1, count + 1)
    ]


def _derive_downscale_path(path: Path, suffix: str) -> Path:
    if suffix and not suffix.startswith("-") and not suffix.startswith("_"):
        suffix = "-" + suffix
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def _precheck_outputs(
    output_paths: List[Path],
    *,
    force: bool,
    downscale_max_dim: Optional[int],
    downscale_suffix: str,
) -> None:
    """付费调用前先做 Fail-Fast 预检：检查目标文件冲突与目录可写性。"""
    checked_dirs: Set[Path] = set()
    to_check = list(output_paths)
    if downscale_max_dim is not None:
        to_check.extend(_derive_downscale_path(p, downscale_suffix) for p in output_paths)

    for p in to_check:
        if p.exists() and not force:
            _die(f"Output already exists: {p} (use --force to overwrite)")
        parent = p.parent
        if parent not in checked_dirs:
            try:
                parent.mkdir(parents=True, exist_ok=True)
                probe_fd, probe_name = tempfile.mkstemp(prefix=".imagegen-writecheck-", dir=str(parent))
                os.close(probe_fd)
                Path(probe_name).unlink(missing_ok=True)
            except Exception as exc:
                _die(f"Output directory is not writable: {parent} ({exc})")
            checked_dirs.add(parent)


def _flatten_for_jpeg(image: Any) -> Any:
    from PIL import Image

    if image.mode in ("RGBA", "LA", "P") or ("transparency" in getattr(image, "info", {})):
        rgba = image.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return image.convert("RGB")


def _encode_pil_image(image: Any, *, output_format: str, output_compression: Optional[int]) -> bytes:
    fmt = (output_format or "png").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ("png", "jpeg", "webp"):
        fmt = "png"

    working = _flatten_for_jpeg(image) if fmt == "jpeg" else image
    save_kwargs: Dict[str, Any] = {}
    if fmt in ("jpeg", "webp") and output_compression is not None:
        save_kwargs["quality"] = int(output_compression)
    elif fmt == "png" and output_compression is not None:
        save_kwargs["compress_level"] = max(0, min(9, int(output_compression)))

    out = BytesIO()
    working.save(out, format=fmt.upper(), **save_kwargs)
    return out.getvalue()


def _convert_image_bytes(
    raw: bytes, *, output_format: str, output_compression: Optional[int]
) -> bytes:
    fmt = (output_format or "png").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt == "png" and output_compression is None:
        return raw
    try:
        from PIL import Image
    except Exception:
        _die(
            "Local re-encoding is required for this request (output-format or "
            f"output-compression). {_dependency_hint('pillow')}"
        )
    with Image.open(BytesIO(raw)) as image:
        image.load()
        return _encode_pil_image(image, output_format=fmt, output_compression=output_compression)


def _downscale_image_bytes(image_bytes: bytes, *, max_dim: int, output_format: str) -> bytes:
    try:
        from PIL import Image
    except Exception:
        _die(f"Downscaling requires Pillow. {_dependency_hint('pillow')}")
    if max_dim < 1:
        _die("--downscale-max-dim must be >= 1")
    with Image.open(BytesIO(image_bytes)) as img:
        img.load()
        w, h = img.size
        scale = min(1.0, float(max_dim) / float(max(w, h)))
        target = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
        resized = img if target == (w, h) else img.resize(target, Image.Resampling.LANCZOS)
        return _encode_pil_image(resized, output_format=output_format, output_compression=None)


def _exact_size_image_bytes(
    image_bytes: bytes,
    *,
    target: Tuple[int, int],
    fit: str,
    output_format: str,
    output_compression: Optional[int] = None,
) -> bytes:
    try:
        from PIL import Image
    except Exception:
        _die(f"--exact-size requires Pillow. {_dependency_hint('pillow')}")

    width, height = target
    if width < 1 or height < 1:
        _die("--exact-size must look like 1024x1536")

    with Image.open(BytesIO(image_bytes)) as img:
        img.load()
        has_alpha = img.mode in ("RGBA", "LA") or "transparency" in getattr(img, "info", {})
        source = img.convert("RGBA") if has_alpha else img.convert("RGB")

        if fit == "cover":
            scale = max(width / source.width, height / source.height)
            size = (max(1, int(round(source.width * scale))), max(1, int(round(source.height * scale))))
            resized = source if size == source.size else source.resize(size, Image.Resampling.LANCZOS)
            left = max(0, (resized.width - width) // 2)
            top = max(0, (resized.height - height) // 2)
            working = resized.crop((left, top, left + width, top + height))
        else:
            scale = min(width / source.width, height / source.height)
            size = (max(1, int(round(source.width * scale))), max(1, int(round(source.height * scale))))
            resized = source if size == source.size else source.resize(size, Image.Resampling.LANCZOS)
            working = (
                Image.new("RGBA", (width, height), (0, 0, 0, 0))
                if source.mode == "RGBA"
                else Image.new("RGB", (width, height), (255, 255, 255))
            )
            working.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))

        return _encode_pil_image(
            working, output_format=output_format, output_compression=output_compression
        )


def _parse_exact_size(value: str) -> Tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX*]\s*(\d+)\s*", value or "")
    if not match:
        _die("--exact-size must look like 1024x1536")
    return int(match.group(1)), int(match.group(2))


def _apply_preset(args: Any) -> None:
    """[relay] --preset 16:9@2k|4k -> 比例提示 + 精确交付像素（调用前完成全部硬校验）。"""
    raw = getattr(args, "preset", None)
    if not raw:
        return

    match = re.fullmatch(r"\s*([0-9]+:[0-9]+)\s*@\s*(2k|4k)\s*", str(raw), flags=re.IGNORECASE)
    if not match:
        _die(
            "--preset must look like 16:9@2k or 9:16@4k. Supported ratios: "
            + ", ".join(_PRESETS)
        )
    ratio = match.group(1)
    tier = match.group(2).lower()
    if ratio not in _PRESETS:
        _die(f"--preset ratio {ratio} is not supported. Supported: " + ", ".join(_PRESETS))

    width, height = _PRESETS[ratio][tier]

    if width % 16 or height % 16:
        _die(f"--preset {raw} is not 16-pixel aligned ({width}x{height})")
    if max(width, height) > 4096:
        _die(f"--preset {raw} exceeds the 4096 max edge ({width}x{height})")
    total = width * height
    if total < 262_144 or total > GPT_IMAGE_2_MAX_PIXELS:
        _die(f"--preset {raw} total pixels {total} outside the supported range")

    if getattr(args, "exact_size", None) and tuple(args.exact_size) != (width, height):
        _die(
            f"--preset {raw} wants {width}x{height} but --exact-size says "
            f"{args.exact_size[0]}x{args.exact_size[1]}; pass only one of them"
        )
    args.exact_size = (width, height)

    if hasattr(args, "size"):
        current = getattr(args, "size", None)
        if not current or str(current).strip().lower() == "auto":
            args.size = f"{width}x{height}"

    notes = [f"[preset] {ratio} @ {tier} -> 交付 {width}x{height}"]
    canonical = _CANONICAL_RENDER.get(ratio)
    if canonical:
        scale = width / canonical[0]
        notes.append(f"上游按此比例渲染约 {canonical[0]}x{canonical[1]}，重采样 {scale:.2f}x")
        if scale > 1.5:
            notes.append("⚠️ 放大超过 1.5 倍，画面会偏软（要更锐利就选低一档或缩小比例）")
    else:
        notes.append("上游会按提示词里的比例渲染，随后本地重采样到目标像素")
    _info("  ".join(notes))


def _expand_output_paths(outputs: List[Path], target_count: int) -> List[Path]:
    """当上游返回多于预期数量的产物时，按自然编号顺延路径（避免 foo-1-1.png）。"""
    if len(outputs) >= target_count or not outputs:
        return list(outputs)
    expanded = list(outputs)
    first = outputs[0]
    last = outputs[-1]

    # 检测是否已是 -1..-N 或 _1.._N 序列
    m_first = re.fullmatch(r"(.+)([-_])1", first.stem)
    m_last = re.fullmatch(r"(.+)([-_])(\d+)", last.stem)
    if (
        len(outputs) >= 2
        and m_first
        and m_last
        and m_first.group(1) == m_last.group(1)
        and m_first.group(2) == m_last.group(2)
    ):
        prefix, sep, start_idx = m_first.group(1), m_first.group(2), int(m_last.group(3)) + 1
        for i in range(start_idx, start_idx + (target_count - len(outputs))):
            expanded.append(first.with_name(f"{prefix}{sep}{i}{first.suffix}"))
        return expanded

    # 单图基准 foo.png -> foo.png, foo-1.png, foo-2.png...
    for i in range(1, target_count - len(outputs) + 1):
        expanded.append(first.with_name(f"{first.stem}-{i}{first.suffix}"))
    return expanded


def _write_results(
    results: List[ImageResult],
    outputs: List[Path],
    *,
    force: bool,
    output_format: str,
    output_compression: Optional[int],
    downscale_max_dim: Optional[int],
    downscale_suffix: str,
    exact_size: Optional[Tuple[int, int]] = None,
    exact_fit: str = "cover",
) -> List[WrittenArtifact]:
    artifacts: List[WrittenArtifact] = []
    expanded_outputs = _expand_output_paths(outputs, len(results))

    for index, result in enumerate(results):
        if index >= len(expanded_outputs):
            break
        out_path = expanded_outputs[index]
        is_gif = _looks_like_gif(result.raw) or (result.mime or "").lower() == "image/gif"
        if is_gif and out_path.suffix.lower() != ".gif":
            out_path = out_path.with_name(out_path.stem + ".gif")
        if out_path.exists() and not force:
            _die(f"Output already exists: {out_path} (use --force to overwrite)")

        if is_gif:
            if output_format not in ("png", None) or output_compression:
                _warn("[gif] 动图按原字节保存，--output-format/--output-compression 已忽略")
            if exact_size is not None or downscale_max_dim is not None:
                _warn("[gif] 动图跳过 --exact-size/--downscale（重采样会丢动画）")
            payload = result.raw
        elif exact_size is not None:
            # 直接从原图做一次重采样 + 格式编码，避免 JPEG 双重有损压缩
            payload = _exact_size_image_bytes(
                result.raw,
                target=exact_size,
                fit=exact_fit,
                output_format=output_format,
                output_compression=output_compression,
            )
        else:
            payload = _convert_image_bytes(
                result.raw, output_format=output_format, output_compression=output_compression
            )

        _atomic_write_bytes(out_path, payload)
        print(f"Wrote {out_path}")

        artifact = WrittenArtifact(
            path=str(out_path),
            actual_size=result.actual_size,
            pixels=_image_size_of(payload),
            revised_prompt=result.revised_prompt,
            source=result.source,
            mime=result.mime or ("image/gif" if is_gif else None),
        )

        if result.sprite:
            sprite_path = out_path.with_name(out_path.stem + "-sprite.png")
            if sprite_path.exists() and not force:
                _warn(f"[gif] sprite sheet 已存在且未传 --force，已跳过：{sprite_path}")
            else:
                try:
                    _atomic_write_bytes(sprite_path, result.sprite)
                    anim = result.animation or {}
                    extra = (
                        f"{anim.get('rows')}x{anim.get('columns')} 帧数={anim.get('frame_count')} "
                        f"每帧={anim.get('frame_duration_ms')}ms"
                        if anim
                        else "无 animation 元数据"
                    )
                    print(f"Wrote {sprite_path}  (sprite sheet {extra})")
                    artifact.sprite_path = str(sprite_path)
                except Exception as exc:
                    _warn(f"[gif] sprite sheet 写入失败：{exc}")

        if downscale_max_dim is not None and not is_gif:
            derived = _derive_downscale_path(out_path, downscale_suffix)
            if derived.exists() and not force:
                _die(f"Output already exists: {derived} (use --force to overwrite)")
            resized = _downscale_image_bytes(
                result.raw, max_dim=downscale_max_dim, output_format=output_format
            )
            _atomic_write_bytes(derived, resized)
            print(f"Wrote {derived}")
            artifact.downscaled = str(derived)

        artifacts.append(artifact)
    return artifacts


def _report_record(
    *,
    command: str,
    model: str,
    prompt: str,
    artifacts: List[WrittenArtifact],
    timing: Dict[str, float],
    base_url: str,
    pipeline_info: Optional[Dict[str, Any]] = None,
    preflight: bool = False,
    job: Optional[int] = None,
) -> Dict[str, Any]:
    first = artifacts[0] if artifacts else None
    pinfo = pipeline_info or {}
    rec: Dict[str, Any] = {
        "ok": True,
        "command": command,
        "model": model,
        "endpoint": base_url,
        "prompt": prompt,
        # 始终使用实际落盘后的路径（含 GIF 改后缀、网页管线多图等）
        "outputs": [a.path for a in artifacts],
        "sprites": [a.sprite_path for a in artifacts if a.sprite_path],
        "downscaled": [a.downscaled for a in artifacts if a.downscaled],
        "actual_size": first.actual_size if first else None,
        "pixels": list(first.pixels) if (first and first.pixels) else None,
        "revised_prompt": first.revised_prompt if first else None,
        "image_source": first.source if first else None,
        "pipeline": pinfo.get("pipeline"),
        "pipeline_note": pinfo.get("note"),
        "surface": pinfo.get("surface"),
        "effort": pinfo.get("effort"),
        "agent_model": pinfo.get("agent_model"),
        "coverage_preflight_applied": preflight,
        "timing_seconds": timing,
    }
    if job is not None:
        rec["job"] = job
    return rec


def _write_report(path: Optional[str], record: Any) -> None:
    if not path:
        return
    target = Path(path)
    _atomic_write_text(target, json.dumps(record, indent=2, ensure_ascii=False))
    _info(f"Wrote report {target}")


def _print_timing(
    label: str, response: Optional[HttpResponse], local_s: float, total_s: float
) -> None:
    if response is None:
        _info(f"[timing] {label} local={local_s:.2f}s total={total_s:.1f}s")
        return
    _info(
        f"[timing] {label} relay+render={response.queue_wait_s:.1f}s "
        f"download={response.download_s:.1f}s local={local_s:.2f}s total={total_s:.1f}s"
    )


def _warn_if_extra_artifacts(requested_n: int, artifacts: List[WrittenArtifact]) -> None:
    if requested_n and len(artifacts) > int(requested_n):
        _warn(
            f"上游返回了 {len(artifacts)} 个产物（请求 {int(requested_n)} 个）。"
            "网页管线在 n>1 时会等整轮对话结束、把上游自发的额外图/动画一起保存；"
            "要严格 N 个请加 --strict-n、切 --model codex-gpt-image-2，或分开多次 --n 1。"
        )


def _run_with_refusal_retry(
    base_prompt: str,
    *,
    retry_refusal: int,
    label: str,
    deadline: float,
    call_fn: Callable[[str, float], Any],
) -> Tuple[Any, str]:
    """统一的输出层内容安全随机拒绝重试器（共享唯一 deadline，绝不在循环内重置）。"""
    refusal_attempts = max(1, int(retry_refusal or 0) + 1)
    for attempt in range(1, refusal_attempts + 1):
        if deadline - time.monotonic() <= 0:
            raise RelayError(
                f"{label} aborted: the overall --deadline budget was used up. Nothing was written.",
                retryable=False,
                code="deadline_exceeded",
            )
        attempt_prompt = base_prompt + _REFUSAL_VARIATIONS[(attempt - 1) % len(_REFUSAL_VARIATIONS)]
        try:
            res = call_fn(attempt_prompt, deadline)
            return res, attempt_prompt
        except RelayError as exc:
            if not _is_refusal_error(exc) or attempt >= refusal_attempts:
                raise
            _warn(
                f"{label} upstream refused (attempt {attempt}/{refusal_attempts}); "
                "retrying with a neutral variation (the output gate is stochastic)"
            )
    raise RelayError(f"{label} exhausted refusal retries")


# --------------------------------------------------------------------------
# Batch Helpers
# --------------------------------------------------------------------------


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:60] if value else "job"


def _normalize_job(job: Any, idx: int) -> Dict[str, Any]:
    if isinstance(job, str):
        prompt = job.strip()
        if not prompt:
            _die(f"Empty prompt at job {idx}")
        return {"prompt": prompt}
    if isinstance(job, dict):
        if "prompt" not in job or not str(job["prompt"]).strip():
            _die(f"Missing prompt for job {idx}")
        return job
    _die(f"Invalid job at index {idx}: expected string or object.")
    return {}


def _read_jobs_jsonl(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        _die(f"Input file not found: {p}")
    jobs: List[Dict[str, Any]] = []
    # 使用 utf-8-sig：Windows PowerShell Set-Content -Encoding UTF8 会写 BOM，
    # 否则第 1 行的 \ufeff{"prompt":...} 会让 line.startswith("{") 为 False 并静默当成纯文本！
    for line_no, raw in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item: Any = json.loads(line) if line.startswith("{") else line
            jobs.append(_normalize_job(item, idx=line_no))
        except json.JSONDecodeError as exc:
            _die(f"Invalid JSON on line {line_no}: {exc}")
    if not jobs:
        _die("No jobs found in input file.")
    if len(jobs) > MAX_BATCH_JOBS:
        _die(f"Too many jobs ({len(jobs)}). Max is {MAX_BATCH_JOBS}.")
    return jobs


def _merge_non_null(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(dst)
    for k, v in src.items():
        if v is not None:
            merged[k] = v
    return merged


def _job_output_paths(
    *,
    out_dir: Path,
    output_format: str,
    idx: int,
    prompt: str,
    n: int,
    explicit_out: Optional[str],
) -> List[Path]:
    ext = "." + output_format
    if explicit_out:
        base = Path(explicit_out)
        if base.suffix == "":
            base = base.with_suffix(ext)
        elif base.suffix.lstrip(".").lower() != output_format:
            _warn(
                f"Job {idx}: output extension {base.suffix} does not match output-format {output_format}."
            )
        base = out_dir / base.name
    else:
        slug = _slugify(prompt[:80])
        base = out_dir / f"{idx:03d}-{slug}{ext}"

    if n == 1:
        return [base]
    return [base.with_name(f"{base.stem}-{i}{base.suffix}") for i in range(1, n + 1)]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _log_conv_info(style_dict: Dict[str, Any], *, is_dry_run: bool = False) -> None:
    if not style_dict or is_dry_run:
        return
    if "mssk_conv" in style_dict or "mssk_gif_speed" in style_dict:
        _info(
            f"[会话] 续接钥匙 mssk_conv={style_dict.get('mssk_conv', '-')}"
            + (f"；速度档 {style_dict['mssk_gif_speed']}x" if "mssk_gif_speed" in style_dict else "")
        )


def _generate(args: argparse.Namespace, creds: RelayCredentials) -> None:
    raw_prompt = _read_prompt(args.prompt, args.prompt_file)
    output_format = _normalize_output_format(args.output_format)
    _validate_image_job_options(
        model=args.model,
        n=args.n,
        size=args.size,
        quality=args.quality,
        background=args.background,
        output_format=output_format,
        output_compression=args.output_compression,
        moderation=args.moderation,
        strict_size=args.strict_size,
    )

    prompt, preflight_applied, _preflight_changes = _prepare_prompt(
        raw_prompt,
        augment=args.augment,
        fields=_fields_from_args(args),
        quality=args.quality,
        background=args.background,
        preflight_mode=args.preflight,
        has_reference=False,
    )

    payload = {
        k: v
        for k, v in {
            "model": args.model,
            "prompt": prompt,
            "n": args.n,
            "size": args.size,
            "quality": args.quality,
            "background": args.background,
            "output_format": output_format,
            "output_compression": args.output_compression,
            "moderation": args.moderation,
        }.items()
        if v is not None
    }

    output_paths = _build_output_paths(args.out, output_format, args.n, args.out_dir)
    downscaled = (
        [str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths]
        if args.downscale_max_dim is not None
        else None
    )

    strict_n = bool(getattr(args, "strict_n", False)) and args.n > 1 and args.model == DEFAULT_MODEL
    wire_style = _build_wire_style(args, include_conv=True, defer_conv_persist=True)
    style_dict = wire_style.as_json_style()
    wire = _wire_payload(args.model, prompt, 1 if strict_n else args.n, args.size)
    if style_dict:
        wire["style"] = style_dict

    timeout_s, deadline_s, slow_after = _resolve_timeouts(
        args,
        command="generate",
        n=args.n,
        web_mode=getattr(args, "web_mode", None),
        effort=getattr(args, "effort", None),
    )

    if args.dry_run:
        preview = dict(payload)
        preview["wire_payload"] = wire
        preview["endpoint"] = "/v1/images/generations"
        preview["outputs"] = [str(p) for p in output_paths]
        preview["outputs_downscaled"] = downscaled
        preview["base_url"] = creds.base_url
        preview["timeouts"] = {"timeout_s": timeout_s, "deadline_s": deadline_s}
        if strict_n:
            preview["strict_n_calls"] = args.n
        _print_request(preview)
        return

    _precheck_outputs(
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
    )
    if args.check_model:
        _check_model(creds, args.model, timeout_s)

    _log_conv_info(style_dict)
    _info(
        f"Calling the relay image route (generation"
        f"{f', strict-n: {args.n}×1' if strict_n else ''})."
    )
    started = time.time()
    deadline = time.monotonic() + deadline_s

    all_results: List[ImageResult] = []
    total_queue_s = 0.0
    total_download_s = 0.0
    pipeline_info: Optional[Dict[str, Any]] = None
    final_prompt = prompt
    calls = args.n if strict_n else 1

    for call_idx in range(1, calls + 1):
        call_label = f"[generation {call_idx}/{calls}]" if strict_n else "[generation]"

        def do_call(attempt_prompt: str, dl: float) -> Tuple[HttpResponse, Dict[str, Any]]:
            attempt_wire = dict(wire)
            attempt_wire["prompt"] = attempt_prompt
            return _post_json_with_retries(
                creds,
                "images/generations",
                attempt_wire,
                attempts=args.max_attempts,
                timeout=timeout_s,
                label=call_label,
                deadline=dl,
                slow_after=slow_after,
            )

        (response, decoded), final_prompt = _run_with_refusal_retry(
            prompt,
            retry_refusal=args.retry_refusal,
            label=call_label,
            deadline=deadline,
            call_fn=do_call,
        )
        wire_style.commit_conv()
        total_queue_s += response.queue_wait_s
        total_download_s += response.download_s
        pipeline_info = _report_pipeline(
            decoded,
            args.model,
            getattr(args, "web_mode", None),
            getattr(args, "effort", None),
            getattr(args, "agent_model", None),
            web_sources=getattr(args, "_web_sources", None),
        )
        parsed = _parse_images(decoded, creds=creds, timeout=timeout_s, deadline=deadline)
        if strict_n:
            all_results.append(parsed[0])
        else:
            all_results.extend(parsed)

    local_started = time.time()
    artifacts = _write_results(
        all_results,
        output_paths,
        force=args.force,
        output_format=output_format,
        output_compression=args.output_compression,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        exact_size=getattr(args, "exact_size", None),
        exact_fit=getattr(args, "exact_fit", "cover"),
    )
    local_s = time.time() - local_started
    total_s = time.time() - started
    agg_resp = HttpResponse(status=200, body=b"", queue_wait_s=total_queue_s, download_s=total_download_s)
    _print_timing("generation", agg_resp, local_s, total_s)
    if not strict_n:
        _warn_if_extra_artifacts(args.n, artifacts)

    _write_report(
        args.report_json,
        _report_record(
            command="generate",
            model=args.model,
            prompt=final_prompt,
            artifacts=artifacts,
            timing={
                "relay_and_render": round(total_queue_s, 2),
                "download": round(total_download_s, 2),
                "local": round(local_s, 2),
                "total": round(total_s, 2),
            },
            base_url=creds.base_url,
            pipeline_info=pipeline_info,
            preflight=preflight_applied,
        ),
    )


def _data_uri_for(path: Path, max_dim: int) -> str:
    raw = path.read_bytes()
    mime = _guess_content_type(path)
    if max_dim > 0:
        try:
            from PIL import Image
        except Exception:
            return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        with Image.open(BytesIO(raw)) as image:
            scale = min(1.0, float(max_dim) / float(max(image.size)))
            if scale < 1.0:
                image = image.convert("RGBA").resize(
                    (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                out = BytesIO()
                image.save(out, format="PNG")
                raw = out.getvalue()
                mime = "image/png"
            # scale >= 1.0：不强转 RGBA PNG，保留原始紧凑字节（如 JPEG）
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _edit(args: argparse.Namespace, creds: RelayCredentials) -> None:
    raw_prompt = _read_prompt(args.prompt, args.prompt_file)
    output_format = _normalize_output_format(args.output_format)
    _validate_image_job_options(
        model=args.model,
        n=args.n,
        size=args.size,
        quality=args.quality,
        background=args.background,
        output_format=output_format,
        output_compression=args.output_compression,
        moderation=args.moderation,
        input_fidelity=getattr(args, "input_fidelity", None),
        strict_size=args.strict_size,
    )

    image_paths = _check_image_paths(args.image)
    mask_path = Path(args.mask) if args.mask else None
    if mask_path:
        if not mask_path.exists():
            _die(f"Mask file not found: {mask_path}")
        if mask_path.suffix.lower() != ".png":
            _warn(f"Mask should be a PNG with an alpha channel: {mask_path}")
        if mask_path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Mask exceeds 50MB limit: {mask_path}")

    prompt, preflight_applied, _preflight_changes = _prepare_prompt(
        raw_prompt,
        augment=args.augment,
        fields=_fields_from_args(args),
        quality=args.quality,
        background=args.background,
        preflight_mode=args.preflight,
        has_reference=True,
    )

    payload = {
        k: v
        for k, v in {
            "model": args.model,
            "prompt": prompt,
            "n": args.n,
            "size": args.size,
            "quality": args.quality,
            "background": args.background,
            "output_format": output_format,
            "output_compression": args.output_compression,
            "input_fidelity": getattr(args, "input_fidelity", None),
            "moderation": args.moderation,
        }.items()
        if v is not None
    }

    output_paths = _build_output_paths(args.out, output_format, args.n, args.out_dir)
    downscaled = (
        [str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths]
        if args.downscale_max_dim is not None
        else None
    )

    wire_style = _build_wire_style(args, include_conv=True, defer_conv_persist=True)
    style_dict = wire_style.as_json_style()
    mp_style_fields = wire_style.as_multipart_fields()

    timeout_s, deadline_s, slow_after = _resolve_timeouts(
        args,
        command="edit",
        n=args.n,
        web_mode=getattr(args, "web_mode", None),
        effort=getattr(args, "effort", None),
    )

    if args.dry_run:
        preview = dict(payload)
        preview["image"] = [str(p) for p in image_paths]
        if mask_path:
            preview["mask"] = str(mask_path)
        wire_preview: Dict[str, Any] = {
            "model": args.model,
            "prompt": prompt,
            "n": args.n,
            "image": [str(p) for p in image_paths],
        }
        if style_dict:
            wire_preview["style"] = style_dict
        if mp_style_fields:
            wire_preview["multipart_fields"] = mp_style_fields
        preview["wire_payload"] = wire_preview
        preview["endpoint"] = "/v1/images/edits"
        preview["outputs"] = [str(p) for p in output_paths]
        preview["outputs_downscaled"] = downscaled
        preview["base_url"] = creds.base_url
        preview["timeouts"] = {"timeout_s": timeout_s, "deadline_s": deadline_s}
        _print_request(preview)
        return

    _precheck_outputs(
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
    )
    if args.check_model:
        _check_model(creds, args.model, timeout_s)

    _log_conv_info(style_dict)
    _info(f"Calling the relay image route (edit) with {len(image_paths)} image(s).")
    started = time.time()
    deadline = time.monotonic() + deadline_s

    base_fields: Dict[str, str] = {"model": args.model, "n": str(args.n)}
    base_fields.update(mp_style_fields)

    files: List[Tuple[str, str, bytes, str]] = [
        ("image", path.name, path.read_bytes(), _guess_content_type(path))
        for path in image_paths
    ]
    if mask_path:
        files.append(("mask", mask_path.name, mask_path.read_bytes(), _guess_content_type(mask_path)))

    cached_contract = _load_edit_contract(creds.base_url)
    # 若已缓存该中转站走 openai-multipart，则跳过昂贵的 base64/PNG 重编码
    image_refs = (
        []
        if cached_contract == "openai-multipart"
        else [{"image_url": _data_uri_for(path, args.max_dim)} for path in image_paths]
    )

    def do_call(attempt_prompt: str, dl: float) -> Tuple[Tuple[HttpResponse, Dict[str, Any]], str]:
        fields = dict(base_fields, prompt=attempt_prompt)
        json_payload: Dict[str, Any] = {
            "model": args.model,
            "prompt": attempt_prompt,
            "images": image_refs,
        }
        if args.n and args.n != 1:
            json_payload["n"] = args.n
        if args.size and args.size != "auto":
            json_payload["size"] = args.size
        if args.quality and args.quality != "medium":
            json_payload["quality"] = args.quality
        if args.background:
            json_payload["background"] = args.background
        if style_dict:
            json_payload["style"] = style_dict
        return _post_edit_with_retries(
            creds,
            json_payload=json_payload,
            fields=fields,
            files=files,
            attempts=args.max_attempts,
            timeout=timeout_s,
            label="[edit]",
            deadline=dl,
            slow_after=slow_after,
            cached_contract=cached_contract,
        )

    ((response, decoded), contract), final_prompt = _run_with_refusal_retry(
        prompt,
        retry_refusal=args.retry_refusal,
        label="[edit]",
        deadline=deadline,
        call_fn=do_call,
    )
    wire_style.commit_conv()

    _info(f"[edit] contract used: {contract}")
    pipeline_info = _report_pipeline(
        decoded,
        args.model,
        getattr(args, "web_mode", None),
        getattr(args, "effort", None),
        getattr(args, "agent_model", None),
        web_sources=getattr(args, "_web_sources", None),
    )
    results = _parse_images(decoded, creds=creds, timeout=timeout_s, deadline=deadline)
    local_started = time.time()
    artifacts = _write_results(
        results,
        output_paths,
        force=args.force,
        output_format=output_format,
        output_compression=args.output_compression,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        exact_size=getattr(args, "exact_size", None),
        exact_fit=getattr(args, "exact_fit", "cover"),
    )
    local_s = time.time() - local_started
    total_s = time.time() - started
    _print_timing("edit", response, local_s, total_s)
    _warn_if_extra_artifacts(args.n, artifacts)

    _write_report(
        args.report_json,
        _report_record(
            command="edit",
            model=args.model,
            prompt=final_prompt,
            artifacts=artifacts,
            timing={
                "relay_and_render": round(response.queue_wait_s, 2),
                "download": round(response.download_s, 2),
                "local": round(local_s, 2),
                "total": round(total_s, 2),
            },
            base_url=creds.base_url,
            pipeline_info=pipeline_info,
            preflight=preflight_applied,
        ),
    )


def _iter_sse_events(
    url: str,
    *,
    api_key: str,
    payload: Dict[str, Any],
    timeout: float,
    label: str,
    deadline: Optional[float] = None,
    slow_after: float = 180.0,
):
    """[relay] Stream a Responses-API call and enforce overall --deadline even across keepalives."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urlrequest.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    mono_start = time.monotonic()
    try:
        with _Heartbeat(label, interval=15.0, slow_after=slow_after):
            with urlrequest.urlopen(request, timeout=_effective_timeout(timeout, deadline)) as response:
                raw_sock = getattr(getattr(response, "fp", None), "raw", None)
                sock = getattr(raw_sock, "_sock", None)
                for raw_line in response:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RelayError(
                                f"{label} aborted: the overall --deadline budget was used up "
                                "(stream stayed open with keepalives). Nothing was written.",
                                retryable=False,
                                code="deadline_exceeded",
                            )
                        if sock is not None:
                            try:
                                sock.settimeout(max(1.0, min(timeout, remaining)))
                            except Exception:
                                pass
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        yield json.loads(data)
                    except Exception:
                        continue
    except urlerror.HTTPError as exc:
        raise _relay_error_from_http(exc.code, exc.read(), headers=exc.headers)
    except (urlerror.URLError, TimeoutError, socket.timeout, OSError) as exc:
        elapsed = time.monotonic() - mono_start
        reason = getattr(exc, "reason", exc)
        if _is_timeout_exception(exc):
            raise RelayError(
                f"request timed out after {elapsed:.0f}s talking to {url}: {reason}",
                retryable=True,
                code="timeout",
                is_timeout=True,
                elapsed_s=elapsed,
            )
        raise RelayError(
            f"network error talking to {url}: {reason}",
            retryable=True,
            code="network_error",
            elapsed_s=elapsed,
        )


def _sse_items(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if isinstance(event.get("item"), dict):
        items.append(event["item"])
    response = event.get("response")
    if isinstance(response, dict):
        items.extend(i for i in response.get("output", []) if isinstance(i, dict))
    return items


def _redact_payload_for_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    """dry-run 预览时把 data:image/...;base64,... 缩略，避免往上下文倾倒几百 KB 垃圾。"""
    clone = json.loads(json.dumps(payload))
    for msg in clone.get("input", []):
        for part in msg.get("content", []):
            url = part.get("image_url")
            if isinstance(url, str) and url.startswith("data:"):
                header = url.split(",", 1)[0]
                part["image_url"] = f"<{header}, {len(url)} chars redacted>"
    return clone


def _reference(args: argparse.Namespace, creds: RelayCredentials) -> None:
    """[relay] Reference-to-image through /v1/responses + the hosted image_generation tool."""
    # 只对 CLI 显式传入的无效参数发出警告，绝不对 config.json 里的网页管线默认值误报！
    cli_explicit: Set[str] = getattr(args, "_cli_explicit", set())
    unsupported_flags = [
        ("conv_key", "--conv-key"),
        ("new_conv", "--new-conv"),
        ("web_mode", "--web-mode"),
        ("effort", "--effort"),
        ("agent_model", "--agent-model"),
        ("gif_speed", "--gif-speed"),
    ]
    explicit_unsupported = [
        flag for attr, flag in unsupported_flags
        if attr in cli_explicit and getattr(args, attr, None)
    ]
    if explicit_unsupported:
        _warn(
            "[reference] 以下参数在这条路由上无效（走 /v1/responses，不经 style 透传）："
            + " ".join(explicit_unsupported)
            + "；请改用 --reasoning-effort 控制推理强度"
        )

    raw_prompt = _read_prompt(args.prompt, args.prompt_file)
    output_format = _normalize_output_format(args.output_format or "png")
    _validate_image_job_options(
        model=args.model,
        n=1,
        size=args.size,
        quality=args.quality,
        background=args.background,
        output_format=output_format,
        output_compression=args.output_compression,
        moderation=None,
        strict_size=args.strict_size,
        is_responses_route=True,
    )

    prompt, preflight_applied, _preflight_changes = _prepare_prompt(
        raw_prompt,
        augment=args.augment,
        fields=_fields_from_args(args),
        quality=args.quality,
        background=args.background,
        preflight_mode=args.preflight,
        has_reference=True,
    )

    image_paths = _check_image_paths(args.image)
    output_paths = _build_output_paths(args.out, output_format, 1, args.out_dir)

    tool: Dict[str, Any] = {"type": "image_generation"}
    if args.size and args.size != "auto":
        tool["size"] = args.size
    if args.quality in ("low", "medium", "high"):
        tool["quality"] = args.quality
    if args.background:
        tool["background"] = args.background

    content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if not args.dry_run:
        for path in image_paths:
            content.append({"type": "input_image", "image_url": _data_uri_for(path, args.max_dim)})
    else:
        for path in image_paths:
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"<data:{_guess_content_type(path)};base64, {path.stat().st_size}B file={path.name}>",
                }
            )

    payload: Dict[str, Any] = {
        "model": args.model,
        "input": [{"role": "user", "content": content}],
        "tools": [tool],
        "store": False,
        "stream": True,
    }
    reasoning_effort = getattr(args, "reasoning_effort", None)
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}

    if getattr(args, "brief_guard", "on") == "on":
        content[0] = {
            "type": "input_text",
            "text": content[0]["text"]
            + "\n\n（写图像提示词时请遵守：不要逐件列举人物服装、首饰或身体细节，也不要使用"
            "“露肩/贴身/曲线”这类描述；只写任务、构图、卡面设计与需要保持的身份特征，"
            "服装由参考图决定。）",
        }
        _info("[reference] brief guard: ON (no garment enumeration in the generated brief)")

    timeout_s, deadline_s, slow_after = _resolve_timeouts(
        args,
        command="reference",
        n=1,
        web_mode=None,
        effort=reasoning_effort,
    )

    if args.dry_run:
        preview = _redact_payload_for_preview(payload)
        preview["endpoint"] = "/v1/responses"
        preview["outputs"] = [str(p) for p in output_paths]
        preview["base_url"] = creds.base_url
        preview["input_images"] = [str(p) for p in image_paths]
        preview["timeouts"] = {"timeout_s": timeout_s, "deadline_s": deadline_s}
        preview["wire_payload"] = {
            "model": args.model,
            "prompt": prompt,
            "tool": tool,
            "images": [p.name for p in image_paths],
        }
        _print_request(preview)
        return

    _precheck_outputs(
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
    )
    if args.check_model:
        _check_model(creds, args.model, timeout_s)

    _info("Calling the relay responses route with the image tool (reference generation).")
    started = time.time()
    deadline = time.monotonic() + deadline_s

    def run_stream_once() -> List[ImageResult]:
        results: List[ImageResult] = []
        statuses: List[str] = []
        model_text: List[str] = []
        revised_prompt: Optional[str] = None
        failure_reason: Optional[str] = None

        for event in _iter_sse_events(
            f"{creds.base_url}/responses",
            api_key=creds.api_key,
            payload=payload,
            timeout=timeout_s,
            label="[reference]",
            deadline=deadline,
            slow_after=slow_after,
        ):
            for item in _sse_items(event):
                kind = item.get("type")
                if kind == "image_generation_call":
                    status = str(item.get("status") or "")
                    statuses.append(status)
                    if item.get("revised_prompt"):
                        revised_prompt = str(item["revised_prompt"])
                    result = item.get("result") or ""
                    if status == "completed" and result:
                        results.append(
                            ImageResult(
                                raw=base64.b64decode(result),
                                actual_size=None,
                                revised_prompt=revised_prompt,
                                source="image_generation_call",
                            )
                        )
                    elif status == "failed":
                        failure_reason = "the upstream image tool rejected this request"
                elif kind == "message":
                    text = "".join(
                        part.get("text", "")
                        for part in item.get("content", [])
                        if isinstance(part, dict)
                    ).strip()
                    if text:
                        model_text.append(text)

        if not results:
            elapsed_now = time.time() - started
            detail = failure_reason or "the relay returned no image"
            if model_text:
                detail = f"{detail}. The model said: {model_text[-1][:400]}"
            raise RelayError(
                f"reference generation failed after {elapsed_now:.0f}s (tool status: "
                f"{', '.join(statuses) or 'none'}). {detail}",
                retryable=False,
            )
        return results

    results = _with_retries(
        run_stream_once,
        attempts=args.max_attempts,
        label="[reference]",
        deadline=deadline,
    )

    pipeline_info = _report_pipeline({}, args.model, route="responses")
    local_started = time.time()
    artifacts = _write_results(
        results,
        output_paths,
        force=args.force,
        output_format=output_format,
        output_compression=args.output_compression,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        exact_size=getattr(args, "exact_size", None),
        exact_fit=getattr(args, "exact_fit", "cover"),
    )
    local_s = time.time() - local_started
    total_s = time.time() - started
    _info(
        f"[timing] reference model+render={total_s - local_s:.1f}s "
        f"local={local_s:.2f}s total={total_s:.1f}s"
    )

    _write_report(
        args.report_json,
        _report_record(
            command="reference",
            model=args.model,
            prompt=prompt,
            artifacts=artifacts,
            timing={
                "model_and_render": round(total_s - local_s, 2),
                "local": round(local_s, 2),
                "total": round(total_s, 2),
            },
            base_url=creds.base_url,
            pipeline_info=pipeline_info,
            preflight=preflight_applied,
        ),
    )


def _prepare_batch_job(
    args: argparse.Namespace,
    base_fields: Dict[str, Optional[str]],
    base_payload: Dict[str, Any],
    out_dir: Path,
    idx: int,
    job: Dict[str, Any],
    batch_style_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """dry-run 与实际执行共用的 batch 单任务构造器（保证两者 100% 一致）。"""
    raw_prompt = str(job["prompt"]).strip()
    fields = _merge_non_null(base_fields, job.get("fields", {}))
    fields = _merge_non_null(fields, {k: job.get(k) for k in base_fields.keys()})

    job_payload = dict(base_payload)
    job_payload = _merge_non_null(job_payload, {k: job.get(k) for k in base_payload.keys()})
    job_payload = {k: v for k, v in job_payload.items() if v is not None}

    effective_output_format = _normalize_output_format(job_payload.get("output_format"))
    job_payload["output_format"] = effective_output_format
    n = int(job_payload.get("n", 1))
    model = str(job_payload.get("model", args.model))
    size = str(job_payload.get("size", args.size))

    _validate_image_job_options(
        model=model,
        n=n,
        size=size,
        quality=str(job_payload.get("quality", DEFAULT_QUALITY)),
        background=job_payload.get("background"),
        output_format=effective_output_format,
        output_compression=job_payload.get("output_compression"),
        moderation=job_payload.get("moderation"),
        strict_size=args.strict_size,
    )

    augmented, job_preflight, _changes = _prepare_prompt(
        raw_prompt,
        augment=args.augment,
        fields=fields,
        quality=job_payload.get("quality"),
        background=job_payload.get("background"),
        preflight_mode=args.preflight,
        has_reference=False,
        log_preflight=not args.dry_run,
        log_prefix=f"[job {idx} preflight]",
    )
    job_payload["prompt"] = augmented

    outputs = _job_output_paths(
        out_dir=out_dir,
        output_format=effective_output_format,
        idx=idx,
        prompt=raw_prompt,
        n=n,
        explicit_out=job.get("out"),
    )
    wire = _wire_payload(model, augmented, n, size)
    if batch_style_dict:
        wire["style"] = dict(batch_style_dict)

    return {
        "idx": idx,
        "model": model,
        "n": n,
        "prompt": augmented,
        "payload": job_payload,
        "output_format": effective_output_format,
        "outputs": outputs,
        "wire": wire,
        "preflight": job_preflight,
    }


def _run_generate_batch(args: argparse.Namespace, creds: RelayCredentials) -> int:
    cli_explicit: Set[str] = getattr(args, "_cli_explicit", set())
    if "conv_key" in cli_explicit or "new_conv" in cli_explicit:
        _warn("[generate-batch] 批量模式为每条任务独立建会话，--conv-key / --new-conv 已忽略。")

    jobs = _read_jobs_jsonl(args.input)
    out_dir = Path(args.out_dir)

    base_fields = _fields_from_args(args)
    base_payload = {
        "model": args.model,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
        "background": args.background,
        "output_format": args.output_format,
        "output_compression": args.output_compression,
        "moderation": args.moderation,
    }

    # 批量是一批独立提示词：保留 mode/effort/model/gif_speed，强制去掉 mssk_conv
    batch_style_dict = _build_wire_style(args, include_conv=False).as_json_style()

    prepared_jobs = [
        _prepare_batch_job(args, base_fields, base_payload, out_dir, i, job, batch_style_dict)
        for i, job in enumerate(jobs, start=1)
    ]

    if args.dry_run:
        for spec in prepared_jobs:
            outputs = spec["outputs"]
            downscaled = (
                [str(_derive_downscale_path(p, args.downscale_suffix)) for p in outputs]
                if args.downscale_max_dim is not None
                else None
            )
            preview = dict(spec["payload"])
            preview["job"] = spec["idx"]
            preview["endpoint"] = "/v1/images/generations"
            preview["outputs"] = [str(p) for p in outputs]
            preview["outputs_downscaled"] = downscaled
            preview["wire_payload"] = spec["wire"]
            preview["base_url"] = creds.base_url
            _print_request(preview)
        return 0

    if args.check_model:
        _check_model(creds, args.model, args.timeout or DEFAULT_STANDARD_TIMEOUT_SECONDS)

    skip_existing = bool(getattr(args, "skip_existing", False))
    secrets = (creds.api_key,) if creds.api_key else ()

    def run_job(spec: Dict[str, Any]) -> Tuple[int, bool, Dict[str, Any]]:
        i = spec["idx"]
        job_label = f"[job {i}/{len(prepared_jobs)}]"
        outputs: List[Path] = spec["outputs"]

        if skip_existing and all(p.exists() and p.stat().st_size > 0 for p in outputs):
            _info(f"{job_label} skipped (already exists)")
            return i, True, {
                "ok": True,
                "skipped": True,
                "job": i,
                "command": "generate-batch",
                "model": spec["model"],
                "outputs": [str(p) for p in outputs],
            }

        try:
            _precheck_outputs(
                outputs,
                force=args.force,
                downscale_max_dim=args.downscale_max_dim,
                downscale_suffix=args.downscale_suffix,
            )
            timeout_s, deadline_s, slow_after = _resolve_timeouts(
                args,
                command="generate-batch",
                n=spec["n"],
                web_mode=getattr(args, "web_mode", None),
                effort=getattr(args, "effort", None),
            )
            job_deadline = time.monotonic() + deadline_s

            _info(f"{job_label} starting")
            started = time.time()

            def do_call(attempt_prompt: str, dl: float) -> Tuple[HttpResponse, Dict[str, Any]]:
                attempt_wire = dict(spec["wire"])
                attempt_wire["prompt"] = attempt_prompt
                return _post_json_with_retries(
                    creds,
                    "images/generations",
                    attempt_wire,
                    attempts=args.max_attempts,
                    timeout=timeout_s,
                    label=job_label,
                    deadline=dl,
                    slow_after=slow_after,
                )

            (response, decoded), final_prompt = _run_with_refusal_retry(
                spec["prompt"],
                retry_refusal=args.retry_refusal,
                label=job_label,
                deadline=job_deadline,
                call_fn=do_call,
            )

            pipeline_info = _report_pipeline(
                decoded,
                spec["model"],
                getattr(args, "web_mode", None),
                getattr(args, "effort", None),
                getattr(args, "agent_model", None),
                web_sources=getattr(args, "_web_sources", None),
            )
            results = _parse_images(decoded, creds=creds, timeout=timeout_s, deadline=job_deadline)
            local_started = time.time()
            artifacts = _write_results(
                results,
                outputs,
                force=args.force,
                output_format=spec["output_format"],
                output_compression=spec["payload"].get("output_compression"),
                downscale_max_dim=args.downscale_max_dim,
                downscale_suffix=args.downscale_suffix,
                exact_size=getattr(args, "exact_size", None),
                exact_fit=getattr(args, "exact_fit", "cover"),
            )
            local_s = time.time() - local_started
            elapsed = time.time() - started
            _print_timing(job_label, response, local_s, elapsed)
            _warn_if_extra_artifacts(spec["n"], artifacts)
            _info(f"{job_label} completed in {elapsed:.1f}s")
            rec = _report_record(
                command="generate-batch",
                model=spec["model"],
                prompt=final_prompt,
                artifacts=artifacts,
                timing={
                    "relay_and_render": round(response.queue_wait_s, 2),
                    "download": round(response.download_s, 2),
                    "local": round(local_s, 2),
                    "total": round(elapsed, 2),
                },
                base_url=creds.base_url,
                pipeline_info=pipeline_info,
                preflight=spec["preflight"],
                job=i,
            )
            return i, True, rec
        except RelayError as exc:
            desc = _failure_dict(exc, secrets=secrets)
            _info(f"{job_label} failed:")
            _print_failure_lines(desc, str(exc), secrets=secrets)
            rec = {
                "ok": False,
                "job": i,
                "command": "generate-batch",
                "model": spec["model"],
                "error": desc,
            }
            if args.fail_fast:
                raise
            return i, False, rec
        except SystemExit as exc:
            msg = str(exc.args[0]) if exc.args else "exited"
            _info(f"{job_label} failed: {msg}")
            rec = {
                "ok": False,
                "job": i,
                "command": "generate-batch",
                "model": spec["model"],
                "error": describe_failure(msg, secrets=secrets),
            }
            if args.fail_fast:
                raise
            return i, False, rec

    ordered_results: List[Tuple[int, bool, Dict[str, Any]]] = []
    if args.concurrency == 1:
        for spec in prepared_jobs:
            ordered_results.append(run_job(spec))
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(run_job, spec) for spec in prepared_jobs]
            for future in futures:
                ordered_results.append(future.result())

    ordered_results.sort(key=lambda t: t[0])
    records = [rec for _i, _ok, rec in ordered_results]
    any_failed = any(not ok for _i, ok, _rec in ordered_results)

    if args.report_json:
        _write_report(args.report_json, records)

    return 1 if any_failed else 0


def _generate_batch(args: argparse.Namespace, creds: RelayCredentials) -> None:
    exit_code = _run_generate_batch(args, creds)
    if exit_code:
        raise SystemExit(exit_code)


def _image_model_ids(payload: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    for item in payload.get("data", []):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "")
        endpoints = item.get("supported_endpoint_types") or []
        if "image-generation" in endpoints or "image" in model_id.lower():
            found.append(model_id)
    return sorted(set(found))


def _list_models(creds: RelayCredentials, timeout: float) -> Dict[str, Any]:
    url = f"{creds.base_url}/models"
    response = _http_request(url, api_key=creds.api_key, timeout=timeout, method="GET", label="[models]")
    try:
        payload = json.loads(_decode_body(response.body))
    except Exception as exc:
        raise RelayError(f"relay returned invalid JSON from /models: {exc}")
    if not isinstance(payload, dict):
        raise RelayError("unexpected /models response shape")
    return payload


def _check_model(creds: RelayCredentials, model: str, timeout: float) -> None:
    payload = _list_models(creds, timeout)
    ids = [str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict)]
    if model not in ids:
        _die(
            f"model `{model}` is not exposed by this relay. Available image models: "
            f"{', '.join(_image_model_ids(payload)) or 'none'}"
        )
    _info(f"Model check passed: {model} is exposed by the relay ({len(ids)} models total).")


def _models_command(args: argparse.Namespace, creds: RelayCredentials) -> None:
    payload = _list_models(creds, args.timeout or DEFAULT_STANDARD_TIMEOUT_SECONDS)
    ids = [str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict)]
    print(f"Relay: {creds.base_url}")
    print(f"Models total: {len(ids)}")
    print("Image-capable models:")
    for model_id in _image_model_ids(payload):
        print(f"  - {model_id}")


# --------------------------------------------------------------------------
# Argument Parsing (Official _add_shared_args + # [relay] flags)
# --------------------------------------------------------------------------


def _add_shared_args(parser: argparse.ArgumentParser) -> None:
    # Official imagegen CLI flags (order & names kept identical to official)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--size", default=DEFAULT_SIZE)
    parser.add_argument("--quality", default=DEFAULT_QUALITY)
    parser.add_argument("--background")
    parser.add_argument("--output-format")
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--moderation")
    parser.add_argument("--out", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--out-dir")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--augment", dest="augment", action="store_true")
    parser.add_argument("--no-augment", dest="augment", action="store_false")
    parser.set_defaults(augment=True)

    # Prompt augmentation hints (official 11 fields)
    for flag in (
        "--use-case", "--scene", "--subject", "--style", "--composition",
        "--lighting", "--palette", "--materials", "--text", "--constraints", "--negative",
    ):
        parser.add_argument(flag)

    # Post-processing (official)
    parser.add_argument("--downscale-max-dim", type=int)
    parser.add_argument("--downscale-suffix", default=DEFAULT_DOWNSCALE_SUFFIX)

    # [relay] web-pipeline controls, presets, conversation continuity & resilience
    parser.add_argument(
        "--web-mode",
        choices=("chat", "work"),
        default=None,
        help="网页管线的 surface：chat（默认，生图通过率更高）/ work（做卡片/素材，4 档滑杆）",
    )
    parser.add_argument(
        "--effort",
        choices=EFFORT_ORDER,
        default=None,
        help=f"网页管线推理档：{'/'.join(EFFORT_ORDER)}（chat 3 档：none/standard/extended；"
        "work 4 档：minimal/low/standard/extended；reference 路由请用 --reasoning-effort）",
    )
    parser.add_argument(
        "--agent-model",
        default=None,
        help="直接指定上游推理模型 slug（传 auto 可临时覆盖 config.json 回到自动）",
    )
    parser.add_argument(
        "--conv-key",
        dest="conv_key",
        default=None,
        help="指定上游对话钥匙（style.mssk_conv）。同一钥匙 = 同一上游对话",
    )
    parser.add_argument(
        "--new-conv",
        dest="new_conv",
        action="store_true",
        help="换一把新钥匙（开一个全新上游对话）；请求成功后更新本地默认钥匙",
    )
    parser.add_argument(
        "--gif-speed",
        dest="gif_speed",
        default=None,
        choices=["0.25", "0.5", "0.75", "1", "1.25", "1.5", "1.75", "2"],
        help="GIF 播放速度档位（0.25~2，8 档）。只影响 animation.frame_duration_ms，GIF 文件本身是 1x",
    )
    parser.add_argument(
        "--preset",
        type=str,
        default=None,
        help="ratio preset like 16:9@2k or 9:16@4k (sets ratio hint + exact delivered pixels)",
    )
    parser.add_argument(
        "--exact-size",
        dest="exact_size",
        type=_parse_exact_size,
        default=None,
        help="resample the delivered file to exactly WxH (e.g. 1024x1536)",
    )
    parser.add_argument(
        "--fit",
        dest="exact_fit",
        choices=("cover", "contain"),
        default="cover",
        help="with --exact-size: cover = crop to fill (default), contain = letterbox",
    )
    parser.add_argument("--base-url", help="override the relay base URL (token still comes from config.json)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="per-request HTTP timeout in seconds (auto: 150s normal / 300s heavy)",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=None,
        help="overall wall-clock budget for one job in seconds (auto: 240s normal / 480s heavy)",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--check-model", action="store_true")
    parser.add_argument("--strict-size", action="store_true")
    parser.add_argument("--report-json")
    parser.add_argument(
        "--preflight",
        choices=["off", "auto", "always"],
        default="off",
        help="coverage preflight (default: off; auto = person prompts only; always = all)",
    )
    parser.add_argument(
        "--no-preflight",
        dest="preflight",
        action="store_const",
        const="off",
        help="shorthand for --preflight off (default)",
    )
    parser.add_argument(
        "--retry-refusal",
        type=int,
        default=0,
        metavar="N",
        help="retry stochastic output-stage content-safety refusals up to N extra times (default: 0)",
    )


def _apply_config_web_defaults(args: argparse.Namespace) -> None:
    """将 config.json 固化的网页三件套仅注入支持网页管线的子命令，并记录每个参数的来源。"""
    cmd = getattr(args, "command", "")
    explicit: Set[str] = set()
    for attr in ("agent_model", "effort", "web_mode", "conv_key", "new_conv", "gif_speed"):
        val = getattr(args, attr, None)
        if val not in (None, False, ""):
            explicit.add(attr)
    args._cli_explicit = explicit

    sources: Dict[str, str] = {}
    if cmd in WEB_PIPELINE_COMMANDS:
        defaults = _config_defaults()
        for attr in ("agent_model", "effort", "web_mode"):
            cur = getattr(args, attr, None)
            if cur not in (None, ""):
                sources[attr] = "flag"
            elif defaults.get(attr):
                setattr(args, attr, defaults[attr])
                sources[attr] = "config"
            else:
                sources[attr] = "default"
    args._web_sources = sources


def main() -> int:
    _force_utf8_stdio()

    parser = argparse.ArgumentParser(
        description=(
            "Image generation or editing CLI for this Codex installation. "
            "Talks directly to the MSSK relay image routes with a stdlib HTTP client."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen_parser = subparsers.add_parser("generate", help="Create a new image")
    _add_shared_args(gen_parser)
    gen_parser.add_argument(
        "--strict-n",
        action="store_true",
        help="on the web pipeline with --n > 1, run N sequential n=1 calls to deliver exactly N images",
    )
    gen_parser.set_defaults(func=_generate)

    batch_parser = subparsers.add_parser(
        "generate-batch",
        help="Generate multiple prompts concurrently (JSONL input)",
    )
    _add_shared_args(batch_parser)
    batch_parser.add_argument("--input", required=True, help="Path to JSONL file (one job per line)")
    batch_parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    batch_parser.add_argument("--fail-fast", action="store_true")
    batch_parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="resume-friendly: skip jobs whose output file(s) already exist and are non-empty",
    )
    batch_parser.set_defaults(func=_generate_batch)

    edit_parser = subparsers.add_parser("edit", help="Edit an existing image")
    _add_shared_args(edit_parser)
    edit_parser.add_argument("--image", action="append", required=True)
    edit_parser.add_argument("--mask")
    edit_parser.add_argument(
        "--max-dim",
        type=int,
        default=0,
        help="downscale each input image before upload (0 = original bytes)",
    )
    edit_parser.add_argument("--input-fidelity")
    edit_parser.set_defaults(func=_edit)

    models_parser = subparsers.add_parser(
        "models", help="List the image-capable models this relay exposes"
    )
    models_parser.add_argument("--base-url")
    models_parser.add_argument("--timeout", type=float, default=DEFAULT_STANDARD_TIMEOUT_SECONDS)
    models_parser.add_argument("--report-json")
    models_parser.set_defaults(func=_models_command)

    tiers_parser = subparsers.add_parser(
        "tiers", help="List web-pipeline reasoning tiers (same names as the web UI)"
    )
    tiers_parser.add_argument("--mode", choices=("chat", "work"), default=None)
    tiers_parser.add_argument("--json", action="store_true")
    tiers_parser.add_argument("--efforts", action="store_true", help="只列档位（需配合 --mode）")
    tiers_parser.set_defaults(func=_cmd_tiers)

    setup_parser = subparsers.add_parser(
        "setup", help="查看或记住网页管线的模型/档位/surface（写进 config.json）"
    )
    setup_parser.add_argument("--agent-model", default=None, help="上游 slug；传 auto 或空串表示清除")
    setup_parser.add_argument("--effort", choices=EFFORT_ORDER, default=None)
    setup_parser.add_argument("--web-mode", choices=("chat", "work"), default=None)
    setup_parser.add_argument(
        "--show",
        action="store_true",
        help="只读查看当前配置状态（绝不输出 api_key，适合 agent 检查是否已首次配置）",
    )
    setup_parser.add_argument("--json", action="store_true", help="配合 --show 输出 JSON")
    setup_parser.add_argument("--base-url", default=None, help=argparse.SUPPRESS)
    setup_parser.set_defaults(func=_cmd_setup)

    ref_parser = subparsers.add_parser(
        "reference",
        help="Generate from reference image(s) via /v1/responses + the image_generation tool",
    )
    _add_shared_args(ref_parser)
    ref_parser.add_argument("--image", action="append", required=True)
    ref_parser.add_argument(
        "--max-dim",
        type=int,
        default=DEFAULT_REFERENCE_MAX_DIM,
        help="downscale the reference before upload (default: %(default)s px; 0 keeps the original)",
    )
    ref_parser.add_argument(
        "--brief-guard",
        choices=["on", "off"],
        default="on",
        help="ask the chat model not to enumerate garments/body details in the image prompt it writes",
    )
    ref_parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh"],
        default=None,
        help="reasoning effort for the chat model that drives the image tool (sent as reasoning.effort)",
    )
    ref_parser.set_defaults(func=_reference, model=DEFAULT_RESPONSES_MODEL)

    args = parser.parse_args()
    _apply_config_web_defaults(args)

    if args.command in ("tiers", "setup"):
        return int(args.func(args) or 0)

    if args.command == "models":
        creds = _resolve_credentials(args)
        _ensure_credentials(creds, dry_run=False)
        try:
            args.func(args, creds)
        except RelayError as exc:
            _emit_failure(exc, args, secrets=(creds.api_key,) if creds.api_key else ())
        except KeyboardInterrupt:
            _die("interrupted; nothing was written", code=130)
        return 0

    _apply_preset(args)

    if args.command in WEB_PIPELINE_COMMANDS:
        _validate_web_controls(
            getattr(args, "web_mode", None),
            getattr(args, "effort", None),
            getattr(args, "agent_model", None),
        )

    if getattr(args, "concurrency", 1) < 1 or getattr(args, "concurrency", 1) > 25:
        _die("--concurrency must be between 1 and 25")
    if getattr(args, "max_attempts", 3) < 1 or getattr(args, "max_attempts", 3) > 10:
        _die("--max-attempts must be between 1 and 10")
    if args.command == "generate-batch" and not args.out_dir:
        _die("generate-batch requires --out-dir")
    if getattr(args, "downscale_max_dim", None) is not None and args.downscale_max_dim < 1:
        _die("--downscale-max-dim must be >= 1")
    if args.timeout is not None and args.timeout <= 0:
        _die("--timeout must be > 0")
    if args.deadline is not None and args.deadline <= 0:
        _die("--deadline must be > 0")

    creds = _resolve_credentials(args)
    _ensure_credentials(creds, dry_run=args.dry_run)

    try:
        args.func(args, creds)
    except RelayError as exc:
        _emit_failure(exc, args, secrets=(creds.api_key,) if creds.api_key else ())
    except KeyboardInterrupt:
        _die("interrupted; nothing was written", code=130)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
