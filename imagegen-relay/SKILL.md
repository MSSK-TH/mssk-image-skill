---
name: "imagegen-relay"
description: "Generate or edit raster images through the MSSK relay (gpt-image-2 / codex-gpt-image-2) when the task benefits from AI-created bitmap visuals such as photos, illustrations, textures, sprites, mockups, posters, character sheets, or transparent-background cutouts. This is the image path on this Codex installation: the built-in image_gen tool does not work with a custom/relay model provider. Use whenever the user asks for a bitmap image -- 生图, 画图, 生成图片, 配图, 出图, 改图, 修图 -- and do not substitute SVG/HTML/canvas/PIL/matplotlib when the user wants a bitmap. Do not use when the task is better handled by editing existing SVG/vector/code-native assets, extending an established icon or logo system, or building the visual directly in HTML/CSS/canvas. Images only, never video."
---

# Image Generation Skill (Relay)

Generates or edits images for the current project (for example website assets, game assets, UI mockups, product mockups, wireframes, logo design, photorealistic images, character sheets, or infographics).

This skill is the relay counterpart of Codex's built-in `imagegen` skill. The prompt craft, use-case taxonomy, CLI surface, and iteration discipline stay aligned with official `imagegen`; what differs is the transport (MSSK relay instead of OpenAI built-in `image_gen` / `api.openai.com`), documented below and winning wherever the two disagree.

## Relay modes and rules

With a custom/relay provider, Codex does not expose the built-in `image_gen` tool. All image requests go through the bundled launcher (`bin/run.cmd` on Windows, `bin/run.sh` on macOS/Linux), which injects `config.json` credentials and exposes both relay routes:

| Route | Subcommand(s) | Wire call | Role |
| --- | --- | --- | --- |
| **Images route** (primary) | `generate`, `edit`, `generate-batch` | `POST /v1/images/generations`, `POST /v1/images/edits` | Default path for text-to-image, edits, batches, and ratio-locked reference work. Carries full web-session pipeline controls (`style.mssk_*`), conversation continuity, and `degraded` reporting. |
| **Responses tool route** | `reference` | `POST /v1/responses` + `tools:[{"type":"image_generation"}]` | Chat-model-driven reference generation. Use when you must **change aspect ratio** from the reference image or want a chat model to freely reinterpret pure style/composition references. Ignores `style.mssk_*` / `setup` defaults (use `--reasoning-effort`). |

Two image model slugs on `generate` / `edit` / `generate-batch` (any other slug fails with `no_channel`):
- `gpt-image-2` (**default**): web session pipeline first (~25–45 s, higher pass rate), auto-falls back to the native subscription endpoint (`degraded: true` + `degraded_reason`).
- `codex-gpt-image-2`: native subscription endpoint only (~15–30 s, stricter safety gate, no fallback; ignores `--web-mode`/`--effort`/`--agent-model`).

Rules:
- Always call the launcher (`bin/run.cmd` / `bin/run.sh`); never build one-off HTTP wrappers or add an SDK, and never modify `scripts/image_gen.py`.
- `--dry-run` is free, requires no network/token, makes zero disk writes, and prints the exact wire payload.
- **Save-path policy** (aligned with official `imagegen`):
  1. If the user names a destination, write there.
  2. If the image is meant for the current project, save inside the workspace (prefer `output/imagegen/`), never in OS temp.
  3. Do not overwrite an existing asset unless the user explicitly asked for replacement (`--force`); otherwise create a sibling versioned filename such as `hero-v2.png` or `item-icon-edited.png`.

### First-run web setup (two steps, then remember)

Before the first web-pipeline image, check status via `setup --show --json` (**never read `config.json` directly**, so `api_key` never enters chat). Only when `configured_web` is `false`, ask in **strictly two turns** (never dump both steps at once, and never split model & tier into three turns):

1. **Step 1 — 模式** (`tiers`): ask `模式：1)聊天  2)工作` (must come first because the tier list depends on surface).
2. **Step 2 — 模型 + 档位** (`tiers --mode <chat|work>`): ask model and tier **together in one message**:
   - 聊天（`chat`，3 档）：**2 个模型家族**（实测上游 `/backend-api/models`）—— `1) GPT-6`（思考档 `gpt-6-thinking` / 即时档 `gpt-6-instant`）`2) GPT-5.6 Sol`（思考档 `gpt-5-6-thinking` / 即时档 `gpt-5-6-instant`，注意连字符 `5-6` 无 `-sol`）/
     `档位：1)即时(none)  2)中(standard)  3)高(extended)`
   - 工作（`work`，5 档）：**10 个 `-wm` 模型**（6.2 至 5.6）——
     `档位：1)轻度(min)  2)中(standard)  3)高(extended)  4)极高(xhigh)  5)最高(max)`（无即时档；
     `work + --effort none` 会被拒；旧写法 `minimal` 会自动映射为 `min` 并提示）
   - **两种 surface 的模型范围与 slug 命名不同，不要混用**：chat 走 `versions[]` 的 `-instant`/`-thinking`（`5-6` 用连字符），work 走 `-wm` 家族（全部 10 个模型共享 5 档 `min/standard/extended/xhigh/max`）。不确定就传 `--agent-model auto`。
3. Map names via `tiers --mode <choice> --json` and save once: `setup --web-mode <m> --agent-model <slug> --effort <e>`（chat 选 `即时(none)` 时用对应 `instant_value`）. If user says "你随便": use `聊天 + GPT-6 + 高` (`chat` / `gpt-6-thinking` / `extended`) and state what you picked. After `setup`, **never ask again**; CLI flags override per run (`--agent-model auto` overrides back to auto).

### Operating discipline (4 rules that prevent wasted renders)

1. **New topic → `--new-conv`; parallel tasks in one folder → distinct `--conv-key <task>`.** Continuity is per project directory (`<cwd>/.imagegen/conv-<hash>`), so an unrelated topic in the same folder would inherit the previous thread without `--new-conv`.
2. **Multi-line prompts on Windows → `--prompt-file` (UTF-8).** PowerShell quoting mangles newlines/quotes; the CLI reads `--prompt-file` with `utf-8-sig` (BOM-safe; mutually exclusive with `--prompt`).
3. **Subject drifted completely off-brief → `--new-conv`, not `edit`.** Continuing a drifted thread anchors the model to the wrong subject; start fresh instead of polishing an unrelated image.
4. **Watch the heartbeat, then verify 3 items before reporting success.** `[waiting]` every 20 s (`[slow]` if stalled) means the process is alive — never spawn a duplicate parallel run. Only report success when all three align: a `[pipeline]` line (disclose `degraded` + reason if fallback happened), a `Wrote <path>` line, and the file existing on disk.

Shared prompt guidance lives in `references/prompting.md` and `references/sample-prompts.md`. Relay-specific docs: `references/cli.md`, `references/relay-api.md`, `references/safety-refusals.md`, `references/codex-network.md`.

## When to use
- Generate a new image (concept art, product shot, cover, website hero)
- Generate a new image using one or more reference images for style, composition, or mood
- Edit an existing image (inpainting, lighting or weather transformations, background replacement, object removal, compositing, transparent background)
- Produce many assets or variants for one task

## When not to use
- Extending or matching an existing SVG/vector icon set, logo system, or illustration library inside the repo
- Creating simple shapes, diagrams, wireframes, or icons that are better produced directly in SVG, HTML/CSS, or canvas
- Making a small project-local asset edit when the source file already exists in an editable native format
- Any task where the user clearly wants deterministic code-native output instead of a generated bitmap

## Decision tree

Think about two separate questions:

1. **Intent:** is this a new image, an edit of an existing image, or a reference-driven image?
2. **Execution strategy:** is this one asset or many assets/variants?

Intent → Relay subcommand:
- Modify an existing image while preserving parts of it → **`edit`**.
- Reference image(s) for identity/character-in-scene/compositing (keeping input aspect ratio) → **default to `edit`** (stays on the web session pipeline with full `surface`/`effort`/`mssk_conv` support, faster, verbatim prompt).
- Reference image(s) where you need to **break out of the input image's aspect ratio** (e.g. vertical reference → 16:9 poster) or want a chat model to freely reinterpret pure style/mood references → **`reference`** (note: bypasses `style.mssk_*` / `setup` defaults; use `--reasoning-effort`).
- No images supplied → **`generate`**.

Execution strategy:
- Many distinct assets → repeated `generate` calls or `generate-batch` (`--concurrency 2`, `--out-dir` required; `--skip-existing` to resume).
- Do not use `--n` as a substitute for separate prompts: `--n` is for variants of one prompt.
- Assume the user wants a new image unless they clearly ask to change an existing one.

### Infer parameters; ask only what cannot be inferred

Derive flags from user intent and state what you picked in one line:

| User cue | Flag / action |
| --- | --- |
| "transparent background" / "cutout" / "透明底" | `--background transparent` (prompt-emulated PNG alpha) |
| "poster" / "banner" / "wide" / "16:9" | `--preset 16:9@2k` (or `--size 1672x941`) |
| "phone wallpaper" / "vertical" / "9:16" | `--preset 9:16@2k` (or `--size 941x1672`) |
| "avatar" / "icon" / "square" | `--size 1254x1254` (or `--preset 1:1@2k`) |
| "4K" / "high-res" | `--preset <ratio>@4k` (state clearly: locally upscaled) |
| "more detail" / "legible text" / "refine" | `--effort extended` |
| "quick idea" / "rough" | `--effort none` (chat) or `--web-mode work --effort min` |
| "card" / "site asset" / "will iterate" | `--web-mode work` |
| "give me 4 options" (text-only) | `--n 4 --strict-n` (exact 4) or `--n 4` (allows extra free variants) |
| "same character" / "put this in X" / "改图" | `edit --image <path>` (or `reference` if changing aspect ratio) |
| "animated" / "loop" / "gif" / "动图" | describe frame sequence + loop; `--gif-speed 1` (or `1.5` for faster playback) |

Ask only when un-derivable and costly to rework: (1) zero aspect-ratio cue on a fresh image, (2) user refers to "the image I made earlier" and no project conversation key exists yet, (3) user asks for multiple variants **and** supplied a reference image (`edit` works best at `n=1`).

## Workflow
1. Check `setup --show --json` once if web-pipeline defaults are unknown; run two-step `tiers` + `setup` only when `configured_web` is `false`.
2. Decide the intent and subcommand (`generate`, `edit`, `reference`, or `generate-batch`).
3. Decide whether the output is preview-only or meant for the current project (prefer `output/imagegen/`).
4. Collect inputs up front: prompt(s), exact text (verbatim), constraints/avoid list, and any input images.
5. For every input image, label its role explicitly: `reference image` / `edit target` / `supporting insert`.
6. Set `--size` or `--preset` from the requested ratio (do not repeat the ratio in the prompt). For `edit`, the input image fixes the aspect ratio.
7. Augment the prompt based on specificity (see **Prompt augmentation**). For multi-line prompts on Windows, write a UTF-8 file and pass `--prompt-file`.
8. Run the launcher. Watch `[waiting]` / `[slow]` heartbeats — never start a duplicate parallel run while one is in flight.
9. Verify three items before claiming success: a `[pipeline]` line, a `Wrote <path>` line, and the file existing on disk. If `[pipeline]` reports `degraded`, disclose it and quote the reason.
10. Inspect outputs (subject, style, composition, text accuracy, invariants/avoid items) and iterate with a single targeted `edit`, repeating invariants each round. If subject drifts off-brief, start fresh with `--new-conv`.
11. Report: final absolute path(s), final prompt used, `actual_size` (and delivered pixels if resampled), model + pipeline (`web`/`degraded`/`native`/`responses`), and any differing `revised_prompt`. Then stop and wait.

## Prompt augmentation

Reformat user prompts into a structured, production-oriented spec. Make the user's goal clearer and more actionable, but do not blindly add detail.

Treat this as prompt-shaping guidance, not a closed schema. Use only the lines that help, and add a short extra labeled line when it materially improves clarity.

### Specificity policy

- If the prompt is already specific and detailed, preserve that specificity and only normalize/structure it.
- If the prompt is generic, you may add tasteful augmentation when it will materially improve the result.

Allowed augmentations:
- composition or framing hints
- polish level or intended-use hints
- practical layout guidance
- reasonable scene concreteness that supports the stated request

Not allowed augmentations:
- extra characters or objects that are not implied by the request
- brand names, slogans, palettes, or narrative beats that are not implied
- arbitrary side-specific placement unless the surrounding layout supports it

The CLI implements this directly (`--use-case`, `--scene`, `--subject`, `--style`, `--composition`, `--lighting`, `--palette`, `--materials`, `--text`, `--constraints`, `--negative`, `--augment` / `--no-augment`). Already-labeled specs auto-skip wrapping.

## Use-case taxonomy (exact slugs)

Classify each request into one of these buckets and keep the slug consistent across prompts and references.

Generate:
- `photorealistic-natural` — candid/editorial lifestyle scenes with real texture and natural lighting.
- `product-mockup` — product/packaging shots, catalog imagery, merch concepts.
- `ui-mockup` — app/web interface mockups and wireframes; specify the desired fidelity.
- `infographic-diagram` — diagrams/infographics with structured layout and text.
- `scientific-educational` — classroom explainers, scientific diagrams, and learning visuals with required labels and accuracy constraints.
- `ads-marketing` — campaign concepts and ad creatives with audience, brand position, scene, and exact tagline/copy.
- `productivity-visual` — slide, chart, workflow, and data-heavy business visuals.
- `logo-brand` — logo/mark exploration, vector-friendly.
- `illustration-story` — comics, children's book art, narrative scenes.
- `stylized-concept` — style-driven concept art, 3D/stylized renders.
- `historical-scene` — period-accurate/world-knowledge scenes.

Edit:
- `text-localization` — translate/replace in-image text, preserve layout.
- `identity-preserve` — try-on, person-in-scene; lock face/body/pose.
- `precise-object-edit` — remove/replace a specific element (including interior swaps).
- `lighting-weather` — time-of-day/season/atmosphere changes only.
- `background-extraction` — transparent background / clean cutout (`--background transparent`, fallback `scripts/remove_chroma_key.py`).
- `style-transfer` — apply reference style while changing subject/scene.
- `compositing` — multi-image insert/merge with matched lighting/perspective.
- `sketch-to-render` — drawing/line art to photoreal render.

## Shared prompt schema

```text
Use case: <taxonomy slug>
Asset type: <where the asset will be used>
Primary request: <user's main prompt>
Input images: <Image 1: role; Image 2: role> (optional)
Scene/backdrop: <environment>
Subject: <main subject>
Style/medium: <photo/illustration/3D/etc>
Composition/framing: <wide/close/top-down; placement>
Lighting/mood: <lighting + mood>
Color palette: <palette notes>
Materials/textures: <surface details>
Text (verbatim): "<exact text>"
Constraints: <must keep/must avoid>
Avoid: <negative constraints>
```

Notes:
- Use only the lines that help; `Asset type` and `Input images` are prompt scaffolding, not CLI flags.
- `Scene/backdrop` is the visual setting, not `--background transparent`.
- Keep edits short and list invariants (`change only X; keep Y unchanged`).

### Person / character brief rule (relay safety discipline)

Long garment/body inventories trigger the output-side safety classifier (~0% pass rate). Compress person/character briefs to **5–7 lines** with **one** `Clothing:` consistency/coverage line — never enumerate individual garments, jewellery, or body/skin terms (`露肩/贴身/曲线/覆盖范围`):

```text
Use case: identity-preserve
Asset type: <e.g. 单张完成的游戏收藏卡牌>
Input images: 图1是编辑目标，也是角色外貌、服装、姿势与道具的唯一依据。
Primary request: <1–2 sentences: action + target scene/card>
Constraints: 保持图1角色的面貌、发型、姿势与道具形态不变；只新增<背景/卡面/装饰>。
Clothing: 角色服装与图1完全一致，为覆盖完整、不透明的服装；衣着保守得体。
Avoid: 其他人物、额外肢体、新增文字、水印。
```

## Examples

### Generation example (hero image)
```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "A minimal hero image of a ceramic coffee mug" `
  --use-case product-mockup --style "clean product photography" `
  --composition "wide composition with usable negative space for page copy" `
  --lighting "soft studio lighting" --constraints "no logos, no text, no watermark" `
  --size 1672x941 --out "output/imagegen/mug-hero.png"
```

### Edit example (invariants)
```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" edit `
  --image "input/product.png" `
  --prompt "Replace only the background with a warm sunset gradient." `
  --constraints "change only the background; keep the product and its edges unchanged; no text; no watermark" `
  --out "output/imagegen/product-sunset.png"
```

## Prompting best practices
- Structure prompt as scene/backdrop -> subject -> details -> constraints.
- Include intended use (ad, UI mock, infographic) to set the mode and polish level.
- Use camera/composition language for photorealism.
- Only use SVG/vector stand-ins when the user explicitly asked for vector output or a non-image placeholder.
- Quote exact text and specify typography + placement; spell tricky words letter-by-letter and require verbatim rendering.
- For multi-image inputs, reference images by index and describe how they should be used.
- For edits, repeat invariants every iteration to reduce drift; iterate with single-change follow-ups.
- If the prompt is generic, add only the extra detail that will materially help; if already detailed, normalize instead of expanding.
- Never promise native 4K detail, a third image model, or an upstream quality slider.

More principles: `references/prompting.md`. Copy/paste specs: `references/sample-prompts.md`.

## Relay controls & caveats (where relay differs from official)
- **Resolution (~1.57 MP native)**: upstream ignores `size` pixel counts and renders fixed ~1.57 MP per ratio (`1:1 -> 1254x1254`, `16:9 -> 1672x941`, `9:16 -> 941x1672`, `3:2 -> 1536x1024`, `4:3 -> 1448x1086`, ultra-wide `3.09:1 -> 2206x713`). Use `--preset <ratio>@2k|4k` or `--exact-size WxH` (`--fit cover|contain`) for exact delivered pixels via local Lanczos resample; when upscaling >1.5× (e.g. `@4k`), state honestly that it was **upscaled locally**.
- **Prompt-emulated controls**: `--quality high|low` folds a hint line into the prompt; `--background transparent` appends a transparency line and keeps PNG alpha (complex subjects: fall back to `scripts/remove_chroma_key.py` on a flat-colour render).
- **`--n > 1` on web pipeline**: upstream drains the whole turn and may return >`n` files and take 3–4 min. Pass `--strict-n` (runs `n` sequential `n=1` calls) or `--model codex-gpt-image-2` when you need strictly `n` outputs.
- **Animated GIF**: request a loop animation (`…的动画GIF，循环播放`). The CLI writes `.gif` verbatim (never re-encodes via Pillow), writes `<name>-sprite.png` if provided, and `--gif-speed {0.25..2}` scales `animation.frame_duration_ms`.
- **Conversation continuity**: isolated per project (`<cwd>/.imagegen/conv-<hash>`). New topic → `--new-conv`; parallel tasks in one folder → `--conv-key <task>`.
- **Higher `--effort` is not always better**: slower and refused more often; raise only for dense text, fine detail, or refinement. (`work` has 5 verified tiers: `min/standard/extended/xhigh/max`; legacy `minimal` auto-maps to `min`.)
- **Defaults**: `--preflight off`, `--retry-refusal 0`, layered timeouts (`150s/240s` normal single-image; `300s/480s` heavy `n>1`/`work`/`extended+`/`reference`). Never fabricate a local PIL composite and call it generation.

## Failure handling (`CODE=` / `RETRYABLE=`)

Every failure writes `CODE=`, `RETRYABLE=`, `HINT=`, `RAW=` to stderr (with automatic `sk-*` redaction) and `{ok: false, error: {...}}` to `--report-json`. Decide by `CODE`, never by prose:

| `CODE` | `RETRYABLE` | Action |
| --- | --- | --- |
| `no_image_data` | yes | Re-send once (empty-stream responses cluster) |
| `content_refusal` | yes | Stochastic output-stage gate: use compact neutral brief + coverage line or retry once (`--retry-refusal 1`); a more conservative reference passes fastest |
| `bad_request` | yes | Unsupported tier/slug — drop one `--effort` tier or switch model and retry once |
| `timeout` / `rate_limited` / `network_error` | yes | Wait briefly and retry once; on repeated timeout, try `--new-conv` or `--model codex-gpt-image-2` |
| `upstream_error` | yes | Retry once verbatim, then report `RAW` |
| `content_safety` | no | Upstream moderation decision: relay verbatim, do not loop-retry; see `references/safety-refusals.md` |
| `webpipe_invalid_request` | no | Web pipeline rejected slug or `thinking_effort` (`conduit_token` / `422 Invalid conversation body`) — fix `--web-mode`/`--agent-model`/`--effort` via `tiers` |
| `webpipe_blocked` | no | Web pipeline hit upstream 403 / Cloudflare / Sentinel challenge — wait, switch to `codex-gpt-image-2`, or ask relay owner to refresh cookies |
| `model_refusal` | no | Chat model declined (`reference` route): rephrase or switch to `edit` / `--model codex-gpt-image-2` |
| `deadline_exceeded` | no | Budget exhausted, nothing written — raise `--deadline`, try `--new-conv`, or switch to `codex-gpt-image-2` |
| `upstream_overloaded` / `relay_cpu` | no | Upstream `NO_MORE_RETRY` or relay CPU saturated — wait, do not retry immediately |
| `token_invalid` / missing token | no | User updates `api_key` in `config.json` locally (**never ask for or print tokens in chat**) |
| `no_model_access` / `no_channel` / `token_revoked` | no | Use `gpt-image-2` / `codex-gpt-image-2`, or notify relay owner to grant access / re-seed OAuth |

Rules: **never retry `RETRYABLE=no`**; retry `RETRYABLE=yes` **at most once**; if the skill/launcher is newly installed and missing, have the user open a new conversation.

## Reference map
- `references/prompting.md`: shared prompting principles (**verbatim from official skill — use for prompt craft only; ignore its `Fallback-only execution controls` section, which mentions `gpt-image-1.5` and native 4K**).
- `references/sample-prompts.md`: shared copy/paste prompt recipes (verbatim from official skill).
- `references/cli.md`: full CLI flags, sizes/presets, masks, batch JSONL format, `--dry-run`.
- `references/relay-api.md`: wire contracts (Codex JSON vs OpenAI multipart), `style.mssk_*`, GIF JSON schema, tier mechanics.
- `references/safety-refusals.md`: input vs output safety gates, measured ablation data, `--preflight`, browser-vs-CLI DevTools troubleshooting.
- `references/codex-network.md`: network/sandbox notes for this installation.
- `scripts/image_gen.py`: the relay CLI itself.
- `scripts/relay_errors.py`: single-source failure classifier & secret redactor.
- `scripts/remove_chroma_key.py`: local chroma-key cutout fallback (verbatim from official skill).
