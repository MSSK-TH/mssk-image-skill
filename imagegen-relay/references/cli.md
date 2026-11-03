# CLI reference (`scripts/image_gen.py`)

This CLI is a sibling of the official `imagegen` skill's fallback CLI: same subcommands, same
flags, same prompt-augmentation engine, same output-path rules, same batch JSONL semantics.
Only the transport differs -- it talks to the MSSK relay instead of `api.openai.com`, and it
needs no `openai` package.

## What this CLI does

Two routes, matching the official skill's two layers (see `SKILL.md`, Relay fact 1):

- **Tool route** (`reference`): `POST /v1/responses` with the hosted `image_generation` tool. The
  chat model integrates the reference image(s) itself. This is the relay counterpart of the
  official built-in `image_gen` path.
- **Images route** (`generate` / `edit` / `generate-batch`): `POST /v1/images/generations` and
  `POST /v1/images/edits` -- the relay counterpart of the official fallback CLI.
- `models`: list the image-capable models the relay exposes (relay-only diagnostics).

Real calls need network access plus a relay token. `--dry-run` needs neither.

## Reference mode (tool route)

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" reference `
  --image "input/character.png" `
  --prompt "把这个角色放进一张炫酷的收藏卡牌，卡面加入夜间球场灯光与蓝金边框" `
  --out "output/imagegen/character-card.png"
```

- `--image` may be repeated; every image is sent as a reference.
- `--model` defaults to `gpt-6-astra` (a chat model that drives the image tool). `gpt-5.6-sol`
  also works. This is not the Images-route model.
- `--size`, `--quality`, `--background` are forwarded to the **tool definition**, not to the
  Images API.
- `--max-dim` (default 1400) downscales the reference before upload when Pillow is available;
  `--max-dim 0` uploads the original bytes.
- Measured: 44.6 s wall clock, output 1024x1536 for a portrait reference (the tool picks its own
  geometry, unlike the Images route's fixed ~1.57 MP fold).
- Failure shape: the tool call ends `status=failed`, the CLI prints one clean
  `Error: reference generation failed ... (tool status: ...)` line and relays the model's own
  explanation. It never retries a refusal.

## Images route

## How to call it

Always go through the launcher, which reads `config.json` and injects the endpoint and token:

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "A cozy alpine cabin at dawn" `
  --size 1672x941 `
  --out output/imagegen/alpine-cabin.png
```

```bash
sh ~/.codex/skills/imagegen-relay/bin/run.sh generate \
  --prompt "A cozy alpine cabin at dawn" \
  --size 1672x941 \
  --out output/imagegen/alpine-cabin.png
```

Calling `python scripts/image_gen.py` directly works too, when `IMAGE_RELAY_URL` and
`IMAGE_RELAY_KEY` (or `config.json`) are already in place.

## Guardrails

- Prefer the launcher; it is what makes the token configuration invisible.
- Do **not** create one-off runners (`gen_images.py` and friends) around this CLI.
- Do **not** rewrite the transport layer to add an SDK; it is dependency-free on purpose.
- A content-safety refusal is final -- do not retry it, do not soften the user's wording on your
  own initiative.

## Defaults

- Model: `gpt-image-2` (the only image model this relay serves)
- Size: `auto` (renders 1:1)
- Quality: `medium` (no prompt hint; see the mapping table in `relay-api.md`)
- Output format: `png`
- Default one-off output path: `output/imagegen/output.png`
- Batch concurrency: `2` (official CLI default is 5; calibrated for this relay)
- Request timeout: 300 s, retries: 3

## Sizes

`--size` selects an aspect ratio; the upstream always renders at roughly 1.57 MP.

| Wanted ratio | Pass `--size` | Actual output |
| --- | --- | --- |
| 1:1 square | `1254x1254` | 1254x1254 |
| 16:9 landscape | `1672x941` | 1672x941 |
| 9:16 portrait | `941x1672` | 941x1672 |
| ultra-wide | `2206x713` | 2206x713 |
| 3:2 | `1536x1024` | 1536x1024 |
| 4:3 | `1448x1086` | 1448x1086 |

- No size given -> `auto` -> 1:1.
- `--size 1024x1024` is accepted and produces 1254x1254: the pixel count is not honoured.
- Official gpt-image-2 size constraints (multiples of 16, long:short <= 3:1, 655,360-8,294,400
  pixels) are checked as **warnings** here; `--strict-size` turns them back into hard errors.
- If the user asks for 4K/2K, say plainly that this relay cannot: the upstream is fixed at
  ~1.57 MP and the only control is the aspect ratio.

Fast draft / final asset examples:

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "A product thumbnail of a matte ceramic mug on a stone surface" `
  --size 1254x1254 --out output/imagegen/mug-draft.png

& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "A polished landing-page hero image of a matte ceramic mug on a stone surface" `
  --size 1672x941 --out output/imagegen/mug-hero.png
```

## Quality, background, fidelity, masks

`--quality` works for `generate`, `edit`, and `generate-batch`, but it is folded into the prompt
(`high` -> `Quality: high quality, rich detail`, `low` -> `Quality: keep it simple`, `medium`/`auto`
-> nothing). There is no real quality setting on this relay.

`--background transparent` appends a transparency requirement to the prompt and keeps PNG output.
It is an emulation, not an API parameter; for complex subjects use
`scripts/remove_chroma_key.py` as the fallback. `--background opaque` / `auto` are no-ops.

`--input-fidelity` is edit-only and validated as `low|high`, then ignored (gpt-image-2 always uses
high fidelity). `--mask` is edit-only and passed through as a multipart part; upstream behaviour is
not yet characterised -- describe the region in the prompt as well.

Mask notes:

- For multi-image edits, pass repeated `--image` flags; their order is meaningful.
- Image and mask should be the same size and format, each under 50MB.
- Masks should be PNG with an alpha channel.
- If multiple input images are provided, the mask applies to the first image.
- Masking is prompt-guided; do not promise pixel-perfect boundaries.

## Output handling

- Use `tmp/imagegen/` for temporary JSONL inputs or scratch files.
- Use `output/imagegen/` for final outputs.
- Reruns fail if a target file already exists unless you pass `--force`.
- `--out-dir` changes one-off naming to `image_1.<ext>`, `image_2.<ext>`, and so on.
- Downscaled copies use the default suffix `-web` unless you override it.
- Non-PNG `--output-format` values (and `--output-compression`) are produced by local re-encoding
  and need Pillow. Without Pillow the CLI stops with a message that shows how to run through `uv`.

## Recipes

Generate with augmentation fields (identical output to the official CLI):

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "A minimal hero image of a ceramic coffee mug" `
  --use-case "product-mockup" `
  --style "clean product photography" `
  --composition "wide product shot with usable negative space for page copy" `
  --constraints "no logos, no text" `
  --out output/imagegen/mug-hero.png
```

Generate plus a downscaled copy for fast web loading:

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "A cozy alpine cabin at dawn" `
  --size 1254x1254 --downscale-max-dim 1024 `
  --out output/imagegen/alpine-cabin.png
```

Batch (JSONL), two prompts concurrently:

```powershell
New-Item -ItemType Directory -Force tmp/imagegen, output/imagegen/batch | Out-Null
@'
{"prompt":"Cavernous hangar interior with a compact shuttle parked near the center","use_case":"stylized-concept","composition":"wide-angle, low-angle","lighting":"volumetric light rays through drifting fog","constraints":"no logos or trademarks; no watermark","size":"1672x941"}
{"prompt":"Gray wolf in profile in a snowy forest","use_case":"photorealistic-natural","composition":"eye-level","constraints":"no logos or trademarks; no watermark","size":"1254x1254"}
'@ | Set-Content tmp/imagegen/prompts.jsonl -Encoding UTF8

& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate-batch `
  --input tmp/imagegen/prompts.jsonl `
  --out-dir output/imagegen/batch `
  --concurrency 2
```

Notes:

- `generate-batch` requires `--out-dir`.
- Per-job overrides are supported in JSONL (`model`, `n`, `size`, `quality`, `background`,
  `output_format`, `output_compression`, `moderation`, `out`, all 11 augmentation fields either
  flat or nested under `fields`).
- `--n` generates variants of one prompt; `generate-batch` is for many different prompts.
- In batch mode, per-job `out` is treated as a filename under `--out-dir`.
- Keep `--concurrency` at 2. One render takes 20-60 s here; higher concurrency buys contention,
  timeouts, and content-safety refusals rather than speed.

## Relay-only flags

| Flag | Purpose |
| --- | --- |
| `--base-url URL` | override the relay URL for one run (token still comes from `config.json`) |
| `--timeout SECONDS` | per-request timeout, default 300 |
| `--deadline SECONDS` | hard wall-clock budget for one job, default 600; the CLI aborts cleanly instead of grinding for tens of minutes |
| `--max-attempts N` | transient-failure retries (5xx / 429 / timeouts), default 3 |
| `--retry-refusal N` | re-sends after an upstream content-safety refusal with a semantically neutral variation, **default 0 (off)**. The output-side gate is stochastic, so this is the difference between ~1/3 and ~70-87% success on a person edit; identical bodies are cached and get the same refusal |
| `--preflight` | coverage preflight, **default `off`** (prompt is sent unchanged). `auto` = person prompts only, `always` = every request; it neutralises body/garment vocabulary and appends one clothing/coverage constraint, printing every change (see `SKILL.md`, Relay fact 7) |
| `--check-model` | verify the model against `/v1/models` before spending a generation |
| `--strict-size` | restore the official hard failure on gpt-image-2 size constraints |
| `--report-json PATH` | write a JSON report (prompt, outputs, `actual_size`, pixels, timing) |

While a request is in flight the CLI prints a `[waiting] …s elapsed` heartbeat every 20 s, so a
long render never looks like a hang. Content-safety refusals are printed as a single clean
`Error: …` line (never a traceback) and are never retried locally.

## Dry run (free)

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "test" --dry-run
```

`--dry-run` makes no network call, needs no token, and echoes the payload. It prints the same keys
as the official CLI plus three relay keys: `wire_payload` (what actually goes on the wire),
`base_url`, and `endpoint`.

## Common failure messages

| Message | Cause | Action |
| --- | --- | --- |
| `Relay token is not configured` | `api_key` in `config.json` is empty | the user fills it in locally; never ask for the token in chat |
| `relay rejected the token (HTTP 401)` | token deleted or expired | recreate it in the relay console, update `config.json` |
| `relay has no channel for this model` | model other than `gpt-image-2` | use `gpt-image-2` |
| `上游内容安全系统拒绝：<text>` | upstream content-safety decision | relay `<text>` verbatim; do not retry or rewrite |
| timeout / `429` / `5xx` | upstream busy | the CLI already retried; wait and retry once, never in a loop |

## See also

- `references/relay-api.md` -- wire contract, measured behaviour, error vocabulary
- `references/prompting.md` -- shared prompting principles (verbatim from the official skill)
- `references/sample-prompts.md` -- copy/paste prompt recipes (verbatim from the official skill)
- `references/codex-network.md` -- network/sandbox notes for this installation
