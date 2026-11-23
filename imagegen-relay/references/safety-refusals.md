# Content-Safety Refusals, Preflight & Browser-vs-CLI Diagnosis

Read this file when an image request is refused by upstream content safety (`CODE=content_refusal` / `CODE=content_safety`), when tuning `--preflight` or `--retry-refusal`, or when the user reports "it worked in the browser client but failed here".

---

## 1. Two Safety Gates (and How Latency Tells Them Apart)

| Gate | What is judged | Latency | Deterministic? |
| --- | --- | --- | --- |
| **Input / prompt level** (`stage=input`) | The prompt text or input reference image | HTTP 400 in a few seconds | Stable — always refused until offending text/image changes |
| **Output-image level** (`stage=output`) | The rendered image after generation | Same as a normal render (25–140 s) | **Stochastic** — the same intent can pass on another render |

Why a browser client can pass the same request: **the web app is an agent**. Its chat model rewrites and expands the user prompt first — typically adding clothing, setting, and lighting constraints — before the image renderer sees it. A direct API call sends the prompt verbatim, so an unconstrained render can drift over the output classifier's boundary.

---

## 2. Measured Ablation Data (Same Reference Image, Same Channel)

Measured on a refused baseball-card character edit (same reference photo, same composition):

| Variant | Result |
| --- | --- |
| Original agent-written long "keep every detail" brief | Refused (~106 s) |
| Original + `服装与图1完全一致，为覆盖完整、不透明的运动套装；衣着保守、得体` | **PASS (93 s)** |
| Original + `穿着图1中的完整运动套装（外套、上衣与短裤），覆盖完整不透明` | **PASS (142 s)** |
| Control: original with one unrelated adjective swapped | Refused (~106 s) |

Multi-run pass rates on the same reference photo / channel:

| Brief style | Sample | Observed pass rate |
| --- | --- | --- |
| Long "keep every detail" + garment enumeration | 0 pass / 5 refusals | ~0% |
| Compact neutral brief + one coverage line (keeping original outfit) | 6 pass / 17 attempts | ~35% (~70% across 3 tries) |
| Compact brief allowing render to cover outfit up (no "keep outfit identical") | 1 pass / 1 attempt (53.7 s) | High |

Key takeaways:
- **Re-sending an identical body hits upstream cache** and returns the exact same refusal and request ID (measured: 6.6 s on duplicate send). `--retry-refusal N` appends semantically neutral render variations (`_REFUSAL_VARIATIONS`) so each attempt produces a fresh render.
- **Rewriting beats patching**: appending a coverage line onto an already-long "keep every detail" brief was still refused (153 s), whereas rewriting to the 5–7 line compact neutral brief passed cleanly.
- **Never soften or alter the user's outfit silently**: if keeping the exact outfit fails repeatedly, tell the user honestly and let them choose between a more conservative reference photo or allowing fuller clothing coverage.

---

## 3. Step-by-Step Refusal Playbook

1. **Check `CODE` and latency**:
   - Fast failure or `判定阶段=input` → input-stage refusal: fix the offending word or swap the reference image (retrying verbatim will always fail; `--retry-refusal` intentionally skips `stage=input`).
   - Slow failure (tens of seconds) or `判定阶段=output` → output-stage stochastic refusal: proceed to step 2.
2. **Rewrite to the compact neutral brief** (5–7 lines, zero garment/body vocabulary, one `Clothing:` consistency/coverage line — see `SKILL.md` Section 5).
3. **Optional retry**: pass `--retry-refusal 1` (or `2`) deliberately. Remember each retry costs a full render and shares the job's single `--deadline`.
4. **Never fake it**: do not substitute local PIL compositing and call it image generation.
5. **Relay upstream text verbatim** if still refused, explaining it is an upstream policy decision the skill cannot override.

---

## 4. Coverage Preflight (`--preflight`, off by default)

Default is `--preflight off`: the CLI sends your prompt byte-for-byte unchanged. Enable deliberately when desired:

- `--preflight auto`: triggers only when the prompt mentions a person/character (word-boundary matched, strips negated mentions like `"no people"` / `"避免其他人物"`, leaves `"surface"`, `"hero image"`, `"3D model"` untouched).
- `--preflight always`: applies to every request.

When triggered, it logs every modification to stderr (`[preflight] ...`) and sets `coverage_preflight_applied: true` in `--report-json`:
1. If the user explicitly locked the outfit (`服装保持不变` / `keep outfit unchanged`), it leaves the wording intact and appends only a neutral curve-de-emphasis line.
2. Otherwise, it neutralizes high-risk garment/body/suggestive terms and appends one explicit clothing/coverage constraint in the prompt's language.

Preflight is cheap first-line insurance, **not** a substitute for a compact brief.

---

## 5. "It Works in the Browser Client, Not Here"

Do not guess. Ask for **one** concrete artifact before changing anything in the skill:

1. **Browser working request**: DevTools → Network → the image request → *Copy as cURL* (or URL + payload).
2. **Relay request log** for the successful call: endpoint, model, token group, and routed channel.

The cause is almost always one of three things:
- Different endpoint (browser chat agent rewrites prompt / uses `picture_v2` web session vs direct native `/images/edits`),
- Different upstream channel behind the same model name (`new-api` routes by token group),
- Different reference image or aspect ratio.
