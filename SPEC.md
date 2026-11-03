# SPEC —— 生图链路与 `imagegen-relay` 技能

> 版本：v1.2.0 · 2026-10-16
> 本文是**行为规格**：链路、参数语义、错误语义、不变量、验收测试。
> 实现细节看 `imagegen-relay/scripts/image_gen.py`；服务端（shim/网页管线）在私有仓 `MSSK-TH/mssk-image-relay`；历史决策与实测数据看 `交接-生图链路与技能-v2.md`。

---

## 1. 路由矩阵（客户端命令 → 上游调用）

| 命令 | 客户端请求 | 中转站落点 | 上游实际调用 | 谁写提示词 |
|---|---|---|---|---|
| `generate` | `POST /v1/images/generations`（JSON） | 渠道 3 → shim | `POST {chatgpt}/backend-api/codex/images/generations` | 调用方（逐字） |
| `edit` | `POST /v1/images/edits`（multipart；支持多图） | 渠道 3 → shim | `POST …/codex/images/edits`，体为 `{"images":[{"image_url":…},…],"prompt":…,"model":…}` | 调用方（逐字） |
| `reference` | `POST /v1/responses` + `tools:[{"type":"image_generation"}]` | 渠道 1 | `POST …/codex/responses`（对话模型先产出 tool call） | **对话模型**（会改写） |
| `generate-batch` | 内部循环 `generate` | 同 `generate` | 同 `generate` | 同 `generate` |

**默认选择规则**：带参考图 → `edit`；无图 → `generate`；只有"想让模型自己发挥/图像路不可用"时才 `reference`。

**回退链**：shim 内部，原生端点出现**基础设施类**失败（401/403/404/5xx/网络/无凭据）→ 自动回退 `/responses` + 图像工具；**内容安全拒绝不回退**（那是确定答案）。

---

## 2. 参数语义（含"哪些是无效的"）

| 参数 | 语义 | 上游是否生效 |
|---|---|---|
| `--prompt` / `--prompt-file` | 提示词 | ✅ 逐字送达（走 `edit`/`generate`） |
| `--image`（可多次） | 参考图，顺序即"参考图1、参考图2…" | ✅ 多张已支持（原生契约 `images[]`） |
| `--size WxH` | 期望尺寸 | ⚠️ **仅比例生效**：shim 把它翻译成提示词里的比例要求（16:9 → 精确 1672×941）；像素被忽略 |
| `--exact-size WxH` | **交付文件的精确像素**（本地重采样） | ✅ 技能侧保证（`--fit cover` 裁切填满 / `contain` 补边） |
| `--preset 16:9@2k` | 比例预设（11 比例 × 2k/4k）：设比例提示 + 精确交付像素，**调用前校验** 16px 对齐/最大边/总像素/冲突参数 | ✅ 技能侧（一次说清"16:9 的 2K"） |
| `--n N` | 一次出 N 张（**同一进程、一条命令**） | ✅ shim 循环实现（上游 `n` 被忽略） |
| `--background transparent` | 透明底 | ✅ 原生端点支持（返回 RGBA）；老路是 400 |
| `--quality` | 质量档 | ❌ 上游忽略 |
| `--n` | 一次多张 | ❌ 上游忽略（shim 循环实现） |
| `mask`（edits） | 局部重绘 | ❌ 当前 shim **收到但不转发**（未验证原生契约） |
| `--retry-refusal` | 被拒后换措辞重发 | 技能侧开关，**默认 0** |
| `--preflight` | 提示词预处理 | 技能侧开关，**默认 off** |

**内容安全参数**：`moderation: "low"` **实测无效**（原生端点接受该字段但结果不变；官方客户端无此开关）。不要在文档里承诺它能降低拒绝率。

**流式（SSE）**：shim 对上游统一使用 `stream=true` + `Accept: text/event-stream`。
- 目的**不是**草稿帧（gpt-image-2 不产草稿），而是 **keepalive + 抗超时**：首字节 ~0.3s（非流式 14s+），中间链路不会把长任务当"卡死"掐掉。
- **超时语义**：`NATIVE_IDLE_TIMEOUT_MS`（默认 120s，**每收到一块数据就重置**）+ `NATIVE_WALL_MS`（默认 900s 硬上限）。
  固定超时会误杀慢图（4K/复杂构图 >240s），所以不要改回固定超时。
- 兼容：若上游仍回 JSON，shim 自动按非流式解析。

---

## 3. 错误语义

| 场景 | HTTP | 体 |
|---|---|---|
| 内容安全拒绝（原生端点） | `400` | `{"error":{"message":"上游内容安全系统拒绝（原生端点）：判定阶段=output，类别=sexual，请求ID=…","type":"image_generation_user_error","code":"moderation_blocked"}}` |
| 内容安全拒绝（老路回退） | `502`→ 由技能映射 | 上游原文透传 |
| 上游基础设施故障 | `502` | `{"error":{"message":…,"type":"upstream_error"}}` |
| 鉴权失败 | `401` | `invalid api key` |

**客户端（技能）行为**：`moderation_blocked` → 一行中文 `Error: …`；其它上游错误 → 一行 `Error: …`；**不打印 traceback**。

**面向用户的措辞（2026-10-16 起，两端一致）**：上游的英文代号会翻成中文再给用户看，原始代号只留在 `raw` / 站点详情 / 日志里给管理员排查。

| 上游原文 | 用户看到 |
| --- | --- |
| `moderation_stage=output` | 图已经画好，只是被判掉 |
| `moderation_stage=input` | 提示词/参考图阶段就被拦下 |
| `categories=[sexual]` | 「性化/暴露」 |
| `violence` / `self-harm` / `harassment` / `hate` / `minors` | 暴力 / 自残 / 骚扰 / 仇恨 / 未成年人相关 |
| `I couldn't generate this image…`（英文原话） | 上游图像安全系统判定这张图/这条提示词不合规，没有生成 |

示例：`Error: 图片被上游安全系统判定为「性化/暴露」内容，没有出图（图已经画好，只是被判掉） · 请求 ID：93328358-…`

---

## 4. 不变量（改动时必须保持）

1. **与官方 imagegen 对齐**：`references/prompting.md`、`references/sample-prompts.md`、`scripts/remove_chroma_key.py`、`LICENSE.txt`、两个图标 —— **逐字节一致**；`parity_check.py` 必须全过。
2. **`config.json` 不入库明文密钥**（仓库里放占位版）。
3. **shim 只读凭据，绝不刷新** OAuth（refresh_token 一次性；谁刷新谁必须回写，那是 new-api 的职责）。
4. **不假装支持**：`mask`、`n>1`、`quality`、精确 `size` 参数 —— 不支持就写"不支持"。
5. **失败要快**：内容安全拒绝不回退、不重试（原生路 ≈27s 给出结构化答案）。

---

## 5. 验收测试

| 测试 | 命令 | 期望 |
|---|---|---|
| 与官方对齐 | `python _probe\parity_check.py` | `ALL PARITY CHECKS PASSED` |
| 文生图 + 精确尺寸 | `run.cmd generate --prompt "…" --exact-size 1024x1536 --out x.png` | 交付文件恰为 1024×1536 |
| 透明底 | `run.cmd generate --prompt "…" --background transparent --out x.png` | PNG 为 RGBA，alpha 有 0 与 255 |
| 多图 | `run.cmd edit --image A.png --image B.png --prompt-file b.txt --out x.png` | shim 日志出现 `images=2` |
| 回退 | `NATIVE_IMAGES=0` 后跑一次 `edit` | 仍出图，日志出现 `↩️ 回退 /responses` |
| 内容安全基线 | `python _probe\shim-work\baseline5.py` | 当前约 1/5（参考图与结论见交接文档） |

---

## 6. 已知边界（不要承诺）

- 被判定为"性感化"的参考图/渲染：**API 侧无法稳定通过**（输出层判定）；
- 精确像素**只能靠本地重采样**；
- 网页版那条会话管线**未接入**（需要真浏览器过 Cloudflare Turnstile，且用户已决定不做）；
- `imagegen-shim` 是必要组件（new-api 的 codex 渠道不实现 `/v1/images/*`）。
