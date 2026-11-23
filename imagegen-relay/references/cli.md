# CLI reference (`scripts/image_gen.py`)

This CLI is the relay sibling of official `imagegen`'s `scripts/image_gen.py`: same core subcommands (`generate`, `edit`, `generate-batch`), same official flags and prompt-augmentation engine, same output-path and batch JSONL semantics — plus relay-specific routes (`reference`, `setup`, `tiers`, `models`) and a stdlib-only HTTP transport (Python 3.9+; Pillow optional for local resample/re-encode).

## What this CLI does
- `generate`: generate a new image from a prompt (`POST /v1/images/generations`)
- `edit`: edit existing image(s) or run ratio-locked reference-driven generation (`POST /v1/images/edits`, primary reference route on this relay)
- `reference`: chat-model-driven reference generation via `POST /v1/responses` + hosted `image_generation` tool (use when changing aspect ratio from the reference or for free style reinterpretation; ignores `style.mssk_*`, use `--reasoning-effort`)
- `generate-batch`: run many generation jobs concurrently from a JSONL file (`--out-dir` required)
- `setup`: persist web-pipeline defaults (`--web-mode`, `--effort`, `--agent-model`) into `config.json`, or inspect safely with `--show [--json]` (never exposes `api_key`)
- `tiers`: list web-pipeline surface & reasoning tiers (`tiers` → `tiers --mode chat|work [--json]`)
- `models`: list image-capable models exposed by the relay (`GET /v1/models`)

Real calls require **network access** + `config.json` (or `IMAGE_RELAY_URL`/`IMAGE_RELAY_KEY`). `--dry-run` requires neither, writes zero files (does not touch `.imagegen/conv-*`), and echoes the exact wire payload (redacting base64 images on `reference`).

## Quick start (via launcher)

Always invoke through the launcher so `config.json` credentials are injected automatically:

```powershell
# Windows (PowerShell)
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "A cozy alpine cabin at dawn" `
  --size 1672x941 `
  --out output/imagegen/alpine-cabin.png
```

```bash
# macOS / Linux
sh ~/.codex/skills/imagegen-relay/bin/run.sh generate \
  --prompt "A cozy alpine cabin at dawn" \
  --size 1672x941 \
  --out output/imagegen/alpine-cabin.png
```

Dry-run (free, zero disk side effects):

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "Test" --out output/imagegen/test.png --dry-run
```

Check setup safely without reading `config.json`:

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" setup --show --json
```

## Guardrails
- Always call the bundled launcher (`bin/run.cmd` / `bin/run.sh`).
- Do **not** create one-off runners (`gen_images.py` and friends) or add an OpenAI SDK.
- **Never modify** `scripts/image_gen.py`. If something is missing, ask the user before doing anything else.
- Never retry a `RETRYABLE=no` failure code; never silently rewrite the user's clothing/intent on content-safety refusal.

## Defaults
- Model: `gpt-image-2` (web session pipeline first + native fallback) or `codex-gpt-image-2` (native subscription endpoint only); `reference` defaults to `gpt-6-astra`
- Size: `auto` (renders 1:1 at 1254×1254)
- Quality: `medium` (no prompt hint)
- Output format: `png`
- Default one-off output path: `output/imagegen/output.png`
- Batch concurrency: `2` (calibrated for this relay; official default is 5)
- Layered timeouts (overridable with `--timeout` / `--deadline`):
  - Normal single-image (`n=1`, not `work`, not `extended/xhigh/max`): `150s` timeout / `240s` deadline
  - Heavy (`n>1`, `work`, `extended/xhigh/max`, `reference`): `300s` timeout / `480s` deadline
- Retries: `--max-attempts 3` (long >=60s render timeouts capped at 1 retry; non-retryable codes like `relay_cpu` 503 are never retried); `--retry-refusal 0`; `--preflight off`

## Sizes & Presets

Upstream always renders at ~1.57 MP per aspect ratio. Pass `--size` for native output or `--preset <ratio>@2k|4k` / `--exact-size WxH` (`--fit cover|contain`) for exact locally-resampled delivery (11 ratios: `1:1 3:2 2:3 4:3 3:4 16:9 9:16 2:1 1:2 7:4 4:7`, up to 8,294,400 px):

| Wanted ratio | Native `--size` | 2K preset | 4K preset (local upscale) |
| --- | --- | --- | --- |
| 1:1 square | `1254x1254` | `1:1@2k` (2048×2048) | `1:1@4k` (2880×2880) |
| 16:9 landscape | `1672x941` | `16:9@2k` (2048×1152) | `16:9@4k` (3840×2160) |
| 9:16 portrait | `941x1672` | `9:16@2k` (1152×2048) | `9:16@4k` (2160×3840) |
| 3:2 / 4:3 / ultra-wide (3.09:1) | `1536x1024` / `1448x1086` / `2206x713` | `3:2@2k` / `4:3@2k` | `3:2@4k` / `4:3@4k` |

## Quality, background, input fidelity, and masks
- `--quality` works for `generate`, `edit`, and `generate-batch`, folded into the prompt (`high` → `Quality: high quality, rich detail`, `low` → `Quality: keep it simple`, `medium`/`auto` → none).
- `--background transparent` appends a transparency requirement to the prompt and keeps PNG alpha (complex subjects: fall back to `scripts/remove_chroma_key.py`). `--background opaque`/`auto` are no-ops.
- `--input-fidelity` is **edit-only**, validated as `low|high`, then ignored (`gpt-image-2` always uses high fidelity).
- `--mask` is **edit-only** and uploaded as a multipart part.

Mask notes:
- For multi-image edits, pass repeated `--image` flags; their order is meaningful, so reference each image by index and role in the prompt.
- Image and mask must be under 50MB and should share size/format; masks should be PNG with an alpha channel.
- If multiple input images are provided, `--mask` applies to the first image.
- Masking is prompt-guided; describe the edit region and repeat invariants (`change only X; keep Y unchanged`) in the prompt.

## Output handling
- Use `tmp/imagegen/` for temporary JSONL inputs or scratch files; use `output/imagegen/` for final workspace outputs.
- Target file conflicts (without `--force`) and directory write permissions are checked **before** making any paid API request.
- All outputs, sprites, and JSON reports are written atomically (`tempfile` + `os.replace`).
- `--out-dir` changes one-off naming to `image_1.<ext>`, `image_2.<ext>`, and so on; downscaled copies use `-web`.
- Animated GIFs (`GIF87a`/`GIF89a`) force `.gif`, write upstream bytes verbatim (skipping Pillow resample/re-encode), and save `<name>-sprite.png` if supplied.
- `--report-json` records actual written paths (including `.gif` switches, extra web-pipeline assets, `sprites`, `downscaled`, and PNG/GIF/JPEG/WebP `pixels`).

## Common recipes

Generate with augmentation fields + 2K preset:

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "A minimal hero image of a ceramic coffee mug" `
  --use-case "product-mockup" `
  --style "clean product photography" `
  --composition "wide product shot with usable negative space for page copy" `
  --constraints "no logos, no text" `
  --preset 16:9@2k `
  --out output/imagegen/mug-hero.png
```

Generate strictly 4 variants on the web pipeline (`--strict-n` runs 4 sequential `n=1` calls):

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate `
  --prompt "Four cozy lantern icons" --n 4 --strict-n --out output/imagegen/lantern.png
```

Edit with reference image + multi-line prompt file:

```powershell
& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" edit `
  --image "input/character.png" `
  --prompt-file "tmp/imagegen/brief.txt" `
  --out "output/imagegen/character-card.png"
```

Batch (JSONL, `utf-8-sig` PowerShell BOM safe) with resume support:

```powershell
New-Item -ItemType Directory -Force tmp/imagegen, output/imagegen/batch | Out-Null
@'
{"prompt":"Cavernous hangar interior with a compact shuttle parked near the center","use_case":"stylized-concept","size":"1672x941"}
{"prompt":"Gray wolf in profile in a snowy forest","use_case":"photorealistic-natural","size":"1254x1254"}
'@ | Set-Content tmp/imagegen/prompts.jsonl -Encoding UTF8

& "$env:USERPROFILE\.codex\skills\imagegen-relay\bin\run.cmd" generate-batch `
  --input tmp/imagegen/prompts.jsonl `
  --out-dir output/imagegen/batch `
  --concurrency 2 `
  --skip-existing `
  --report-json tmp/imagegen/batch-report.json
```

Notes:
- `generate-batch` requires `--out-dir`; per-job overrides support `model`, `n`, `size`, `quality`, `background`, `output_format`, `output_compression`, `moderation`, `out`, and all 11 augmentation fields (flat or under `fields`).
- Batch strips `mssk_conv` (each job runs independently) and writes ordered success + failure records to `--report-json`.

## Relay-only flags

| Flag | Purpose |
| --- | --- |
| `--base-url URL` | Override relay URL for one run |
| `--timeout SECONDS` | Per-request HTTP timeout (auto: `150` normal / `300` heavy) |
| `--deadline SECONDS` | Hard per-job wall-clock budget shared across all retries (auto: `240` normal / `480` heavy) |
| `--max-attempts N` | Transient retries (default `3`; >=60s render timeouts capped at `1`) |
| `--retry-refusal N` | Retry stochastic output-stage safety refusals with neutral variations (default `0`; skips `stage=input`) |
| `--preflight off\|auto\|always` | Coverage preflight (default `off`; see `references/safety-refusals.md`) |
| `--strict-n` | On `generate` with `gpt-image-2` and `--n > 1`, run `n` sequential `n=1` calls |
| `--skip-existing` | On `generate-batch`, skip already-completed non-empty outputs |
| `--preset RATIO@2k\|4k` / `--exact-size WxH` / `--fit` | Ratio hint + exact local Lanczos resample |
| `--web-mode chat\|work` / `--effort TIER` / `--agent-model SLUG` | Web-pipeline controls (`chat` 3 tiers: `none/standard/extended`, slugs `gpt-6-thinking`/`gpt-5-6-thinking` or `-instant`; `work` 5 tiers: `min/standard/extended/xhigh/max`, legacy `minimal` auto-maps to `min`, `work` + `--effort none` rejected; `--agent-model auto` overrides config back to auto) |
| `--conv-key NAME` / `--new-conv` | Pin or rotate `<cwd>/.imagegen/conv-<hash>` (persisted after live call succeeds) |
| `--gif-speed 0.25..2` | GIF playback speed multiplier (`animation.frame_duration_ms` only) |
| `--check-model` / `--strict-size` / `--report-json PATH` | Model probe / official hard size validation / structured report |

## See also
- `references/relay-api.md` — wire contracts, `style.mssk_*`, GIF JSON schema, tier mechanics
- `references/safety-refusals.md` — safety gates, ablation data, `--preflight`, browser-vs-CLI troubleshooting
- `references/prompting.md` & `references/sample-prompts.md` — shared prompting principles & recipes
- `references/codex-network.md` — network/sandbox notes for this installation
