---
name: "imagegen-relay"
description: "Generate or edit raster images through the MSSK relay (gpt-image-2 / codex-gpt-image-2) when the task benefits from AI-created bitmap visuals such as photos, illustrations, textures, sprites, mockups, posters, character sheets, or transparent-background cutouts. This is the image path on this Codex installation: the built-in image_gen tool does not work with a custom/relay model provider. Use whenever the user asks for a bitmap image -- 生图, 画图, 生成图片, 配图, 出图, 改图, 修图 -- and do not substitute SVG/HTML/canvas/PIL/matplotlib when the user wants a bitmap. Do not use when the task is better handled by editing existing SVG/vector/code-native assets, extending an established icon or logo system, or building the visual directly in HTML/CSS/canvas. Images only, never video."
---

# Image Generation Relay (`imagegen-relay`)

## Role boundary: TWO distinct pipelines (read first!)

Always invoke the bundled launcher (`%USERPROFILE%\.codex\skills\imagegen-relay\bin\run.cmd` on Windows, `bin/run.sh` on macOS/Linux). **Do not** write custom HTTP wrappers, install SDKs, or edit `scripts/image_gen.py`.

| Pipeline | Trigger | Remote chat brain? | Your exact role | Prompt handling |
| --- | --- | --- | --- | --- |
| **Web pipeline (default)** | `--model gpt-image-2` (default) or `reference` | **YES** (ChatGPT GPT-6 / 6.2 on the other side) | **Pure passthrough launcher** (same as typing into the web UI) | **Send the user's prompt verbatim** (CLI defaults to `--no-augment`). Strip only control directives (mode/model/ratio/save path). **Do NOT** rewrite, expand, translate, add style/negative fluff, or wrap into a 14-line schema — prompt understanding & refinement belong to the remote web agent. |
| **Native pipeline** | `--model codex-gpt-image-2` (only when user explicitly requests native/codex endpoint) | **NO** (bare image model, no chat brain) | **Prompt structurer + launcher** | CLI auto-enables `--augment`. If the user's prompt is a brief/vague phrase, lightly structure it using the CLI's augmentation flags (`--use-case`, `--style`, `--composition`, `--lighting`, `--constraints`, etc.) and consult `references/prompting.md` only if needed. If the user already gave a detailed prompt, keep it intact. |

## Speed & anti-bloat rules (match web-UI speed)

1. **NEVER run `tiers` to look up models/efforts** — use the inline table below directly (saves ~25s of wasted CLI round-trips).
2. **NEVER run `--dry-run`** before a real render unless the user explicitly asks to preview a payload.
3. **NEVER read `references/prompting.md` or `references/sample-prompts.md`** on web-pipeline runs unless the user explicitly asks you to help write/optimize a prompt.
4. **ONE shell turn to launch (never split prompt-writing and execution)** — for short/single-line prompts, pass `--prompt "..."` directly; for multi-line or quote-heavy prompts on Windows, write the UTF-8 file and invoke `run.cmd` **in the exact same shell command** (PowerShell `@' ... '@` here-string + `run.cmd --prompt-file`). **NEVER** spend a separate `apply_patch` / file-write turn before running the launcher (saves ~15–25s).
5. **Wait in ONE call (`yield_time_ms: 120000`)** — when calling `exec_command` for `generate`/`edit`/`reference`, always pass `yield_time_ms: 120000` (`180000` for `work`/`extended+`/`n>1`) so a normal 25–45s render returns in one shot without getting cut off at 30s and burning extra polling turns (saves ~5–16s).
6. **ZERO post-render verification turn** — the CLI atomically writes the image and prints `Wrote <abs_path> (<W>x<H>, <size>KB)` on exit code `0`. Once `[pipeline]` + `Wrote <abs_path> (<W>x<H>, <size>KB)` appear, deliver immediately. **NEVER** run an extra shell command (`Test-Path`, `Resolve-Path`, `[System.Drawing.Image]`, `ls`) to re-check existence/path/size, and **NEVER** run `view_image` unless the user asks you to review or edit the result (saves ~14–18s).
7. **Do NOT interrogate for aspect ratio** when the user omitted it — pass `--size auto` (exact same default as the web UI). Only infer `--size`/`--preset` when the user mentions a ratio/format.
8. **Announce in 1 short line before running** (e.g. `正在发往网页管线（聊天 · GPT-6 · 高），预计 25～45 秒…`), run **one** launcher call, wait for completion (never spawn parallel duplicates while `[waiting]` heartbeats print), then return the image link/path + 1 line of metadata (`pipeline / actual_size / total_s`; disclose `degraded` + reason if fallback happened).

## Inline web-pipeline catalog (no `tiers` call needed)

- **`chat`（聊天，3 档）** — 档位：`即时`=`none` · `中`=`standard` · `高`=`extended`
  - `GPT-6`：思考档 `gpt-6-thinking` / 即时档 `gpt-6-instant`
  - `GPT-5.6 Sol`：思考档 `gpt-5-6-thinking` / 即时档 `gpt-5-6-instant`（注意连字符 `5-6`，无 `-sol`）
- **`work`（工作，5 档，无即时档）** — 档位：`轻度`=`min` · `中`=`standard` · `高`=`extended` · `极高`=`xhigh` · `最高`=`max`
  - 10 个 `-wm` 模型（新到旧）：`GPT-6.2 Astra` (`gpt-6.2-astra-wm`) · `GPT-6.2 Sol` (`gpt-6.2-sol-wm`) · `GPT-6.1 Sol` (`gpt-6.1-sol-wm`) · `GPT-6.1 Terra` (`gpt-6.1-terra-wm`) · `GPT-6 Astra` (`gpt-6-astra-wm`) · `GPT-6 Sol` (`gpt-6-sol-wm`) · `GPT-6 Luna` (`gpt-6-luna-wm`) · `GPT-5.6 Sol` (`gpt-5.6-sol-wm`) · `GPT-5.6 Terra` (`gpt-5.6-terra-wm`) · `GPT-5.6 Luna` (`gpt-5.6-luna-wm`)

> `--effort` must use the exact English slug above (`standard` / `extended` / `min` / `xhigh` / `max` / `none`), never `high`/`medium` (CLI & shim also auto-normalize as safety nets).

## Per-conversation model selection (fast, separate single-choice)

Before the **first** web-pipeline image in a conversation (skip entirely for `codex-gpt-image-2` or `reference`), run `setup --show --json` **once** (**never read `config.json` directly**):

- **User already named mode/model/tier in their prompt** → skip asking; pass their choice via CLI flags for this conversation only (do not overwrite `config.json`).
- **`configured_web` is `true` & `web_ask_policy` is `"confirm"` (default)** → ask **1 single line**:
  `本次新对话生图：1) 沿用默认（<模式> · <模型> · <档位>）  2) 换一个`
  - `1` / 沿用 / 随便 → run immediately with saved defaults.
  - `2` / 换 → ask **separate single-choice questions** (mode first → then **model** and **tier** as **two separate single-choice questions**, NEVER combined into `(1,2)` / `(1,3)` pairs). Apply via **all three** CLI flags (`--web-mode`, `--agent-model`, `--effort`) on every call in this conversation. Do **not** call `setup` unless user explicitly says "设为默认".
- **`configured_web` is `false`** → run the same separate single-choice flow (mode → separate model + separate tier), save once via `setup`, then generate. ("你随便" → `chat / gpt-6-thinking / extended`.)
- **`web_ask_policy` is `"remember"`** → skip asking; use saved defaults directly.
- Once confirmed/chosen in a conversation, **never ask again** on later turns of the same conversation.

## Subcommand & parameter routing

| User intent | Subcommand & key flags |
| --- | --- |
| New image OR **follow-up edit of an image just generated in this conversation** (web pipeline) | `generate` (`POST /v1/images/generations`) — **do NOT pass `--new-conv` and do NOT re-upload `--image`**; the remote web thread already has the image and edits in place, just like ChatGPT Web UI |
| Edit a **local/external image file** or attach a new reference image (keeping aspect ratio; or any edit on stateless `codex-gpt-image-2`) | `edit --image <path>` (repeat `--image` for multiple; stays in the same web conversation & preserves chosen model/effort) |
| Reference image + **must change aspect ratio** | `reference --image <path>` (`/v1/responses`, ignores `setup` web defaults; use `--reasoning-effort`) |
| Many distinct prompts | `generate-batch --input <jsonl> --out-dir <dir> --concurrency 2 [--skip-existing]` |
| Multi-line / quoted prompt on Windows | Write UTF-8 via PowerShell `@' ... '@` and run `--prompt-file` **in the same shell command** (short prompts: pass `--prompt "..."` directly; never spend a separate file-write turn) |
| Save path | User's path if named; else workspace `output/imagegen/<name>.png` (never OS temp; never overwrite without `--force`, version as `-v2.png`) |
| Ratio / exact size (only if user mentions) | `16:9` → `--size 1672x941` (`--preset 16:9@2k`); `9:16` → `941x1672`; `1:1` → `1254x1254`; `3:2` → `1536x1024`; `4:3` → `1448x1086`; exact pixels → `--exact-size WxH` (`@4k` is locally upscaled — disclose if used) |
| Transparent / GIF / exact N / reset thread | `--background transparent` · `--gif-speed 0.25..2` · `--n N --strict-n` · `--new-conv` (**only** when user explicitly asks to start a new thread or context drifted badly) · parallel task `--conv-key <id>` |

> **Web-UI conversation continuity (1 Codex chat = 1 Web chat)**: Continuity is per directory (`<cwd>/.imagegen/conv-<hash>`).
> - **Stay in the SAME web conversation across turns of the same Codex chat** — do **NOT** pass `--new-conv` just because the user asks for a different subject (e.g. puppy → kitten). Only pass `--new-conv` when the user explicitly requests a fresh conversation/reset or the thread drifted irreparably.
> - **Editing the image just generated in this conversation (web pipeline `gpt-image-2`)**: call **`generate`** with the user's edit instruction verbatim (no `--image` re-upload, no `--new-conv`) so the remote web thread modifies it in place. Use `edit --image <path>` only when attaching a local/external image file or when running stateless `codex-gpt-image-2`.

## Minimal execution template (Windows PowerShell — single `exec_command`, `yield_time_ms: 120000`)

```powershell
# Short / single-line prompt: pass --prompt directly (ZERO extra file-write turn)
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "用户的原始单行提示词" `
  --out "output/imagegen/result.png"

# Multi-line / quoted prompt: write UTF-8 file + invoke launcher in ONE command
New-Item -ItemType Directory -Force -Path "tmp/imagegen" | Out-Null
@'
用户的原始多行提示词（原样保留）
'@ | Set-Content -Encoding UTF8 "tmp/imagegen/prompt.txt"
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" edit `
  --image "input/ref.png" `
  --prompt-file "tmp/imagegen/prompt.txt" `
  --out "output/imagegen/result-edited.png"
```

## Failure handling (`CODE=` / `RETRYABLE=`)

Decide strictly by `CODE=` / `RETRYABLE=` on stderr (never retry `RETRYABLE=no`; retry `RETRYABLE=yes` **at most once**):

| `CODE` | `RETRYABLE` | Action |
| --- | --- | --- |
| `no_image_data` / `timeout` / `rate_limited` / `network_error` / `upstream_error` | yes | Retry once (on repeated timeout, try `--new-conv` or `--model codex-gpt-image-2`) |
| `content_refusal` | yes | Stochastic output-stage refusal: report to user; if they want a retry, suggest a conservative reference or compact neutral phrasing (see `references/safety-refusals.md`) |
| `bad_request` | yes | Unsupported parameter — adjust and retry once |
| `content_safety` / `model_refusal` | no | Upstream safety/model refusal — report verbatim to user, do not loop-retry |
| `webpipe_invalid_request` / `webpipe_blocked` | no | Slug/effort mismatch (check inline table) or 403 risk control (wait or switch to `--model codex-gpt-image-2`) |
| `deadline_exceeded` / `upstream_overloaded` / `relay_cpu` | no | Report reason; wait or raise `--deadline` / switch to `codex-gpt-image-2` |
| `token_invalid` / `no_model_access` / `no_channel` / `token_revoked` | no | User updates `config.json` locally (never print tokens) or contacts relay owner |

## References (load ONLY on demand)
- `references/cli.md` — full CLI flags, masks, presets, batch JSONL format
- `references/relay-api.md` — wire contracts (`style.mssk_*`, multipart, GIF schema)
- `references/safety-refusals.md` — content-safety refusal troubleshooting (only when refused)
- `references/prompting.md` & `references/sample-prompts.md` — **native pipeline (`codex-gpt-image-2`) or explicit prompt-writing requests only**
