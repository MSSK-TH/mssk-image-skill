# Relay API quick reference

This file describes the MSSK relay image routes that `scripts/image_gen.py` talks to.
It replaces the official skill's `references/image-api.md` (which targets `api.openai.com`).

## Endpoint & Credential Resolution

1. `config.json` in the skill root (`api_url`, `api_key`) — normal path.
2. Environment variables: `IMAGE_RELAY_URL` / `IMAGE_RELAY_KEY` → `CODEX_API_URL` / `CODEX_API_KEY` → `OPENAI_BASE_URL` / `OPENAI_API_KEY`.
3. `--base-url` overrides the URL only; a URL without trailing `/vN` gets `/v1` appended automatically.
4. Both `api_url` (`http://` or `https://`) and `api_key` are required before any live call (missing either fails fast cleanly).

The relay certificate is verified normally; there is deliberately no `--insecure` flag.

## Routes & Pipelines

| Route | Method | Body | Auth |
| --- | --- | --- | --- |
| `/v1/images/generations` | POST | JSON | `Authorization: Bearer <token>` |
| `/v1/images/edits` | POST | Codex JSON or OpenAI `multipart/form-data` | `Authorization: Bearer <token>` |
| `/v1/responses` | POST (**`stream: true` required**) | JSON (SSE response) | `Authorization: Bearer <token>` |
| `/v1/models` | GET | — | `Authorization: Bearer <token>` |

### Two image model slugs (execution layer routing)

- **`gpt-image-2`** (default): web session pipeline first (`backend-api/f/conversation` + `system_hints:["picture_v2"]`, ~25–45 s), automatically falls back to the native subscription endpoint if the web attempt fails. On fallback, the response includes `degraded: true` and `degraded_reason`.
- **`codex-gpt-image-2`**: native subscription endpoint only (`/backend-api/codex/images/*`, ~15–30 s, stricter safety gate, no fallback).

### Why `image_generation` built-in tool does not work on custom providers (verified 2026-10-16)

Four `codex exec` captures against a custom provider `base_url` confirmed that the Codex client omits the hosted `image_generation` tool from `/v1/responses` whenever a custom/relay provider is active. Neither a relay rewrite nor a translation shim restores the built-in tool; this CLI is the supported image path.

## Web-Pipeline Controls (`style.mssk_*` & Multipart `web_*`)

`new-api` deserializes JSON image requests into a strongly-typed struct and **silently drops unknown top-level JSON fields**. Only `style` (`json.RawMessage`) passes through untouched to the execution layer:

```json
{
  "model": "gpt-image-2",
  "prompt": "...",
  "n": 1,
  "style": {
    "mssk_conv": "skill-a1b2c3d4e5f6",
    "mssk_mode": "work",
    "mssk_effort": "extended",
    "mssk_model": "gpt-6-astra-wm",
    "mssk_gif_speed": "1.5"
  }
}
```

| JSON (`style.*`) | Multipart `/v1/images/edits` field | Meaning |
| --- | --- | --- |
| `mssk_conv` | `web_conv_key` | Conversation key mapped to upstream `conversation_id` + `parent_message_id` (avoids new-conversation rate limit) |
| `mssk_mode` | `web_mode` | Surface: `chat` (default) or `work` |
| `mssk_effort` | `web_effort` | Reasoning tier → `thinking_effort` (`none` MUST be omitted, never sent as `"none"`) |
| `mssk_model` | `web_model` | Upstream reasoning model slug (omitted for `auto`) |
| `mssk_gif_speed` | `gif_speed` | GIF playback speed multiplier (`0.25`..`2`, scales `animation.frame_duration_ms` only) |

### Surface & Tier Mechanics (`webpipe.mjs`, verified 2026-11-23 against live `/backend-api/models`)

| | `chat` surface | `work` surface |
| --- | --- | --- |
| UI tiers | 3: `即时` / `中` / `高` | 5: `轻度` / `中` / `高` / `极高` / `最高` |
| What changes | Preset slug per lane: `gpt-6-instant` / `gpt-6-thinking` and `gpt-5-6-instant` / `gpt-5-6-thinking` (hyphen `5-6`, no `-sol`) | Slug stays in the 10-model `-wm` family (`gpt-6.2-astra-wm` … `gpt-5.6-luna-wm`); only `thinking_effort` changes |
| `effort` values | `none` (omitted, for `-instant`) / `standard` / `extended` | `min` / `standard` / `extended` / `xhigh` / `max` (no `none` tier; upstream returns `422` on `"minimal"`, which the CLI auto-maps to `min`) |
| Same tiers across models? | Yes | Yes (all 10 `-wm` models share the exact 5 tiers) |

Because `/web/models` on the execution layer is a top-level route and `new-api` only forwards `/v1/*`, the CLI cannot query the account's live web-model list; candidate slugs in `tiers` are calibrated from real upstream `/backend-api/models` captures. When the web pipeline fails and auto-falls back to native, `degraded_reason` exposes the exact cause (`prepare 没返回 conduit_token` → invalid/mismatched slug; `422 Invalid conversation body` → invalid `thinking_effort`; `webpipe 403` / Cloudflare / Sentinel → risk control) and is recorded as a standalone `"degraded_reason"` field in `--report-json`.

## Edit Contracts: OpenAI Multipart vs Codex JSON

- **OpenAI Images contract** (`new-api` & `image-web` default): `multipart/form-data` with repeated `image` file parts, optional `mask`, `background`, and top-level `web_conv_key` / `web_mode` / `web_model` / `web_effort` / `gif_speed` fields (all passed untouched through `new-api` with zero base64 inflation).
- **Codex JSON contract** (`codex-rs/codex-api/src/endpoint/images.rs`): JSON body with `images: [{"image_url": "data:image/...;base64,..."}]` + `style.mssk_*`.

`edit` uses `openai-multipart` first by default (matching `image-web`, skipping base64 encoding, and preserving conversation continuity & model selection through `new-api`), falling back lazily to `codex-json` only if a gateway rejects multipart with HTTP 400/415. You can pin `"edit_contract": "openai-multipart"` or `"codex-json"` in `config.json`.

## Responses Route (`reference` subcommand)

`POST /v1/responses` requires `"stream": true` (otherwise HTTP 400 `"Stream must be set to true"`), a chat model (`gpt-6-astra`, `gpt-5.6-sol`), `"tools": [{"type": "image_generation"}]`, and optional `"reasoning": {"effort": "low|medium|high|xhigh"}` (never use `-high` model-name suffixes, which fail with HTTP 500).

SSE events carry `image_generation_call` items (`in_progress` → `generating` → `completed` / `failed` with base64 `result`) plus periodic `keepalive` frames during long renders. The CLI enforces `--deadline` across keepalive frames so stalled streams abort cleanly. Note that `/v1/responses` passes through without reading `style.mssk_*`.

## Response Formats (Static, Multi-Asset & Animated GIF)

Standard response:

```json
{
  "created": 1792120342,
  "data": [
    {
      "b64_json": "iVBORw0KGgo...",
      "revised_prompt": "...",
      "size": "1254x1254"
    }
  ]
}
```

Animated GIF response item (verified 2026-11-06 & 2026-11-19):

```json
{
  "b64_json": "...",
  "mime_type": "image/gif",
  "sprite_sheet": "...",
  "animation": {"rows": 3, "columns": 3, "frame_count": 9, "frame_duration_ms": 120}
}
```

- `sprite_sheet` (when attached by upstream) comes from `parts[0].metadata.generation`; the CLI writes `<name>-sprite.png` alongside the `.gif`.
- GIF bytes are written **verbatim** (never re-encoded via Pillow, which would drop frames).
- One turn can emit multiple `data[i]` assets (especially when `--n > 1` on the web pipeline, which drains the full turn). The CLI writes every entry (`foo.png`, `foo-1.png`… or `foo-1.png`, `foo-2.png`, `foo-3.png`…) and records all actual written paths in `--report-json`.

## Resolution & Locally Resolved Controls

Upstream always renders at **~1.57 MP** per aspect ratio (`1:1 -> 1254x1254`, `16:9 -> 1672x941`, `9:16 -> 941x1672`, `3:2 -> 1536x1024`, `4:3 -> 1448x1086`, ultra-wide `3.09:1 -> 2206x713`).

| Flag | Behaviour |
| --- | --- |
| `--preset <ratio>@2k\|4k` / `--exact-size WxH` | Sends ratio via `size`, then locally Lanczos-resamples to exact delivered pixels (all 11 ratios × 2K/4K, max 8,294,400 px) |
| `--quality high\|low` | Appends `Quality: ...` hint to prompt (`medium`/`auto` = no-op) |
| `--background transparent` | Appends transparency requirement to prompt and preserves PNG alpha |
| `--output-format jpeg\|webp`, `--output-compression` | Single-pass local encode via Pillow |
| `--downscale-max-dim` | Writes additional `-web` downscaled copy via Pillow |
| `--moderation`, `--input-fidelity` | Validated locally, ignored upstream |

## Error Vocabulary & Classification

All errors are classified in `scripts/relay_errors.py` (single source of truth) into machine-readable `CODE=` / `RETRYABLE=` / `HINT=` / `RAW=` (with automatic `sk-*` / `Bearer` secret redaction). For detailed content-safety refusal behavior, ablation measurements, and browser-vs-CLI troubleshooting, see `references/safety-refusals.md`.
