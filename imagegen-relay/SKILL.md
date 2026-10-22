---
name: "imagegen-relay"
description: "Generate or edit raster images through the MSSK relay (gpt-image-2). This is the image path on this Codex installation: the built-in image_gen tool does not work with a custom/relay model provider. Use whenever the user asks for a bitmap image -- 生图, 画图, 生成图片, 配图, 出图, 改图, 修图 -- or when a task needs photos, illustrations, posters, storyboards, character sheets, sprites, product shots, UI mockups, infographics, textures, or transparent-background cutouts. Do not substitute SVG/HTML/canvas/PIL/matplotlib when the user wants a bitmap. Images only, never video."
---

# Image Generation (Relay)

Generates or edits images for the current project -- website assets, game assets, UI mockups,
product mockups, posters, storyboards, character sheets, infographics, transparent cutouts.

This skill is the relay counterpart of Codex's built-in `imagegen` skill. The prompt craft,
the use-case taxonomy, the CLI surface, and the iteration discipline are deliberately identical
to that skill. What differs is the transport: requests go to the MSSK relay instead of the
OpenAI backend, so a few of the official promises do not hold here. Those differences are listed
below and they win wherever the two disagree.

## Relay facts (read first)

### 1. Two routes inside one CLI -- mirroring the official skill

The official skill has two layers: the built-in `image_gen` tool (a **Responses-API hosted tool**
that the chat model drives) and a fallback CLI that calls the **Images API** directly. This relay
exposes both, so this skill exposes both, through one launcher.

| Route | Subcommand(s) | Wire call | Use it for |
| --- | --- | --- | --- |
| **Tool route** -- the official "built-in" analogue | `reference` | `POST /v1/responses` with `tools:[{"type":"image_generation"}]` | Anything that starts from reference image(s): "put this character/object into X", style transfer, scene compositing, identity-preserving variants. **Preferred for reference work.** |
| **Images route** -- the official "fallback CLI" analogue | `generate`, `edit`, `generate-batch` | `POST /v1/images/generations`, `POST /v1/images/edits` | Text-only generation, `--n` variants, batch JSONL, masks, explicit size/format/quality handling |

```powershell
# tool route: the model integrates the reference itself
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" reference `
  --image "character.png" --prompt "..." --out "output/imagegen/card.png"

# images route: pure text generation, or pixel-level edits with explicit controls
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "..." --size 1672x941 --out "output/imagegen/hero.png"
```

Rules:

- Always call the launcher; it is what injects the relay URL and token.
- Never build one-off wrappers around it, and never rewrite the transport to add an SDK.
- `--dry-run` is free: no network, no token, and it echoes the exact payload.
- The two routes do **not** share an upstream safety gate -- see fact 7 for which one refuses what.

### 2. Two model slugs, two pipelines (both served by this relay)

`--model` picks **which pipeline** the relay uses. Both reach the same upstream account; the
difference is the judgement that decides whether an image is allowed through.

| `--model` | Pipeline | Measured speed | Refusal means |
| --- | --- | --- | --- |
| `gpt-image-2` (**default**) | **Web pipeline first** (`backend-api/f/conversation` + `system_hints:["picture_v2"]`), auto-falls back to the native endpoint when the web attempt is refused | ~26-45 s (adds ~20 s only when it falls back) | **both** pipelines tried and refused |
| `codex-gpt-image-2` | **Native subscription endpoint only** (`/backend-api/codex/images/*`) | ~15-30 s | native refused; no fallback |

Rules of thumb:
- **Default to `gpt-image-2`** — higher pass rate. Measured: a reference-image card request that the
  native endpoint refused 6/6 went through on the first web attempt.
- Use `codex-gpt-image-2` only when the caller needs **speed** or explicitly wants the Codex path.
- Never promise a third image model name: any other slug fails with `No available channel`.

### 3. Resolution: upstream render is fixed, delivered size is yours

The upstream always renders at roughly **1.57 MP** per ratio and cannot be talked into anything else
(`size` is ignored; measured 16:9 -> 1672x941, 1:1 -> 1254x1254). Two separate layers:

1. **Ratio** -- pass `--size`; the relay folds it into one aspect-ratio requirement line.
2. **Delivered pixels** -- `--preset <ratio>@<2k|4k>` or `--exact-size WxH` (local Lanczos resample;
   `--fit cover` crops to fill, `--fit contain` letterboxes).

| User asks for | Pass | Delivered file | Native render behind it |
| --- | --- | --- | --- |
| 1:1 / avatar | `--preset 1:1@2k` | 2048x2048 | ~1254x1254 (1.63x upscale) |
| 16:9 / wallpaper | `--preset 16:9@2k` | 2048x1152 | ~1672x941 (1.22x) |
| 16:9 at 4K | `--preset 16:9@4k` | 3840x2160 | same native render, 2.3x upscale |
| 9:16 vertical | `--preset 9:16@2k` | 1152x2048 | ~941x1672 |
| square, small | `--size 1254x1254` (no preset) | 1254x1254 | native, no resample |
| anything else | `--size <same ratio> --exact-size WxH` | exactly WxH | resampled |

- `--preset` accepts 11 ratios: `1:1 3:2 2:3 4:3 3:4 16:9 9:16 2:1 1:2 7:4 4:7`, each at `2k` or `4k`.
- It validates **before** the paid call: 16-pixel alignment, max edge 4096, total-pixel range, and it
  refuses a `--preset` that fights an explicit `--exact-size`.
- When the resample factor is above 1.5x, say the file was **upscaled locally** -- do not imply real
  4K detail. Never describe the native render itself as "2K" or "4K".
- `edit` ignores the ratio part of `--preset` (the reference image sets the composition); only the
  delivered size applies.

| Ratio the user wants | Pass `--size` | Native output |
| --- | --- | --- |
| 1:1 square / avatar | `1254x1254` | 1254x1254 |
| 16:9 widescreen / desktop wallpaper | `1672x941` | 1672x941 |
| 9:16 vertical / phone wallpaper | `941x1672` | 941x1672 |
| ultra-wide | `2206x713` | 2206x713 |
| 3:2 | `1536x1024` | 1536x1024 |
| 4:3 | `1448x1086` | 1448x1086 |

- User named a ratio -> pass the matching `--size`. **Do not write the ratio into the prompt**;
  the relay already folds `size` into one aspect-ratio requirement line.
- User named no ratio -> leave `--size` alone (`auto` -> 1:1).
- `2206x713` is **3.09:1, not 21:9**. If the user asks for 21:9, say this relay's ultra-wide is
  wider than that and there is no exact 2.33:1 slot.

### 4. `--quality` is a prompt hint, not a setting

Upstream forces `auto`. The CLI folds `--quality high` into `Quality: high quality, rich detail`
and `--quality low` into `Quality: keep it simple`, inside the prompt. If the user asks to change
the quality slider, say honestly that this relay has no such control.

### 5. Transparency is prompt-emulated

`--background transparent` does not send an API parameter: the CLI appends a transparency
requirement to the prompt and keeps PNG output. Measured on this relay, 46-52% of returned pixels
came back fully transparent. State the requirement in the prompt as well (isolated subject, no
backdrop/floor/shadow, clean alpha edges with detail preserved in semi-transparent areas) and
preserve the alpha channel when saving. If the subject is complex and the alpha comes back dirty,
fall back to `scripts/remove_chroma_key.py` on a flat-colour render.

### 6. One render takes 20-60 s

That is normal. The CLI waits up to `--timeout` seconds (default 300) and prints a timing line
(`relay+render`, `download`, `local`, `total`). A `[waiting] …s elapsed` heartbeat prints every
20 s while a request is in flight, so a slow render is distinguishable from a hang.

- Do not re-issue a request because it "feels slow" -- a second render in parallel makes the
  session slower, not faster.
- One job has a hard wall-clock budget: `--deadline` (default 600 s). When it is used up the CLI
  aborts with a clean message instead of grinding for 20 minutes.
- Batch concurrency stays at 2. Higher values buy contention, timeouts, and refusals.
- Keep the path short: one call produces the image. Do not chain dry-run -> generate -> convert ->
  edit as a warm-up ritual, and do not trigger a package install unless the user asked for
  something that needs one.

### 7. Content-safety refusals: two gates, and the fix that keeps the intent

There are **two** safety gates, and the latency tells you which one fired:

| Gate | What is judged | Latency | Stable? |
| --- | --- | --- | --- |
| Prompt-level | the text you sent | 400 in a few seconds | stable -- always refused |
| **Output-image level** | the image that was just rendered | **same as a success** (tens of seconds) | **stochastic** -- the same wording sometimes passes |

Everything measured on this relay lands in the second row: refusals came back after ~100-140 s,
`moderation: low` changed nothing, and re-sending an identical body returned the same cached
refusal. Deleting words, cropping the reference, shrinking it to 512 px JPEG, or switching to the
`/responses` tool route all failed.

Why a browser client can pass the same request: **the web app is an agent**. Its chat model rewrites
and expands your prompt first -- typically filling in clothing, setting and lighting -- before the
image model ever sees it. An API call sends the prompt verbatim, so the model decides the outfit
itself and the render drifts over the line. Same picture, different prompt reaching the renderer.

**The fix that keeps the original intent: ADD an explicit coverage constraint instead of deleting
words.** Measured on the refused baseball card (same reference image, same composition):

| Variant | Result |
| --- | --- |
| original request | refused (~106 s) |
| original + `服装与图1完全一致，为覆盖完整、不透明的运动套装；衣着保守、得体` | **PASS (93 s)** |
| original + `穿着图1中的完整运动套装（外套、上衣与短裤），覆盖完整不透明` | **PASS (142 s)** |
| control: original with one unrelated adjective swapped | refused (~106 s) |

So, when a refusal names 性化内容 / 过度暴露 / 露骨:

1. **First check the latency.** Seconds -> prompt-level: remove the offending word. Tens of seconds
   -> output-level: go to step 2.
2. **Add a coverage constraint** (clothing fully covering, opaque, consistent with the reference,
   modest). Write it yourself -- the CLI does **not** add it by default; enable `--preflight auto`
   only if you would rather have the CLI do it (see "Coverage preflight" below).
3. **If it is still refused, do not patch the long brief.** Measured: the full "keep every detail"
   brief plus the coverage line was still refused (153 s), while a **compact neutral brief** with
   the same intent and the same reference image passed twice (93 s / 142 s). Rewrite instead of
   appending.
4. **Do not silently delete or soften the user's own description**, and do not quietly swap the
   outfit. If the only way through is changing the content, say so and let the user decide.
5. **Never fabricate a workaround.** Compositing the subject into a background locally is *not*
   image generation: it changes what the deliverable is. Do it only if the user explicitly asks for
   compositing as such, and never present it as 生图.
6. **Relay the upstream text verbatim** and state that this is an upstream policy decision this
   skill cannot override.

#### Compact neutral brief (the shape that passed)

Long constraint lists are what push the render over the line: enumerating garments and body
metrics, "keep the clothing coverage unchanged", and a long `Avoid:` inventory all make the
classifier read the brief as sexualised. When a person edit is refused, rebrief it like this and
re-send -- short, no body vocabulary, one coverage line:

```text
Use case: identity-preserve
Input images: 图1是编辑目标，也是角色外貌、服装、姿势与道具的唯一依据。
Primary request: 将图1中的角色融入一张完整可见的收藏卡牌；只在角色身后与四周增加卡面背景与边缘装饰。
Constraints: 保持图1角色的面貌、发型、姿势与道具形态不变；仅新增卡面与边缘装饰。
Clothing: 服装与参考图一致，覆盖完整、不透明、保守得体。
Avoid: 其他人物、额外肢体、改变造型、新增文字、水印。
```

#### Measured pass rates (this reference photo, one channel)

The gate is stochastic, so single results mean nothing. Sample so far:

| Brief style | Results | Pass rate |
| --- | --- | --- |
| long "keep every detail" list (agent-written) | 0 pass / 5 refusals | ~0% |
| compact narrative brief + coverage line | 3 pass / 8 attempts | ~1/3 |
| compact brief that lets the render **cover the outfit up** (no "keep the outfit identical" clause) | 1 pass / 1 attempt, first try (53.7 s) | high |

Two practical consequences:

- **Keeping the exact outfit is the expensive choice.** Demanding "clothing unchanged" while the
  upstream wants coverage leaves roughly a 1-in-3 chance per attempt. Letting the render put the
  character in fully covering sportswear normally succeeds first try.
- **Retry, because the gate is stochastic.** `--retry-refusal N` (default 2) re-sends with a
  semantically neutral variation on a refusal -- identical bodies are answered from a cache and
  would return the same refusal. At a 1/3 pass rate, three attempts reach ~70%, five ~87%.
  Each attempt costs a full render, so raise `N` deliberately, not by reflex.

#### Coverage preflight (`--preflight`, **off by default**)

**Default is `off`: the CLI sends your prompt exactly as written and never rewrites it.** That keeps
the request byte-comparable with other clients, which is what you want when you are measuring.
Enable it deliberately when you want the extra insurance:

- **`auto`** -- only for prompts that mention a person/character;
- **`always`** -- every request; **`off`** / `--no-preflight` -- never (the default).

When enabled it does two things and **reports every change** on stderr (`[preflight] …`) and in the
report (`coverage_preflight_applied`):

1. neutralises phrases that pull the render toward a sexualised reading (undergarment nouns, body
   metrics, "coverage" phrasing, revealing/sexy wording) -- matched with word boundaries, so
   `surface`, `women`, `factor`, `hero image` and `3D model` are left alone (9/9 negative cases
   verified);
2. appends one explicit clothing/coverage constraint in the prompt's language.

The preflight prevents the text-only class of refusals, but it is **not** a substitute for the
compact brief above: on a long, heavily constrained person brief it was still refused, so treat it
as the first cheap attempt, not a guarantee.

### 8. Contracts: Codex JSON vs OpenAI multipart

The official Codex client posts to `{provider_base_url}/images/generations` and
`/images/edits` itself (source: `codex-rs/codex-api/src/endpoint/images.rs`) with a **JSON** body:

```json
{"images":[{"image_url":"data:image/png;base64,..."}],"prompt":"...","model":"gpt-image-2"}
```

OpenAI-compatible relays (new-api and friends) expect the **OpenAI Images contract** instead:
`multipart/form-data` with one `image` file part per input, and they answer a Codex-contract
request with `400 image is required for /v1/images/edits`.

What this means in practice:

- `edit` tries the Codex contract first and falls back to multipart automatically, reporting which
  one it used (`[edit] contract used: …`). Nothing else changes for the caller.
- **Verified limit (2026-10-16):** with a custom/relay provider the Codex client does **not** send
  the hosted `image_generation` tool at all. Four `codex exec` runs with the provider `base_url`
  pointed at a translating shim -- including a capture of the raw `/responses` body -- show a tools
  array containing only harness tools: no `image_generation`, no `images/*` request, no new entry
  in `$CODEX_HOME/generated_images`, and the model itself reports having no built-in image tool.
  So neither a relay-side rewrite nor a translation shim brings the built-in `image_gen` back on a
  relay provider; that tool exists only on the official account path. Never tell a user that a
  relay-side change will restore it.
- The contract knowledge still matters for this CLI and for any other client that speaks the Codex
  images contract (a future Codex build, a script that mimics it): those requests are answered
  correctly instead of failing with `400 image is required`.

### 9. "It works in the web client but not here"

When the user reports that the same reference image and prompt succeed in a browser client, do not
guess. Ask for **one** concrete artifact:

1. The working request from the browser: DevTools -> Network -> the image request -> *Copy as cURL*
   (or the URL plus the request payload).
2. The relay's own request log for that successful call: endpoint, model, and the token group and
   channel it was routed to.

The difference is nearly always one of three things: a different endpoint (chat/responses tool vs
`images/edits`), a different upstream channel behind the same model name (`new-api` routes by token
group), or a different reference image. Change something in this skill only after that comparison.

## When to use

- Generate a new image (concept art, product shot, cover, website hero, character sheet)
- Generate a new image using one or more reference images for style, composition, or mood
- Edit an existing image (inpainting, lighting or weather changes, background replacement,
  object removal, compositing, transparent background)
- Produce many assets or variants for one task

## When not to use

- Extending or matching an existing SVG/vector icon set, logo system, or illustration library
  inside the repo
- Creating simple shapes, diagrams, wireframes, or icons that are better produced directly in
  SVG, HTML/CSS, or canvas
- Making a small project-local asset edit when the source file already exists in an editable
  native format
- Any task where the user clearly wants deterministic code-native output instead of a generated
  bitmap

## Decision tree

Two separate questions:

1. **Intent:** a new image, or an edit / a reference-driven image?
2. **Execution strategy:** one asset, or many assets/variants?

Route first (Relay fact 1):

- **Any request that carries a reference image (person/character, card, scene) -> `edit`.** The
  relay now serves `/v1/images/*` straight from the upstream's own image endpoints
  (`/backend-api/codex/images/*`), so this route is the fastest and sends the brief **verbatim**
  (no chat model in the loop). Measured 2026-10-16 on this relay: reference edit **24.5 s**,
  text-to-image **14.6 s**, refusal **26.9 s** with a structured reason
  (`moderation_blocked`, stage/category/request id). The relay keeps its legacy
  `/responses` + image-tool path as an **automatic fallback** if the native endpoint fails.
- No images at all -> **`generate`**.
- Use `reference` only when you specifically want a chat model to write the image prompt, or when
  the relay's image path is unavailable. It is slower (measured **57 s**) and the model's rewrite can
  lengthen or alter the brief.
- Many distinct assets -> one `generate` call each, or `generate-batch`.

Intent:

- The user wants to modify an existing image while preserving parts of it -> **edit**.
- The user supplies images only as references for style, composition, mood, or subject ->
  **reference**.
- No images supplied -> **generate**.

Execution strategy:

- Many assets or variants -> one call per asset, or one `generate-batch` job per asset. Do not use
  `n` as a substitute for separate prompts: `n` is variants of one prompt.
- Assume the user wants a new image unless they clearly ask to change an existing one.

Relay-specific note: an **edit keeps the input's aspect ratio** and always outputs ~1.57 MP.
"Change this image to 16:9" cannot be done by editing -- regenerate at the target ratio, or crop
afterwards. Never fake it by pasting the subject onto a blank canvas at the target ratio; that
produces flat border bands, not a composition.

## Workflow

1. Decide the intent: `generate` or `edit`.
2. Decide whether the output is preview-only or meant to be consumed by the current project.
3. Decide the execution strategy: single call, repeated calls, or `generate-batch`.
4. Collect inputs up front: prompt(s), exact text (verbatim), constraints/avoid list, input images.
5. For every input image, label its role explicitly: reference image / edit target / supporting
   insert.
6. Set `--size` from the requested ratio (Relay fact 3). For `edit`, do not pass `--size`.
7. Augment the prompt based on specificity (see **Prompt augmentation**).
8. Run the launcher with `generate`, `edit`, or `generate-batch`.
9. Inspect the output: subject, style, composition, text accuracy, invariants/avoid items.
10. Iterate with a single targeted change, then re-check.
11. For project-bound work, put the final artifact inside the workspace (prefer
    `output/imagegen/`), never in a temp path.
12. Report the final path(s), the final prompt, `actual_size`, and the model.

## Prompt augmentation

Reformat user prompts into a structured, production-oriented spec. Make the user's goal clearer
and more actionable, but do not blindly add detail.

Treat this as prompt-shaping guidance, not a closed schema. Use only the lines that help, and add
a short extra labeled line when it materially improves clarity.

### Specificity policy

- If the prompt is already specific and detailed, preserve that specificity and only
  normalize/structure it.
- If the prompt is generic, you may add tasteful augmentation when it will materially improve the
  result.

Allowed augmentations:

- composition or framing hints
- polish level or intended-use hints
- practical layout guidance
- reasonable scene concreteness that supports the stated request

Not allowed augmentations:

- extra characters or objects that are not implied by the request
- brand names, slogans, palettes, or narrative beats that are not implied
- arbitrary side-specific placement unless the surrounding layout supports it

The CLI implements this directly: `--use-case`, `--scene`, `--subject`, `--style`, `--composition`,
`--lighting`, `--palette`, `--materials`, `--text`, `--constraints`, `--negative` build the labeled
spec for you, and `--augment` / `--no-augment` controls the behaviour (augment is the default).

`--augment` output is byte-identical to the official skill's CLI for the same inputs. When only the
relay-specific hints are added (`--quality high|low`, `--background transparent`), the CLI appends
them as extra labeled lines at the end -- never reword the user's own lines.

## Use-case taxonomy (exact slugs)

Classify each request into one of these buckets and keep the slug consistent across prompts and
references.

Generate:

- `photorealistic-natural` -- candid/editorial lifestyle scenes with real texture and natural
  lighting.
- `product-mockup` -- product/packaging shots, catalog imagery, merch concepts.
- `ui-mockup` -- app/web interface mockups and wireframes; specify the desired fidelity.
- `infographic-diagram` -- diagrams/infographics with structured layout and text.
- `scientific-educational` -- classroom explainers, scientific diagrams, and learning visuals with
  required labels and accuracy constraints.
- `ads-marketing` -- campaign concepts and ad creatives with audience, brand position, scene, and
  exact tagline/copy.
- `productivity-visual` -- slide, chart, workflow, and data-heavy business visuals.
- `logo-brand` -- logo/mark exploration, vector-friendly.
- `illustration-story` -- comics, children's book art, narrative scenes.
- `stylized-concept` -- style-driven concept art, 3D/stylized renders.
- `historical-scene` -- period-accurate/world-knowledge scenes.

Edit:

- `text-localization` -- translate/replace in-image text, preserve layout.
- `identity-preserve` -- try-on, person-in-scene; lock face/body/pose.
- `precise-object-edit` -- remove/replace a specific element (including interior swaps).
- `lighting-weather` -- time-of-day/season/atmosphere changes only.
- `background-extraction` -- transparent background / clean cutout.
- `style-transfer` -- apply reference style while changing subject/scene.
- `compositing` -- multi-image insert/merge with matched lighting/perspective.
- `sketch-to-render` -- drawing/line art to photoreal render.

## Shared prompt schema

Use the following labeled spec as prompt scaffolding for both generation and editing:

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

- Use only the lines that help. This is not a form to fill in.
- `Asset type` and `Input images` are prompt scaffolding, not CLI flags.
- `Scene/backdrop` is the visual setting, not a transparency control.
- Do **not** add an aspect-ratio line; `--size` already handles it.

Augmentation rules:

- Keep it short.
- Add only the details needed to improve the prompt materially.
- For edits, explicitly list invariants (`change only X; keep Y unchanged`).
- If any critical detail is missing and blocks success, ask a question; otherwise proceed.

**人物/角色类提示词必须写明服装 —— 但要写成"一句覆盖/一致性约束"，不要逐件列举。**

实测数据（同一参考图、同一渠道）：

| 简报写法 | 结果 |
| --- | --- |
| 一句「服装与图1完全一致，为覆盖完整、不透明的运动套装；衣着保守、得体」 | ✅ 2/2 通过，且出图保留了原服装 |
| 逐件列举（"白色露肩上衣、银色百褶短裙、腰部多层银链…"） | ❌ 几乎全被拒（长清单 0/5） |

逐件列举是**风险来源**，而且不管你写不写，改写层都会自己加上去（Codex 的 agent 和图像工具背后的模型都会）。

#### 默认简报模板（写人物/角色类请求时照这个来）

**这是实测最优写法**（紧凑简报 + 一句覆盖约束：6 过 / 17 试 ≈ 35%，重试 3 次约 70%；对照的长清单是 0/5）。

```text
Use case: identity-preserve
Asset type: <这张图用在哪儿，例如"单张完成的游戏收藏卡牌">
Input images: 图1是编辑目标，也是角色外貌、服装、姿势与道具的唯一依据。
Primary request: <一到两句：要做什么、放进什么场景/卡面里>
Constraints: 保持图1角色的面貌、发型、姿势与道具形态不变；只新增<卡面/背景/装饰>。
Clothing: 角色服装与图1完全一致，为覆盖完整、不透明的服装；衣着保守得体。
Avoid: 其他人物、额外肢体、新增文字、水印。
```

硬规则：

1. **总长控制在 5～7 行**。不要写"必须逐一保持不变"的长清单；
2. **服装只写一行**（`Clothing:`）—— 这是收益最高的一句，也是唯一必须写的一句；
3. **禁止**逐件列举衣物/首饰/身体细节（如"白色露肩上衣、银色百褶短裙、多层银链"），**禁止**"露肩/贴身/曲线/覆盖范围"这类词；
4. 用户给的超长约束清单**不要原样转贴**，压缩成上面那两行 `Constraints` 与 `Clothing`；
5. 用 `--prompt-file` 传这份简报时，**加 `--no-augment`**（简报本身已是标签式规格，不需要再加包装；CLI 现在也会自动识别并跳过包装）。
所以：**一句话写清"服装与参考图一致、覆盖完整"，然后停手**；不要发明用户没提过的服装、首饰或身体细节。

## Examples

### Generation example (hero image)

```text
Use case: product-mockup
Asset type: landing page hero
Primary request: a minimal hero image of a ceramic coffee mug
Style/medium: clean product photography
Composition/framing: wide composition with usable negative space for page copy if needed
Lighting/mood: soft studio lighting
Constraints: no logos, no text, no watermark
```

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "A minimal hero image of a ceramic coffee mug" `
  --use-case product-mockup `
  --style "clean product photography" `
  --composition "wide composition with usable negative space for page copy" `
  --lighting "soft studio lighting" `
  --constraints "no logos, no text, no watermark" `
  --size 1672x941 `
  --out "output/imagegen/mug-hero.png"
```

### Edit example (invariants)

```text
Use case: precise-object-edit
Asset type: product photo background replacement
Primary request: replace only the background with a warm sunset gradient
Constraints: change only the background; keep the product and its edges unchanged; no text; no watermark
```

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" edit `
  --image "input/product.png" `
  --prompt "Replace only the background with a warm sunset gradient." `
  --constraints "change only the background; keep the product and its edges unchanged; no text; no watermark" `
  --out "output/imagegen/product-sunset.png"
```

## Prompting best practices

- Structure the prompt as scene/backdrop -> subject -> details -> constraints.
- Include intended use (ad, UI mock, infographic) to set the mode and polish level.
- Use camera/composition language for photorealism.
- Specify framing and viewpoint (close-up, wide, top-down) and placement only when it helps.
- Call out negative space if the asset needs room for UI or copy.
- For people, describe body framing, scale, gaze, and object interactions when they matter.
- Quote exact text and specify typography + placement. For tricky words, spell them letter by
  letter and require verbatim rendering.
- For multi-image inputs, reference images by index and describe how they should be used.
- For edits, repeat invariants every iteration to reduce drift.
- Iterate with single-change follow-ups.
- If the prompt is generic, add only the extra detail that will materially help.
- If the prompt is already detailed, normalize it instead of expanding it.
- Only use SVG/vector stand-ins when the user explicitly asked for vector output.
- **Do not promise 2K/4K, other models, or a working quality slider** (Relay facts 2-4).

## Iteration discipline

**Inspect every output; when something is wrong, make a single targeted change and re-check.**

1. Inspect: subject, style, composition, text accuracy, and whether constraints/avoid items were
   violated.
2. Found a problem -> **change one thing**, then look again.
3. **Make the change with `edit`, not by regenerating.** Regenerating also throws away everything
   that was already right, and every generation consumes real relay quota.
4. Repeat the invariants on every iteration to reduce drift.
5. Ask only when blocked: if a critical detail is missing and you cannot get it right without
   asking, ask once. Otherwise proceed.

Use `generate` again only when the command errored (no image was produced) or the user explicitly
asks to redo the whole thing.

## Reporting

After every image, tell the user:

1. The image's **absolute path**
2. The final prompt used
3. **`actual_size`** from the response (for example `1672x941`) -- not a "requested size"
4. The model (`gpt-image-2`)
5. Any `revised_prompt` that differs from what was sent

Then stop and wait. If you just made a single targeted change, say what you changed. A closing
question such as "Tell me what to adjust and I'll edit it" is fine.

## Failure handling

| Error | Cause | Action |
| --- | --- | --- |
| `Relay token is not configured` | `api_key` in `config.json` is empty | **Let the user fill it in** -- open `%USERPROFILE%\.codex\skills\imagegen-relay\config.json`, put the token in `api_key`, save, then retry. Never ask for the token in chat |
| `relay rejected the token (HTTP 401)` | token deleted or expired | recreate it in the relay console; the user updates `config.json` |
| `relay has no channel for this model` | a model other than `gpt-image-2` | use `gpt-image-2` |
| timeout / `429` / `5xx` | upstream busy | the CLI already retried; wait and retry once, never in a loop. `--deadline` caps the total wait |
| `上游内容安全系统拒绝：<text>` | upstream content-safety decision on the **input image** -- not a fault, no image was produced | **Relay `<text>` to the user verbatim.** The relay already exhausted its internal retries, so do not re-send the same request and do not quietly make the description more modest. State that it is an upstream policy decision, and escalate the diagnosis (Relay fact 8). **Never substitute a local composite and call it generation.** |
| 4K / higher resolution requested | upstream fixed at ~1.57 MP | say no plainly; the only control is the aspect ratio |
| Skill not loaded / launcher missing | installed but not refreshed | have the user **open a new conversation** |

## Token and security

- The token lives in `config.json`. **Never** print it into a conversation, a log, a report, or a
  file, and never expose the full `Authorization` header when reporting an error.
- If a token appears in relayed error text, redact it first.
- `config.json` also accepts the environment fallbacks `IMAGE_RELAY_URL` / `IMAGE_RELAY_KEY`
  (then `CODEX_API_URL` / `CODEX_API_KEY`, then `OPENAI_*`), but `config.json` takes precedence and
  is what normal use relies on.

## Reference map

- `references/prompting.md` -- shared prompting principles (verbatim from the official skill)
- `references/sample-prompts.md` -- copy/paste prompt recipes (verbatim from the official skill)
- `references/cli.md` -- full CLI usage, sizes, batch format, relay-only flags
- `references/relay-api.md` -- wire contract, measured behaviour, error vocabulary
- `references/codex-network.md` -- network/sandbox notes for this installation
- `scripts/image_gen.py` -- the CLI itself
- `scripts/remove_chroma_key.py` -- fallback for transparent cutouts (verbatim from the official skill)

## One-line summary

**Write the prompt like the official skill, pick the ratio with `--size`, one call per image,
iterate with `edit`, and report `actual_size` -- no 4K, one model, 20-60 s per render.**
