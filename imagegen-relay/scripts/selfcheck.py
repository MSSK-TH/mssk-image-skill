#!/usr/bin/env python3
"""selfcheck.py —— 零成本全量回归自检（不发任何真实出图请求、不消耗额度）。

用法：
    python scripts/selfcheck.py            # 全部检查
    python scripts/selfcheck.py -v         # 打印每项细节

覆盖维度：
  1. 代码完整性（无重复顶层函数定义）
  2. 全路由 --dry-run（含 style 真实性、reference base64 脱敏、零磁盘副作用、BOM 去重）
  3. 全部 22 个 --preset（含 11 个 @4k）
  4. 4 路由 × 每契约 style/web_* 逐条校验（堵住并集盲区）+ auto/none 覆盖
  5. tiers 两步提问
  6. setup 持久化 / --show 不泄密 / --reset / reference 不误报 config 默认值
  7. pipeline 上报（含 responses、config 来源、无全局态串扰）
  8. 错误分类（16 类 + HTTP 兜底 + 503 relay_cpu 不重试 + no_image_data HINT + refusal 人话识别 + 脱敏）
  9. 产物命名、GIF 报告路径、多格式像素头解析、写前预检、分层超时、deadline 共享、batch 续跑
  10. 非法参数与缺失 api_url 的干净拒绝
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = HERE / "image_gen.py"
CONFIG = ROOT / "config.json"

sys.path.insert(0, str(HERE))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))


def run(
    args: list[str],
    *,
    stdin: str | None = None,
    cwd: str | Path | None = None,
    env_extra: dict[str, str] | None = None,
    clear_relay_env: bool = False,
) -> tuple[int, str, str]:
    env = dict(os.environ)
    if clear_relay_env:
        for k in (
            "IMAGE_RELAY_URL", "IMAGE_RELAY_KEY",
            "CODEX_API_URL", "CODEX_API_KEY",
            "OPENAI_BASE_URL", "OPENAI_API_KEY",
        ):
            env.pop(k, None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(CLI)] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=stdin,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ─────────────────────────────────────────── 1. 代码完整性
def check_code_integrity() -> None:
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    defs: dict[str, list[int]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            defs.setdefault(node.name, []).append(node.lineno)
    dups = {k: v for k, v in defs.items() if len(v) > 1}
    record("image_gen.py 无重复顶层函数定义", not dups, str(dups) if dups else "")


# ─────────────────────────────────────────── 2. dry-run、BOM 与零副作用
def check_dry_run() -> None:
    env_ok = {
        "IMAGE_RELAY_URL": "https://selfcheck.invalid/v1",
        "IMAGE_RELAY_KEY": "selfcheck-dummy-key",
    }
    with tempfile.TemporaryDirectory() as td:
        ref = ROOT / "assets" / "imagegen.png"
        cases = {
            "generate --dry-run": ["generate", "--prompt", "x", "--dry-run"],
            "generate 全参数 --dry-run": [
                "generate", "--prompt", "x", "--n", "2", "--background", "transparent",
                "--agent-model", "gpt-6-thinking", "--effort", "extended", "--web-mode", "chat",
                "--preset", "16:9@2k", "--strict-n", "--dry-run",
            ],
            "edit --dry-run": [
                "edit", "--image", str(ref), "--prompt", "x",
                "--web-mode", "work", "--effort", "extended", "--dry-run",
            ],
            "reference --dry-run": ["reference", "--image", str(ref), "--prompt", "x", "--dry-run"],
        }
        jobs = Path(td) / "jobs.jsonl"
        jobs.write_text('{"prompt":"a"}\n', encoding="utf-8")
        cases["batch --dry-run"] = [
            "generate-batch", "--input", str(jobs), "--out-dir", td,
            "--web-mode", "work", "--effort", "extended", "--dry-run",
        ]
        for name, argv in cases.items():
            code, out, err = run(argv, cwd=td, env_extra=env_ok)
            ok = code == 0 and ("endpoint" in out or "wire_payload" in out)
            record(f"dry-run: {name}", ok, "" if ok else (out + err)[-300:])

        # edit & batch dry-run 必须如实反映 style / web_*
        code, out, _ = run(cases["edit --dry-run"], cwd=td, env_extra=env_ok)
        record(
            "edit --dry-run 如实包含 style 与 web_*",
            code == 0 and '"mssk_mode": "work"' in out and '"web_mode": "work"' in out,
            out[-200:],
        )
        code, out, _ = run(cases["batch --dry-run"], cwd=td, env_extra=env_ok)
        record(
            "batch --dry-run 如实包含 style（且无 mssk_conv）",
            code == 0 and '"mssk_mode": "work"' in out and "mssk_conv" not in out,
            out[-200:],
        )

        # reference --dry-run 不能把整段 base64 倾倒进 stdout
        code, out, _ = run(cases["reference --dry-run"], cwd=td, env_extra=env_ok)
        record(
            "reference --dry-run 脱敏 base64 图像",
            code == 0 and "data:image/png;base64,iVBOR" not in out and "file=imagegen.png" in out,
            out[:200],
        )

        # generate --dry-run 和 --new-conv --dry-run 严格零磁盘副作用
        fresh = Path(td) / "fresh_proj"
        fresh.mkdir()
        run(["generate", "--prompt", "x", "--new-conv", "--dry-run"], cwd=fresh, env_extra=env_ok)
        record("dry-run 零磁盘副作用（不创建 .imagegen）", not (fresh / ".imagegen").exists())

        # prompt-file 带 BOM 时去掉 \ufeff
        bom = Path(td) / "bom.txt"
        bom.write_bytes(b"\xef\xbb\xbf" + "画一只猫".encode("utf-8"))
        code, out, _ = run(["generate", "--prompt-file", str(bom), "--dry-run"], cwd=td, env_extra=env_ok)
        record("prompt-file 去掉 UTF-8 BOM", code == 0 and "\\ufeff" not in out and "\ufeff" not in out)

        # batch JSONL 带 BOM 时第 1 行仍按 JSON 对象解析（保留 size）
        bom_jl = Path(td) / "bom.jsonl"
        bom_jl.write_bytes(b"\xef\xbb\xbf" + '{"prompt":"一只猫","size":"1672x941"}\n'.encode("utf-8"))
        code, out, _ = run(
            ["generate-batch", "--input", str(bom_jl), "--out-dir", td, "--dry-run"],
            cwd=td,
            env_extra=env_ok,
        )
        record(
            "batch JSONL 去掉 UTF-8 BOM（保留 JSON 字段）",
            code == 0 and '"size": "1672x941"' in out and "\\ufeff" not in out,
            out[-240:],
        )


# ─────────────────────────────────────────── 3. 全部 22 个预设（含 11 个 @4k）
def check_presets() -> None:
    import argparse
    import image_gen

    bad = []
    for ratio, tiers in image_gen._PRESETS.items():
        for tier in tiers:
            ns = argparse.Namespace(preset=f"{ratio}@{tier}", exact_size=None, size="auto")
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    image_gen._apply_preset(ns)
            except SystemExit as exc:
                bad.append(f"{ratio}@{tier} (exit {exc.code})")
    record(f"全部 {len(image_gen._PRESETS) * 2} 个 @2k/@4k 预设通过校验", not bad, "; ".join(bad))


# ─────────────────────────────────────────── 4. style 注入矩阵（逐契约严格校验）
def check_style_matrix() -> None:
    import image_gen

    captured: list[tuple[str, str, dict]] = []
    real_json = image_gen._post_json_with_retries
    real_edit = image_gen._post_edit_with_retries

    def fake_json(creds, path, payload, **kw):
        captured.append(("json", kw.get("label", str(path)), payload.get("style") or {}))
        raise SystemExit(0)

    def fake_edit(creds, *, json_payload, fields, files, **kw):
        captured.append(("codex-json", "edit", json_payload.get("style") or {}))
        mp_map = {
            "web_conv_key": "mssk_conv",
            "web_mode": "mssk_mode",
            "web_model": "mssk_model",
            "web_effort": "mssk_effort",
            "gif_speed": "mssk_gif_speed",
        }
        captured.append((
            "multipart",
            "edit",
            {mp_map[k]: v for k, v in fields.items() if k in mp_map},
        ))
        raise SystemExit(0)

    image_gen._post_json_with_retries = fake_json
    image_gen._post_edit_with_retries = fake_edit

    old_url = os.environ.get("IMAGE_RELAY_URL")
    old_key = os.environ.get("IMAGE_RELAY_KEY")
    os.environ["IMAGE_RELAY_URL"] = "https://selfcheck.invalid/v1"
    os.environ["IMAGE_RELAY_KEY"] = "selfcheck-dummy-key"

    common = [
        "--web-mode", "work", "--effort", "extended",
        "--agent-model", "gpt-6-astra-wm", "--conv-key", "selfcheck", "--gif-speed", "1.5",
    ]
    ref = str(ROOT / "assets" / "imagegen.png")
    with tempfile.TemporaryDirectory() as td:
        jobs = Path(td) / "j.jsonl"
        jobs.write_text('{"prompt":"a"}\n', encoding="utf-8")
        routes = {
            "generate": (
                ["generate", "--prompt", "x", "--out", str(Path(td) / "a.png")] + common,
                {"mssk_mode", "mssk_effort", "mssk_model", "mssk_conv", "mssk_gif_speed"},
            ),
            "generate(native)": (
                ["generate", "--prompt", "x", "--model", "codex-gpt-image-2",
                 "--out", str(Path(td) / "b.png")] + common,
                {"mssk_mode", "mssk_effort", "mssk_model", "mssk_conv", "mssk_gif_speed"},
            ),
            "edit": (
                ["edit", "--image", ref, "--prompt", "x", "--out", str(Path(td) / "c.png")] + common,
                {"mssk_mode", "mssk_effort", "mssk_model", "mssk_conv", "mssk_gif_speed"},
            ),
            "batch": (
                ["generate-batch", "--input", str(jobs), "--out-dir", td] + common,
                {"mssk_mode", "mssk_effort", "mssk_model", "mssk_gif_speed"},
            ),
        }
        old_argv = sys.argv
        for name, (argv, need) in routes.items():
            captured.clear()
            sys.argv = ["image_gen.py"] + argv
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    image_gen.main()
                except (SystemExit, Exception):
                    pass
            # 逐条契约单独校验（严禁取并集）
            missing_details = []
            if not captured:
                missing_details.append("未触发发送函数")
            for kind, _label, style in captured:
                miss = need - set(style.keys())
                if miss:
                    missing_details.append(f"{kind} 缺 {miss}")
            if name == "edit" and len(captured) < 2:
                missing_details.append("edit 应同时校验 codex-json 与 multipart")
            record(f"style 逐契约注入: {name}", not missing_details, "; ".join(missing_details))

        # batch 绝不带 mssk_conv
        captured.clear()
        sys.argv = ["image_gen.py"] + routes["batch"][0]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                image_gen.main()
            except (SystemExit, Exception):
                pass
        conv_leaked = any("mssk_conv" in style for _k, _l, style in captured)
        record("batch 不带 mssk_conv", not conv_leaked, "batch 出现了续接钥匙" if conv_leaked else "")

        # --effort none 绝不发 mssk_effort；--agent-model auto 绝不发 mssk_model
        captured.clear()
        sys.argv = [
            "image_gen.py", "generate", "--prompt", "x", "--out", str(Path(td) / "d.png"),
            "--web-mode", "chat", "--effort", "none", "--agent-model", "auto",
        ]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                image_gen.main()
            except (SystemExit, Exception):
                pass
        st = captured[0][2] if captured else {}
        record(
            "effort=none / agent-model=auto 不发无效字段",
            bool(captured) and "mssk_effort" not in st and "mssk_model" not in st,
            str(st),
        )

        sys.argv = old_argv

    image_gen._post_json_with_retries = real_json
    image_gen._post_edit_with_retries = real_edit
    if old_url is None:
        os.environ.pop("IMAGE_RELAY_URL", None)
    else:
        os.environ["IMAGE_RELAY_URL"] = old_url
    if old_key is None:
        os.environ.pop("IMAGE_RELAY_KEY", None)
    else:
        os.environ["IMAGE_RELAY_KEY"] = old_key


# ─────────────────────────────────────────── 5. tiers
def check_tiers() -> None:
    code, out, _ = run(["tiers"])
    ok = code == 0 and "聊天" in out and "工作" in out and "模型" not in out
    record("tiers 第一步只列模式", ok, out[-200:] if not ok else "")

    code, out, _ = run(["tiers", "--mode", "chat"])
    ok = code == 0 and "模型" in out and "档位" in out and "极高" not in out
    record("第2步一次列模型+档位（chat 3 档）", ok, out[-200:] if not ok else "")

    code, out, _ = run(["tiers", "--mode", "work"])
    ok = code == 0 and "模型" in out and "档位" in out and "轻度" in out and "极高" in out and "最高" in out
    record("第2步一次列模型+档位（work 5 档）", ok, out[-200:] if not ok else "")

    code, out, _ = run(["tiers", "--mode", "work", "--json"])
    try:
        wdata = json.loads(out[out.index("{") : out.rindex("}") + 1])
        w_effs = [e["value"] for e in wdata["efforts"]]
        ok = (
            code == 0
            and len(wdata["models"]) == 10
            and all(m["value"].endswith("-wm") for m in wdata["models"])
            and w_effs == ["min", "standard", "extended", "xhigh", "max"]
        )
    except Exception:
        ok = False
    record("tiers --mode work --json 含 10 个 -wm 模型与 5 档真值", ok, out[-200:] if not ok else "")

    code, out, _ = run(["tiers", "--mode", "chat", "--models"])
    ok = code == 0 and "模型" in out and "档位" not in out
    record("--models 只列模型", ok, out[-200:] if not ok else "")

    code, out, _ = run(["tiers", "--mode", "chat", "--efforts"])
    ok = code == 0 and "档位" in out and "模型" not in out
    record("--efforts 只列档位", ok, out[-200:] if not ok else "")

    code, _, _ = run(["tiers", "--efforts"])
    record("--efforts 缺 --mode 应报错", code != 0, f"退出码 {code}")

    code, _, _ = run(["tiers", "--mode", "chat", "--models", "--efforts"])
    record("--models 与 --efforts 同时传应报错", code != 0, f"退出码 {code}")

    code, out, _ = run(["tiers", "--mode", "chat", "--json"])
    try:
        data = json.loads(out[out.index("{") : out.rindex("}") + 1])
        slugs = [m.get("value") for m in data["models"]]
        instants = [m.get("instant_value") for m in data["models"]]
        c_effs = [e["value"] for e in data["efforts"]]
        ok = (
            code == 0
            and slugs == ["gpt-6-thinking", "gpt-5-6-thinking"]
            and instants == ["gpt-6-instant", "gpt-5-6-instant"]
            and c_effs == ["none", "standard", "extended"]
        )
    except Exception:
        ok = False
    record("tiers --mode chat --json 含真实 slug 与 3 档", ok, out[-200:] if not ok else "")


# ─────────────────────────────────────────── 6. setup、--show 脱敏与 reference 隔离
def check_setup() -> None:
    backup = CONFIG.read_bytes() if CONFIG.exists() else None
    secret_tok = "sk-selfcheck-secret-99887766"
    ref = str(ROOT / "assets" / "imagegen.png")
    try:
        CONFIG.write_text(
            json.dumps({"api_url": "https://selfcheck.invalid/v1", "api_key": secret_tok}),
            encoding="utf-8",
        )

        code, out, _ = run([
            "setup", "--agent-model", "gpt-6-astra-wm", "--effort", "extended", "--web-mode", "work"
        ])
        record("setup 写入", code == 0 and "已写入" in out, out[-200:] if code != 0 else "")

        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        ok = (
            cfg.get("web_agent_model") == "gpt-6-astra-wm"
            and cfg.get("web_effort") == "extended"
            and cfg.get("web_mode") == "work"
        )
        record("setup 内容正确", ok, json.dumps(cfg, ensure_ascii=False))

        # setup --show --json 不泄露 api_key，且默认 web_ask_policy=confirm
        code, out, _ = run(["setup", "--show", "--json"])
        try:
            st = json.loads(out)
            ok = (
                code == 0
                and st["configured_web"] is True
                and st["has_api_key"] is True
                and st.get("web_ask_policy") == "confirm"
                and secret_tok not in out
            )
        except Exception:
            ok = False
        record("setup --show 安全检查且默认 web_ask_policy=confirm（不泄密）", ok, out[:200])

        # setup --ask-policy remember 可单独持久化并回读
        code, _, _ = run(["setup", "--ask-policy", "remember"])
        code2, out2, _ = run(["setup", "--show", "--json"])
        try:
            st2 = json.loads(out2)
            ok_pol = code == 0 and code2 == 0 and st2.get("web_ask_policy") == "remember"
        except Exception:
            ok_pol = False
        record("setup --ask-policy remember 持久化与回读", ok_pol, out2[:200])

        # setup 后命令自动沿用，且 --agent-model auto 可单次覆盖回 auto
        code, out, _ = run(["generate", "--prompt", "x", "--dry-run"])
        record("setup 后命令自动沿用", code == 0 and all(k in out for k in ("mssk_mode", "mssk_effort", "mssk_model")))

        code, out, _ = run(["generate", "--prompt", "x", "--effort", "min", "--agent-model", "auto", "--dry-run"])
        record(
            "命令行覆盖 config（含 --effort min 与 --agent-model auto）",
            code == 0 and '"mssk_effort": "min"' in out and "mssk_model" not in out,
            out[-260:],
        )

        # work 模式下旧写法 minimal / low 自动映射为 min，high/medium 自动映射为 extended/standard
        code, out, err = run(["generate", "--prompt", "x", "--web-mode", "work", "--effort", "minimal", "--dry-run"])
        record(
            "work + --effort minimal 自动归一为 min 并告警",
            code == 0 and '"mssk_effort": "min"' in out and "422" in err,
            (out + err)[-260:],
        )

        code, out, err = run([
            "generate", "--prompt", "x", "--web-mode", "chat",
            "--agent-model", "gpt-6-thinking", "--effort", "high", "--dry-run",
        ])
        record(
            "chat + --effort high 自动归一为 extended（防 422）",
            code == 0 and '"mssk_effort": "extended"' in out and "422" in err,
            (out + err)[-260:],
        )

        code, out, err = run([
            "generate", "--prompt", "x", "--web-mode", "chat",
            "--agent-model", "gpt-6-thinking", "--effort", "medium", "--dry-run",
        ])
        record(
            "chat + --effort medium 自动归一为 standard（防 422）",
            code == 0 and '"mssk_effort": "standard"' in out and "422" in err,
            (out + err)[-260:],
        )

        code, out, err = run([
            "generate", "--prompt", "x", "--web-mode", "work",
            "--agent-model", "gpt-6-astra-wm", "--effort", "low", "--dry-run",
        ])
        record(
            "work + --effort low 自动归一为 min（防 422）",
            code == 0 and '"mssk_effort": "min"' in out and "422" in err,
            (out + err)[-260:],
        )

        # setup chat + high 落盘为 extended，setup work + minimal 落盘为 min
        code, _, err = run(["setup", "--web-mode", "chat", "--agent-model", "gpt-6-thinking", "--effort", "high"])
        cfg_hi = json.loads(CONFIG.read_text(encoding="utf-8"))
        record(
            "setup chat + high 落盘为 extended",
            code == 0 and cfg_hi.get("web_effort") == "extended" and "extended" in err,
            json.dumps(cfg_hi, ensure_ascii=False),
        )

        code, _, err = run(["setup", "--web-mode", "work", "--agent-model", "gpt-6-astra-wm", "--effort", "minimal"])
        cfg_min = json.loads(CONFIG.read_text(encoding="utf-8"))
        record(
            "setup work + minimal 落盘为 min",
            code == 0 and cfg_min.get("web_effort") == "min" and "min" in err,
            json.dumps(cfg_min, ensure_ascii=False),
        )

        # setup 后跑 reference（未显式传网页参数）绝不误报 Warning
        code, _, err = run(["reference", "--image", ref, "--prompt", "x", "--dry-run"])
        record("setup 后 reference 不误报 config 默认值警告", code == 0 and "在这条路由上无效" not in err, err)

        # 显式传 --conv-key 给 reference 仍应警告
        code, _, err = run(["reference", "--image", ref, "--prompt", "x", "--conv-key", "k1", "--dry-run"])
        record("reference 显式传 --conv-key 正常警告", code == 0 and "--conv-key" in err, err)

        # setup --agent-model "" 可清除模型且保留 api_key
        code, _, _ = run(["setup", "--agent-model", ""])
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        record(
            "setup 可清除模型且保留 api_key",
            code == 0 and "web_agent_model" not in cfg and cfg.get("api_key") == secret_tok,
        )
    finally:
        if backup is not None:
            CONFIG.write_bytes(backup)
        elif CONFIG.exists():
            CONFIG.unlink()


# ─────────────────────────────────────────── 7. pipeline 上报（无全局态）
def check_pipeline() -> None:
    import image_gen

    def grab(payload, model, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            info = image_gen._report_pipeline(payload, model, **kw)
        return buf.getvalue(), info

    out, info = grab({"data": []}, "gpt-image-2")
    record("pipeline: 正常网页管线", "网页管线" in out and "surface=" in out and info["pipeline"] == "web", out.strip())

    out, info = grab({"data": [], "degraded": True, "degraded_reason": "webpipe 403"}, "gpt-image-2")
    record(
        "pipeline: 降级有醒目警告与风控诊断建议",
        "备用通道" in out and "403" in out and "webpipe_blocked" in out and info["pipeline"] == "degraded",
        out.strip(),
    )

    out_c, info_c = grab(
        {"data": [], "degraded": True, "degraded_reason": "prepare 没返回 conduit_token"},
        "gpt-image-2",
    )
    rec_c = image_gen._report_record(
        command="generate", model="gpt-image-2", prompt="p",
        artifacts=[], timing={"total": 1.0}, base_url="https://x", pipeline_info=info_c,
    )
    record(
        "pipeline: 降级附带 slug/effort 诊断且 report 含独立 degraded_reason",
        "webpipe_invalid_request" in out_c and rec_c.get("degraded_reason") == "prepare 没返回 conduit_token",
        f"{out_c.strip()} | {rec_c.get('degraded_reason')}",
    )

    out, info = grab({"data": []}, "codex-gpt-image-2")
    record("pipeline: 显式原生", "原生端点" in out and info["pipeline"] == "native", out.strip())

    out, info = grab({"data": []}, "gpt-image-2", web_mode="work", effort="extended")
    record("pipeline: 显示指定值", "work" in out and "extended" in out and "指定" in out, out.strip())

    out, info = grab({}, "gpt-6-astra", route="responses")
    record("pipeline: reference 路由输出 [pipeline]", "[pipeline]" in out and info["pipeline"] == "responses", out.strip())

    # 两次调用返回值相互独立（无全局 _LAST_PIPELINE 互相污染）
    _, i1 = grab({"data": [], "degraded": True, "degraded_reason": "x"}, "gpt-image-2")
    _, i2 = grab({"data": []}, "gpt-image-2")
    record("pipeline 无全局可变态串扰", i1["pipeline"] == "degraded" and i2["pipeline"] == "web" and not hasattr(image_gen, "_LAST_PIPELINE"))


# ─────────────────────────────────────────── 8. 错误分类、重试信号与脱敏
def check_errors() -> None:
    import image_gen
    import relay_errors

    cases = {
        "no image data": "no_image_data",
        "relay returned no images": "no_image_data",
        "moderation_blocked": "content_safety",
        "内容安全": "content_refusal",
        "server_is_overloaded": "upstream_overloaded",
        "system cpu overloaded": "relay_cpu",
        "token_revoked": "token_revoked",
        "no access to model": "no_model_access",
        "No available channel for model foo": "no_channel",
        "overall --deadline budget was used up": "deadline_exceeded",
        "request timed out after 150s": "timeout",
        "network error talking to https://x": "network_error",
        "Too many requests": "rate_limited",
        "Invalid value": "bad_request",
        "prepare 没返回 conduit_token": "webpipe_invalid_request",
        "请求体被上游拒绝（422 Invalid conversation body）：model=gpt-6-astra-wm": "webpipe_invalid_request",
        "webpipe 403 forbidden": "webpipe_blocked",
        "cloudflare cf_chl challenge": "webpipe_blocked",
        "invalid token": "token_invalid",
        "抱歉，我无法基于这张参考图": "model_refusal",
        "完全没见过的错误": "upstream_error",
    }
    bad = []
    for text, want in cases.items():
        got = relay_errors.classify_error(text)["code"]
        if got != want:
            bad.append(f"{text!r} -> {got}（期望 {want}）")
    record(f"错误分类 {len(cases)} 例", not bad, "; ".join(bad))

    # 网页管线参数/风控失败绝不标为可重试
    cls_inv = relay_errors.classify_error("prepare 没返回 conduit_token")
    cls_blk = relay_errors.classify_error("webpipe 403")
    record(
        "webpipe_invalid_request / webpipe_blocked 均标为 RETRYABLE=no",
        cls_inv["code"] == "webpipe_invalid_request"
        and cls_inv["retryable"] is False
        and cls_blk["code"] == "webpipe_blocked"
        and cls_blk["retryable"] is False,
        f"{cls_inv} / {cls_blk}",
    )

    # HTTP 401 裸状态码兜底
    record("HTTP 401 裸响应兜底为 token_invalid", relay_errors.classify_error("Unauthorized", http_status=401)["code"] == "token_invalid")

    # 503 system cpu overloaded 绝不自动重试且 RETRYABLE=no
    exc503 = image_gen._relay_error_from_http(503, b'{"error":{"message":"system cpu overloaded"}}')
    desc503 = image_gen._failure_dict(exc503)
    record(
        "503 relay_cpu 不自动重试且 RETRYABLE=no",
        exc503.retryable is False and desc503["code"] == "relay_cpu" and desc503["retryable"] is False,
        str(desc503),
    )

    # _parse_images 空 data 命中 no_image_data 专属 HINT（非 upstream_error 通用 HINT）
    try:
        image_gen._parse_images({"data": []}, creds=image_gen.RelayCredentials("https://x", "k", "t"), timeout=10)
        ok_nid = False
        detail_nid = "未抛异常"
    except image_gen.RelayError as exc_nid:
        d_nid = image_gen._failure_dict(exc_nid)
        ok_nid = d_nid["code"] == "no_image_data" and "流里没有图" in d_nid["hint"]
        detail_nid = str(d_nid)
    record("no_image_data 命中专属 HINT", ok_nid, detail_nid)

    # sexual/minors 正确翻译 + 人话后 _is_refusal_error 仍能触发（stage=output=True, stage=input=False）
    exc_out = image_gen._relay_error_from_http(
        400, '{"error":{"message":"内容安全系统拒绝（原生端点）：判定阶段=output，类别=sexual/minors；[网页管线先降级：违反内容政策]"}}'.encode("utf-8")
    )
    exc_in = image_gen._relay_error_from_http(
        400, '{"error":{"message":"内容安全系统拒绝（原生端点）：判定阶段=input，类别=sexual"}}'.encode("utf-8")
    )
    record(
        "sexual/minors 完整映射 + 保留网页先降级线索 + --retry-refusal 识别",
        "未成年人性化" in str(exc_out)
        and "网页管线先降级：违反内容政策" in str(exc_out)
        and image_gen._is_refusal_error(exc_out) is True
        and image_gen._is_refusal_error(exc_in) is False,
        str(exc_out),
    )

    # 令牌脱敏（sk-*、Bearer、自定义密钥）
    d_sec = relay_errors.describe_failure(
        "failed with Bearer sk-testsecret123456 and custom-secret-xyz",
        secrets=("custom-secret-xyz",),
    )
    record(
        "错误信息自动脱敏令牌",
        "sk-testsecret" not in d_sec["raw"]
        and "custom-secret-xyz" not in d_sec["raw"]
        and "[REDACTED]" in d_sec["raw"],
        d_sec["raw"],
    )


# ─────────────────────────────────────────── 9. 产物、预检、分层超时与续跑
def check_outputs_and_timeouts() -> None:
    import argparse
    import image_gen

    # n=2 返回 4 张时命名为 foo-1..foo-4（非 foo-1-1）
    outs = image_gen._build_output_paths("foo.png", "png", 2, None)
    exp = [p.name for p in image_gen._expand_output_paths(outs, 4)]
    record("多产物顺延命名（无 foo-1-1）", exp == ["foo-1.png", "foo-2.png", "foo-3.png", "foo-4.png"], str(exp))

    # GIF 落盘改扩展名 + sprite + report 反映真实路径 + _image_size_of 支持 GIF/PNG/JPEG/WebP
    gif_bytes = b"GIF89a" + struct.pack("<HH", 320, 240) + b"\x00\x00\x00;"
    png_bytes = (ROOT / "assets" / "imagegen.png").read_bytes()
    jpeg_hdr = b"\xff\xd8\xff\xc0\x00\x0b\x08" + struct.pack(">HH", 480, 640) + b"\x01\x01\x11\x00\xff\xd9"
    webp_vp8x = b"RIFF\x1e\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x00\x00\x00\x00" + (1023).to_bytes(3, "little") + (767).to_bytes(3, "little")
    sizes_ok = (
        image_gen._image_size_of(gif_bytes) == (320, 240)
        and image_gen._image_size_of(png_bytes) is not None
        and image_gen._image_size_of(jpeg_hdr) == (640, 480)
        and image_gen._image_size_of(webp_vp8x) == (1024, 768)
    )
    record("多格式像素头解析（PNG/GIF/JPEG/WebP）", sizes_ok)

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "anim.png"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            arts = image_gen._write_results(
                [image_gen.ImageResult(raw=gif_bytes, actual_size="320x240", revised_prompt=None, source="b64_json", mime="image/gif", sprite=png_bytes)],
                [target],
                force=True,
                output_format="png",
                output_compression=None,
                downscale_max_dim=None,
                downscale_suffix="-web",
            )
        rec = image_gen._report_record(
            command="generate", model="gpt-image-2", prompt="p",
            artifacts=arts, timing={"total": 1.0}, base_url="https://x",
        )
        record(
            "GIF 报告反映真实 .gif 路径与 sprite",
            rec["ok"] is True
            and rec["outputs"] == [str(Path(td) / "anim.gif")]
            and rec["sprites"] == [str(Path(td) / "anim-sprite.png")]
            and rec["pixels"] == [320, 240],
            str(rec),
        )

        # Fail-Fast 预检：文件已存在且无 --force 在发请求前即拦截
        existing = Path(td) / "exists.png"
        existing.write_bytes(b"old")
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                image_gen._precheck_outputs([existing], force=False, downscale_max_dim=None, downscale_suffix="-web")
            pre_ok = False
        except SystemExit:
            pre_ok = True
        record("写盘前 Fail-Fast 预检拦截已存在文件", pre_ok)

        # batch --skip-existing 跳过已有非空文件（零 HTTP 调用）
        jl = Path(td) / "skip.jsonl"
        jl.write_text('{"prompt":"a","out":"done.png"}\n', encoding="utf-8")
        (Path(td) / "done.png").write_bytes(b"already-rendered")
        rep = Path(td) / "rep.json"
        code, _, _ = run(
            ["generate-batch", "--input", str(jl), "--out-dir", td, "--skip-existing", "--report-json", str(rep)],
            env_extra={"IMAGE_RELAY_URL": "https://selfcheck.invalid/v1", "IMAGE_RELAY_KEY": "k"},
        )
        rep_data = json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else []
        record(
            "batch --skip-existing 跳过已完成任务",
            code == 0 and len(rep_data) == 1 and rep_data[0].get("skipped") is True,
            str(rep_data),
        )

    # 分层超时：normal vs heavy vs 显式覆盖（slow_after 随短 --timeout 自动缩放）
    t_norm = image_gen._resolve_timeouts(argparse.Namespace(timeout=None, deadline=None), command="generate", n=1, web_mode="chat", effort=None)
    t_heavy = image_gen._resolve_timeouts(argparse.Namespace(timeout=None, deadline=None), command="generate", n=4, web_mode="chat", effort=None)
    t_exp = image_gen._resolve_timeouts(argparse.Namespace(timeout=400.0, deadline=None), command="generate", n=1, web_mode="chat", effort=None)
    t_short_heavy = image_gen._resolve_timeouts(argparse.Namespace(timeout=150.0, deadline=200.0), command="generate", n=1, web_mode="work", effort="extended")
    record(
        "分层超时策略（常规 150/240，重任务 300/480，显式覆盖与 slow_after 缩放）",
        t_norm == (150.0, 240.0, 90.0)
        and t_heavy == (300.0, 480.0, 180.0)
        and t_exp[:2] == (400.0, 400.0)
        and t_short_heavy == (150.0, 200.0, 90.0),
        f"{t_norm} / {t_heavy} / {t_exp} / {t_short_heavy}",
    )

    # _run_with_refusal_retry 共享同一 deadline，过期立即报 deadline_exceeded
    try:
        image_gen._run_with_refusal_retry(
            "p", retry_refusal=3, label="[t]", deadline=time.monotonic() - 1.0,
            call_fn=lambda p, dl: None,
        )
        dl_ok = False
    except image_gen.RelayError as exc_dl:
        dl_ok = exc_dl.code == "deadline_exceeded"
    record("refusal 重试共享单一 deadline", dl_ok)


# ─────────────────────────────────────────── 10. 参数校验与缺失 api_url
def check_validation() -> None:
    bad_cases = {
        "非法 effort": ["generate", "--prompt", "x", "--effort", "nonexistent-tier", "--dry-run"],
        "非法 mode": ["tiers", "--mode", "bad"],
        "缺 prompt": ["generate", "--dry-run"],
        "work + effort=none 矛盾": ["generate", "--prompt", "x", "--web-mode", "work", "--effort", "none", "--dry-run"],
        "setup work + effort=none 矛盾": ["setup", "--web-mode", "work", "--effort", "none"],
    }
    for name, argv in bad_cases.items():
        code, _, _ = run(argv)
        record(f"拒绝 {name}", code != 0, f"退出码 {code}（应为非 0）")

    # 旧错误 slug 与 instant 搭 effort 提前告警
    env_ok = {"IMAGE_RELAY_URL": "https://selfcheck.invalid/v1", "IMAGE_RELAY_KEY": "k"}
    code, _, err = run(
        ["generate", "--prompt", "x", "--web-mode", "chat", "--agent-model", "gpt-5.6-sol-thinking", "--dry-run"],
        env_extra=env_ok,
    )
    record(
        "chat + 旧 slug gpt-5.6-sol-thinking 提前提示真实值 gpt-5-6-thinking",
        code == 0 and "gpt-5-6-thinking" in err,
        err.strip(),
    )
    code, _, err = run(
        ["generate", "--prompt", "x", "--web-mode", "chat", "--agent-model", "gpt-6-instant", "--effort", "extended", "--dry-run"],
        env_extra=env_ok,
    )
    record(
        "chat + -instant 搭 --effort extended 提前告警",
        code == 0 and "不支持 thinking_effort" in err,
        err.strip(),
    )

    # 有 key 但缺 api_url 应干净退出（不能抛 ValueError 堆栈）
    backup = CONFIG.read_bytes() if CONFIG.exists() else None
    try:
        if CONFIG.exists():
            CONFIG.unlink()
        code, out, err = run(
            ["generate", "--prompt", "x"],
            clear_relay_env=True,
            env_extra={"IMAGE_RELAY_KEY": "sk-only-key-no-url"},
        )
        record(
            "缺失 api_url 干净报错（无 Traceback）",
            code != 0 and "Traceback" not in (out + err) and "api_url" in err,
            err.strip(),
        )
    finally:
        if backup is not None:
            CONFIG.write_bytes(backup)


def main() -> int:
    verbose = "-v" in sys.argv
    checks = [
        check_code_integrity,
        check_dry_run,
        check_presets,
        check_style_matrix,
        check_tiers,
        check_setup,
        check_pipeline,
        check_errors,
        check_outputs_and_timeouts,
        check_validation,
    ]
    for fn in checks:
        try:
            fn()
        except Exception as exc:
            record(f"{fn.__name__} 异常", False, f"{type(exc).__name__}: {exc}")

    width = max((len(n) for n, _, _ in RESULTS), default=10)
    failed = 0
    for name, ok, detail in RESULTS:
        print(
            ("PASS  " if ok else "FAIL  ")
            + name.ljust(width)
            + ("  " + detail if (detail and (verbose or not ok)) else "")
        )
        if not ok:
            failed += 1
    print()
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} 项通过" + ("" if failed == 0 else f"，{failed} 项失败"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
