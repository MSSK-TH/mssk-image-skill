# Relay API quick reference

This file describes the MSSK relay image routes that `scripts/image_gen.py` talks to.
It replaces the official skill's `references/image-api.md`, which documents the OpenAI-hosted
Image API (`api.openai.com`) and therefore does not apply here.

Read it when you need the exact wire contract, the measured behaviour of this relay, or the
error vocabulary used by `new-api`.

## Endpoint resolution

The CLI resolves credentials in this order:

1. `config.json` in the skill root (`api_url`, `api_key`) -- the normal path.
2. Environment variables: `IMAGE_RELAY_URL` / `IMAGE_RELAY_KEY`,
   then `CODEX_API_URL` / `CODEX_API_KEY`, then `OPENAI_BASE_URL` / `OPENAI_API_KEY`.
3. Built-in default: `https://relay.msskwithth.com:9443/v1`.

`--base-url` overrides the URL only; the token always comes from `config.json` or the environment.
A URL without a trailing `/v1` gets `/v1` appended automatically.

The certificate on `relay.msskwithth.com:9443` is a valid Let's Encrypt certificate, so no
`-k` / `--insecure` / certificate-bypass flag exists or is needed.

## Routes

| Route | Method | Body | Auth |
| --- | --- | --- | --- |
| `/v1/responses` | POST (**`stream: true` required**) | JSON | `Authorization: Bearer <token>` |
| `/v1/images/generations` | POST | JSON | `Authorization: Bearer <token>` |
| `/v1/images/edits` | POST | `multipart/form-data` | `Authorization: Bearer <token>` |
| `/v1/models` | GET | -- | `Authorization: Bearer <token>` |

## Responses route (the tool route)

`POST /v1/responses` with `stream: true` (without it the relay answers 400 `Stream must be set to
true`), a chat model (`gpt-6-astra`, `gpt-5.6-sol`), and the hosted image tool:

```json
{
  "model": "gpt-6-astra",
  "input": [{"role": "user", "content": [
    {"type": "input_text", "text": "<prompt>"},
    {"type": "input_image", "image_url": "data:image/png;base64,..."}
  ]}],
  "tools": [{"type": "image_generation"}],
  "store": false,
  "stream": true
}
```

The image arrives in an SSE event as an `image_generation_call` item with a `status` of
`in_progress` -> `generating` -> `completed` (or `failed`) and a base64 `result`. Measured event
types: `response.created`, `response.output_item.added`, `response.image_generation_call.*`,
`response.output_text.delta`, `response.output_item.done`, `response.completed`, plus `keepalive`
frames during long renders.

Practical notes:

- Latency: 42 s (reference card render) to 135 s (when the tool refuses and two models are tried).
- The tool chooses its own output geometry (measured 1024x1536 for a portrait reference).
- `--size` / `--quality` / `--background` belong to the tool definition here, not to the Images
  route parameters.
- When the tool reports `failed`, the assistant usually writes an explanation; the CLI relays it
  verbatim.

## Generate request

Only these fields go on the wire. Anything else the official CLI accepts is resolved locally
(see "Locally resolved controls" below) instead of being sent, so the relay can never reject
the request because of an unknown parameter.

```json
{
  "model": "gpt-image-2",
  "prompt": "<final prompt, augmentation already applied>",
  "n": 1,
  "size": "1672x941"
}
```

- `size` is **omitted** when it is `auto`; the relay then renders 1:1.
- `n` is 1-10 (variants of one prompt).

## Edit request

Multipart fields: `model`, `prompt`, `n`, plus one repeated `image` part per input image and an
optional `mask` part. `size` is not sent: an edit follows the input image's aspect ratio.

## Response

```json
{
  "created": 1792120342,
  "data": [
    {
      "b64_json": "iVBORw0KGgo...",
      "revised_prompt": "a single flat red circle ...",
      "size": "1254x1254"
    }
  ]
}
```

- `data[].b64_json` is the normal path; `data[].url` is also supported by the CLI.
- `data[].size` is the **actual** output size -- report it verbatim (`actual_size`).
- `data[].revised_prompt` has been observed to echo the request, not to rewrite it. When the CLI
  reports a `revised_prompt` that differs from what it sent, trust the reported one.

## Measured behaviour (this relay, 2026-10-16)

| Observation | Value |
| --- | --- |
| Model served | exactly one: `gpt-image-2` (`supported_endpoint_types` includes `image-generation`) |
| Generate latency | 24.6 s for one image (`n=1`) |
| Edit latency | 21.3 s for one image |
| Output pixels | ~1.57 MP, fixed. `size` selects the **aspect ratio**, not the pixel count |
| `size: 1024x1024` produced | 1254 x 1254 |
| Quality knob | not honoured upstream; folded into the prompt by this CLI |
| True transparency | not available as a parameter; emulated in the prompt (measured 46-52% fully transparent pixels) |
| Total models exposed | 222 (`GET /v1/models`) |

### Aspect ratio -> `size` cheat sheet

| Wanted ratio | Pass `--size` |
| --- | --- |
| 1:1 square / avatar | `1254x1254` |
| 16:9 widescreen | `1672x941` |
| 9:16 vertical | `941x1672` |
| ultra-wide (3.09:1) | `2206x713` |
| 3:2 | `1536x1024` |
| 4:3 | `1448x1086` |

`1K` / `2K` / `4K` are accepted by the relay but only fold to a 1:1 or 16:9 shape; the pixel count
stays ~1.57 MP. **This relay can never produce 4K.** The official gpt-image-2 size constraints
(multiples of 16, ratio <= 3:1, the 655,360-8,294,400 pixel window) are validated by the CLI as
semantics only -- pass `--strict-size` to restore the official hard failure.

## Locally resolved controls

| Flag | What actually happens |
| --- | --- |
| `--quality high` | appends `Quality: high quality, rich detail` to the prompt |
| `--quality low` | appends `Quality: keep it simple` to the prompt |
| `--quality medium` / `auto` | nothing is appended |
| `--background transparent` | appends a transparency requirement line to the prompt and keeps PNG output |
| `--background opaque` / `auto` | no-op |
| `--output-format jpeg/webp`, `--output-compression` | local re-encode after the response (needs Pillow) |
| `--moderation` | validated, then ignored (relay-controlled) |
| `--input-fidelity` | validated, then ignored (gpt-image-2 always uses high fidelity) |
| `--downscale-max-dim` | local downscale copy with `-web` suffix (needs Pillow) |

## Errors

`new-api` returns errors in this shape:

```json
{"error": {"code": "", "message": "...", "type": "new_api_error"}}
```

| Status / message | Meaning | CLI behaviour |
| --- | --- | --- |
| `401` `Invalid token` | token deleted, expired, or wrong | fatal, with instructions to update `config.json` |
| `503` `No available channel for model X under group auto (distributor)` | the relay has no channel for that model | fatal; only `gpt-image-2` is served |
| `400` `invalid JSON request body` | malformed request (a CLI bug, not a user error) | fatal |
| `上游内容安全系统拒绝：<text>` | upstream content-safety decision | fatal, no local retry. **Relay `<text>` to the user verbatim; do not rewrite the prompt on your own.** Measured: the decision follows the **input image**, the relay retries twice internally (so a refusal costs ~120 s), and an identical request returns the identical refusal text and request ID |
| `429`, `408`, `5xx`, network timeouts | transient | retried up to `--max-attempts` (default 3) with backoff; one job is additionally capped by `--deadline` (default 600 s) |

The CLI never retries a content-safety refusal and never softens a user's description by itself.

### Content-safety refusals in detail (measured 2026-10-16)

| Experiment | Result |
| --- | --- |
| `/images/edits`, original prompt + character photo, sent twice | identical refusal text **and identical request ID** -- deterministic, cached upstream (6.6 s the second time) |
| `/images/edits`, thoroughly reworded prompt + same photo | refused again, **new** request ID (`482d646d...`), ~123 s (relay retried internally twice) |
| `/images/edits`, head-and-shoulders crop of the same photo | refused (~130 s); upstream text suggests "提高服装覆盖度、弱化胸部强调" |
| `/images/generations` + `image` field (data URI, bare base64, array) | not supported -- HTTP 500 `do_request_failed` |
| `/responses` + `{"type":"image_generation"}` tool, `gpt-6-astra` and `gpt-5.6-sol` | tool call returns `status=failed` (~135 s / ~105 s); the model explains the reference was judged sexualised |
| `/responses` + 512 px JPEG + `detail: low` / 768 px + `detail: low` | `status=failed` (~51 s / ~68 s) |
| Person-free prompts (product shot, landscape, empty card frame) | pass on both routes |

So on the API surface of this relay, neither rewording the prompt nor shrinking, cropping or
downgrading the reference changes the outcome for this class of photo. The refusal is an upstream
policy decision, and it is attached to the picture.

When a user reports that the same reference and prompt succeed in a **browser client**, the
difference has to be in the request or the routing, not in the prompt. Collect one of:

- the browser's working request (DevTools -> Network -> *Copy as cURL*), or
- the relay's request log for that call (endpoint, model, token group, channel).

`new-api` routes by token group, so the same model name can resolve to different upstream channels
with different safety gates.

## Diagnostics

- `models` subcommand: lists the image-capable models the relay exposes.
- `--check-model`: verifies the model against `/v1/models` before spending a generation.
- Timing line on stderr: `relay+render`, `download`, `local`, `total` seconds.
- `--report-json <path>`: writes model, endpoint, prompt, outputs, `actual_size`, pixels,
  `revised_prompt`, and the timing breakdown.

## Security

- The token lives in `config.json` (or an environment variable). It is never printed, never
  echoed into a report, and never written to a log.
- If a token ever appears in relayed error text, redact it before showing it to the user.
