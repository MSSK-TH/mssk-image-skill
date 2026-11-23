#!/usr/bin/env python3
"""selfcheck.py —— 零成本自检（不发任何出图请求、不消耗额度）。

用法：
    python scripts/selfcheck.py            # 全部检查
    python scripts/selfcheck.py -v         # 打印每项细节

它做的事：
  1. 所有子命令的 --dry-run 能否跑通
  2. 4 条路由 × 每种契约，`style` 里的 mssk_* 是否都注入了（用 spy 拦截发送函数）
  3. tiers 的行为（先模式、再按模式列 3/4 档）
  4. setup 能否写入并在后续命令里生效（会备份并恢复 config.json）
  5. pipeline 上报的三种情况（网页 / 降级 / 原生）
  6. 错误分类（relay_errors 的每个 code 都能命中）
  7. 非法参数是否被拒绝

检查完会打印 PASS/FAIL 汇总，退出码 0 = 全绿。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = HERE / "image_gen.py"
CONFIG = ROOT / "config.json"

sys.path.insert(0, str(HERE))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))


def run(args: list[str], *, stdin: str | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(CLI)] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=stdin,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ─────────────────────────────────────────── 1. dry-run 冒烟
def check_dry_run() -> None:
    os.environ.setdefault("IMAGE_RELAY_KEY", "selfcheck-dummy")
    with tempfile.TemporaryDirectory() as td:
        ref = ROOT / "assets" / "imagegen.png"
        cases = {
            "generate --dry-run": ["generate", "--prompt", "x", "--dry-run"],
            "generate 全参数 --dry-run": [
                "generate", "--prompt", "x", "--n", "2", "--background", "transparent",
                "--agent-model", "gpt-6-thinking", "--effort", "high", "--web-mode", "chat",
                "--preset", "16:9@2k", "--dry-run",
            ],
            "edit --dry-run": ["edit", "--image", str(ref), "--prompt", "x", "--dry-run"],
            "reference --dry-run": ["reference", "--image", str(ref), "--prompt", "x", "--dry-run"],
        }
        jobs = Path(td) / "jobs.jsonl"
        jobs.write_text('{"prompt":"a"}\n', encoding="utf-8")
        cases["batch --dry-run"] = [
            "generate-batch", "--input", str(jobs), "--out-dir", td, "--dry-run",
        ]
        for name, argv in cases.items():
            code, out = run(argv)
            ok = "endpoint" in out or "wire_payload" in out or "jobs" in out
            record(f"dry-run: {name}", ok, "" if ok else out[-300:])

        # prompt-file 带 BOM 时不能把 \ufeff 漏进提示词
        bom = Path(td) / "bom.txt"
        bom.write_bytes(b"\xef\xbb\xbf" + "画一只猫".encode("utf-8"))
        code, out = run(["generate", "--prompt-file", str(bom), "--dry-run"])
        record("prompt-file 能去掉 BOM", "\\ufeff" not in out and code == 0, out[-200:])


# ─────────────────────────────────────────── 2. style 注入矩阵
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
        captured.append((
            "multipart", "edit",
            {k.replace("web_", "mssk_", 1): v for k, v in fields.items() if k.startswith("web_")},
        ))
        raise SystemExit(0)

    image_gen._post_json_with_retries = fake_json
    image_gen._post_edit_with_retries = fake_edit

    common = ["--web-mode", "work", "--effort", "extended", "--agent-model", "gpt-6-thinking",
              "--conv-key", "selfcheck"]
    ref = str(ROOT / "assets" / "imagegen.png")
    with tempfile.TemporaryDirectory() as td:
        jobs = Path(td) / "j.jsonl"
        jobs.write_text('{"prompt":"a"}\n', encoding="utf-8")
        routes = {
            "generate": ["generate", "--prompt", "x", "--out", str(Path(td) / "a.png")] + common,
            "generate(native)": ["generate", "--prompt", "x", "--model", "codex-gpt-image-2",
                                 "--out", str(Path(td) / "b.png")] + common,
            "edit": ["edit", "--image", ref, "--prompt", "x", "--out", str(Path(td) / "c.png")] + common,
            "batch": ["generate-batch", "--input", str(jobs), "--out-dir", td] + common,
        }
        old_argv = sys.argv
        for name, argv in routes.items():
            captured.clear()
            sys.argv = ["image_gen.py"] + argv
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    image_gen.main()
                except (SystemExit, Exception):
                    pass
            keys = set()
            for _kind, _label, style in captured:
                keys |= set(style.keys())
            need = {"mssk_mode", "mssk_effort", "mssk_model"}
            ok = need <= keys
            record(f"style 注入: {name}", ok, "" if ok else f"缺少 {need - keys}")

        # batch 不应带 mssk_conv
        captured.clear()
        sys.argv = ["image_gen.py"] + routes["batch"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                image_gen.main()
            except (SystemExit, Exception):
                pass
        conv_leaked = any("mssk_conv" in style for _k, _l, style in captured)
        record("batch 不带 mssk_conv", not conv_leaked, "batch 里出现了续接钥匙" if conv_leaked else "")

        sys.argv = old_argv

    image_gen._post_json_with_retries = real_json
    image_gen._post_edit_with_retries = real_edit


# ─────────────────────────────────────────── 3. tiers
def check_tiers() -> None:
    code, out = run(["tiers"])
    ok = code == 0 and "聊天" in out and "工作" in out and "模型" not in out
    record("tiers 第一步只列模式", ok, out[-200:] if not ok else "")

    code, out = run(["tiers", "--mode", "chat"])
    ok = code == 0 and "模型" in out and "档位" in out and "极高" not in out
    record("第2步一次列模型+档位（chat 3 档）", ok, out[-200:] if not ok else "")

    code, out = run(["tiers", "--mode", "work"])
    ok = code == 0 and "模型" in out and "档位" in out and "极高" in out and "max" not in out
    record("第2步一次列模型+档位（work 4 档，无 max）", ok, out[-200:] if not ok else "")

    code, out = run(["tiers", "--mode", "chat", "--efforts"])
    ok = code == 0 and "档位" in out and "模型" not in out
    record("--efforts 只列档位", ok, out[-200:] if not ok else "")

    code, out = run(["tiers", "--efforts"])
    record("--efforts 缺 --mode 应报错", code != 0, f"退出码 {code}")

    code, out = run(["tiers", "--mode", "chat", "--json"])
    try:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        ok = code == 0 and len(data["models"]) == 3 and len(data["efforts"]) == 3
    except Exception:
        ok = False
    record("tiers --json 结构正确", ok, out[-200:] if not ok else "")


# ─────────────────────────────────────────── 4. setup 持久化
def check_setup() -> None:
    backup = None
    if CONFIG.exists():
        backup = CONFIG.read_bytes()
    try:
        code, out = run(["setup", "--agent-model", "gpt-6-thinking", "--effort", "high",
                         "--web-mode", "work"])
        record("setup 写入", code == 0 and "已写入" in out, out[-200:] if code != 0 else "")

        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        ok = (cfg.get("web_agent_model") == "gpt-6-thinking"
              and cfg.get("web_effort") == "high" and cfg.get("web_mode") == "work")
        record("setup 内容正确", ok, json.dumps(cfg, ensure_ascii=False))

        code, out = run(["generate", "--prompt", "x", "--dry-run"])
        ok = all(k in out for k in ("mssk_mode", "mssk_effort", "mssk_model"))
        record("setup 后命令自动沿用", ok, out[-300:] if not ok else "")

        code, out = run(["generate", "--prompt", "x", "--effort", "low", "--dry-run"])
        ok = '"mssk_effort": "low"' in out
        record("命令行覆盖 config", ok, out[-300:] if not ok else "")

        code, out = run(["setup", "--agent-model", ""])
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        record("setup 可清除模型", code == 0 and "web_agent_model" not in cfg, json.dumps(cfg, ensure_ascii=False))
    finally:
        if backup is not None:
            CONFIG.write_bytes(backup)
        elif CONFIG.exists():
            CONFIG.unlink()


# ─────────────────────────────────────────── 5. pipeline 上报
def check_pipeline() -> None:
    import image_gen

    def grab(payload, model, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            image_gen._report_pipeline(payload, model, **kw)
        return buf.getvalue()

    out = grab({"data": []}, "gpt-image-2")
    record("pipeline: 正常网页管线", "网页管线" in out and "surface=" in out, out.strip())

    out = grab({"data": [], "degraded": True, "degraded_reason": "webpipe 403"}, "gpt-image-2")
    record("pipeline: 降级有醒目警告", "备用通道" in out and "403" in out, out.strip())

    out = grab({"data": []}, "codex-gpt-image-2")
    record("pipeline: 显式原生", "原生端点" in out, out.strip())

    out = grab({"data": []}, "gpt-image-2", web_mode="work", effort="extended")
    record("pipeline: 显示指定值", "work" in out and "extended" in out and "指定" in out, out.strip())


# ─────────────────────────────────────────── 6. 错误分类
def check_errors() -> None:
    import relay_errors

    cases = {
        "no image data": "no_image_data",
        "moderation_blocked": "content_safety",
        "内容安全": "content_refusal",
        "server_is_overloaded": "upstream_overloaded",
        "system cpu overloaded": "relay_cpu",
        "token_revoked": "token_revoked",
        "no access to model": "no_model_access",
        "Invalid value": "bad_request",
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

    d = relay_errors.describe_failure("no image data", http_status=200)
    ok = d["code"] == "no_image_data" and d["retryable"] is True and d["zh"] and d["hint"]
    record("describe_failure 结构完整", ok, json.dumps(d, ensure_ascii=False)[:200])


# ─────────────────────────────────────────── 7. 参数校验
def check_validation() -> None:
    bad_cases = {
        "非法 effort": ["generate", "--prompt", "x", "--effort", "nonexistent-tier", "--dry-run"],
        "非法 mode": ["tiers", "--mode", "bad"],
        "缺 prompt": ["generate", "--dry-run"],
    }
    for name, argv in bad_cases.items():
        code, _out = run(argv)
        record(f"拒绝 {name}", code != 0, f"退出码 {code}（应为非 0）")


def main() -> int:
    verbose = "-v" in sys.argv
    checks = [check_dry_run, check_style_matrix, check_tiers, check_setup,
              check_pipeline, check_errors, check_validation]
    for fn in checks:
        try:
            fn()
        except Exception as exc:  # 单项炸掉不影响其它
            record(f"{fn.__name__} 异常", False, f"{type(exc).__name__}: {exc}")

    width = max((len(n) for n, _, _ in RESULTS), default=10)
    failed = 0
    for name, ok, detail in RESULTS:
        print(("PASS  " if ok else "FAIL  ") + name.ljust(width) + ("  " + detail if (detail and (verbose or not ok)) else ""))
        if not ok:
            failed += 1
    print()
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} 项通过" + ("" if failed == 0 else f"，{failed} 项失败"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
