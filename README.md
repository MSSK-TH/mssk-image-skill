# imagegen-relay

给 Codex 用的生图技能。装完直接说话就能出图——**不用装 Node.js / Go，不用配环境变量，不用重启客户端**。

> **前置条件只有一个：Python 3.9+ 在 PATH 里**（Windows 上是 `python` 或 `py`）。
> 除此之外不需要任何依赖，不用 `pip install`。
> 只有用 `--max-dim` / `--exact-size` / `--output-format` 这几个参数时才需要 Pillow
> （装了 `uv` 的话 launcher 会自动带上，不用手动装）。

当前版本 **v1.6.1**（2026-11-24）。

---

## 装（3 步）

### 1. 把技能装进去

```bash
git clone <仓库地址> imagegen-relay && cp -r imagegen-relay/imagegen-relay ~/.codex/skills/
# Windows: 拷到 %USERPROFILE%\.codex\skills\imagegen-relay\
```

装完目录里应该有 `SKILL.md`、`scripts/`、`references/`、`config.example.json`。

### 2. 填你自己的令牌

仓库里**没有** `config.json`（防止有人把令牌提交上去）。先复制一份：

```bash
cp imagegen-relay/config.example.json imagegen-relay/config.json
```

然后填两个字段：

```json
{
  "api_url": "https://<你的服务地址>/v1",
  "api_key": "sk-<你的令牌>"
}
```

> ⚠️ **安全**：令牌只属于你，别贴进聊天、日志、截图。
> 仓库自带一个 pre-commit 钩子会拦截"填了令牌的 config.json"：
>
> ```bash
> git config core.hooksPath .githooks    # 克隆后跑一次
> ```
>
> 也可以完全不用 `config.json`，改用环境变量（更新技能时不会丢）：
> `IMAGE_RELAY_URL` / `IMAGE_RELAY_KEY`。

检查是否配好（**不会回显令牌**）：

```bash
python scripts/image_gen.py setup --show --json
# config_exists: true, has_api_key: true  → 就绪
```

### 3. 开一个新对话

```
$imagegen-relay 生成一张海边日出的照片，要横版
```

---

## 首次配置与新对话确认（全局兜底 + 按对话生效）

技能不会盲猜你的模型和推理强度：

- **第一次使用（未配置过）**：分步单选（① 模式 → ② 选模型 → ③ 选档位，模型与档位拆为独立单选，不用输入 `(1,2)` 坐标对），选完后用 `setup` 存为全局兜底默认。
- **以后每次新对话（默认 `confirm` 策略）**：第 1 次网页生图只问 **1 句轻确认**（如果在提示词里已经点名模型/模式则直接跳过）：
  > `本次新对话生图：1) 沿用默认（模式 · 模型 · 档位）  2) 换一个`
  - 选 `1` 立刻沿用默认画图；
  - 选 `2` 按独立单选重新选模型与档位，且**只在本对话内生效**（不覆盖全局默认，除非你明确说“设为默认”）；同一对话后续轮次不再重复提问。
- 如果希望新对话完全不确认、直接静默沿用全局默认，可随时切换：
  ```bash
  python scripts/image_gen.py setup --ask-policy remember
  ```

```
（聊天）模型：1) GPT-6.2   2) GPT-6   3) GPT-5.6 Sol
（聊天）档位：1)即时(none)   2)中(standard)   3)高(extended)

（工作）模型：1) GPT-6.2 Astra  2) GPT-6.2 Sol  …  10) GPT-5.6 Luna（共 10 个 -wm 模型）
（工作）档位：1)轻度(min)  2)中(standard)  3)高(extended)  4)极高(xhigh)  5)最高(max)
```

保存/更新全局默认示例：

```bash
python scripts/image_gen.py setup --agent-model gpt-6-2-thinking --effort extended --web-mode chat
```

之后不传这三个参数时自动沿用；单次或单对话临时切换由命令行参数覆盖。

| 模式 | 档位 | 什么时候用 |
|---|---|---|
| **聊天**（3 档） | 即时(`none`) / 中(`standard`) / 高(`extended`) | 日常出图，通过率更高（`GPT-6.2` slug 为 `gpt-6-2-thinking` / `gpt-6-2-instant`；`GPT-5.6 Sol` 为 `gpt-5-6-thinking` / `gpt-5-6-instant`） |
| **工作**（5 档） | 轻度(`min`) / 中(`standard`) / 高(`extended`) / 极高(`xhigh`) / 最高(`max`) | 做卡片、网页素材、需要反复改的图 |

> 工作模式没有"即时"档（`work + --effort none` 会被拒）；旧写法 `--effort minimal` 会自动映射为 `min` 并告警。

---

## 怎么用（会说话就行）

| 你说 | 它会做 | 实测耗时 |
|---|---|---|
| 「生成一张……的照片」 | 文生图 | **25～36 秒** |
| 「把这张图做成卡牌」（附参考图） | 参考图出图 | **25～35 秒** |
| 「要透明底」 | 透明 PNG（RGBA，实测约 45% 像素透明） | 同上 |
| 「要 1024×1536 的竖版」 | 精确交付尺寸 | +1 秒 |
| 「做成动图 / 循环」 | GIF（帧数由描述决定，实测 8 帧） | 35～40 秒 |
| 「一次给我 4 个方案」 | 多图（注意下面的坑） | 可能 3～4 分钟 |

**尺寸怎么控**（三层，别混）：

| 你想要的 | 怎么传 | 效果 |
|---|---|---|
| **比例** | 说「16:9」→ `--preset 16:9@2k` | ✅ 精确（2048×1152） |
| **精确像素** | `--exact-size 1024x1536`（可选 `--fit cover\|contain`） | ✅ 交付文件就是该尺寸（本地重采样） |
| **`size` 参数本身** | — | ❌ 服务端忽略像素数，只按比例渲染 |

**其它常用参数**：

```bash
--model gpt-image-2            # 默认：会话通道优先（成功率高，失败自动切订阅通道）
--model codex-gpt-image-2      # 订阅通道直连（更快，判定更严）
--quality high|low             # 折进提示词的字样，不是真参数
--gif-speed 0.25..2            # 动图播放速度（只改帧时长）
--strict-n                     # 需要"严格 N 张"时用（拆成 N 次单张调用）
--report-json out.json         # 落一份 JSON 报告（含 pipeline / actual_size / 耗时）
```

---

## 能力与限制（都是实测，不是推测）

**能用**

- 文生图 / 改图（**单张或多张参考图**）/ 参考图生图（对话模型驱动）/ 批量（JSONL）
- **透明背景**（真 RGBA）
- **精确交付尺寸**（本地重采样）
- **动图 GIF**（原字节保存，不重编码；带帧元数据）
- **多轮改图**（同一目录连续调用即在同一个会话里继续；`--new-conv` 另起话题）
- 失败可编程：stderr 打 `CODE=` / `RETRYABLE=` / `HINT=`，`--report-json` 落结构化错误
- 每次出图打印 `[pipeline]` 行：说明走的是会话通道 / 降级 / 订阅通道，以及 surface、effort 的实际值

**不能指望**

- **内容安全**：服务端在**生成之后**判定，客户端没有任何开关能绕过。`moderation: low` 实测无效。
  被拒了就重试（每次都是真渲染、独立随机）、换一张更保守的参考图，或去网页端手动出。
- **`quality` 档位、`mask` 局部重绘**：服务端不支持（技能不会假装支持）。
- **精确像素只能本地重采样**得到（服务端每个比例只有一个固定尺寸）。
- **`--n > 1` 会多给**：会话通道会等整轮结束，把服务端自发的额外产物一并返回
  （实测要 4 张给了 10 个、含 2 个动图，耗时 221 秒）。需要严格 N 张就用 `--strict-n`。

---

## 目录里有什么

```
imagegen-relay/                  ← 一个标准 skill 目录，整体拷进 skills/ 即可
├── SKILL.md                    技能说明（agent 读这个决定怎么做事，~270 行）
├── VERSION.txt                 版本戳 + 变更记录
├── LICENSE.txt                 许可证
├── config.example.json         配置模板（复制成 config.json 后填令牌）
├── agents/openai.yaml          技能在客户端里的标题/图标/默认指令
├── bin/                        run.cmd / run.ps1 / run.sh（注入 config 后调 CLI）
├── scripts/
│   ├── image_gen.py            CLI 本体（generate / edit / reference / batch / models / tiers / setup）
│   ├── relay_errors.py         失败分类（CODE= / HINT= 的唯一来源）
│   ├── selfcheck.py            72 项零成本自检（不发真实生成请求）
│   └── remove_chroma_key.py    抠图兜底（与官方 skill 逐字节一致）
├── references/
│   ├── cli.md                  完整命令行参考
│   ├── relay-api.md            接口契约（含参数为什么必须走 style）
│   ├── safety-refusals.md      内容安全的实测数据与排障
│   ├── prompting.md            提示词写法（与官方一致）
│   ├── sample-prompts.md       示例提示词（与官方一致）
│   └── codex-network.md        网络与沙箱说明
└── assets/                     图标
```

仓库根只有 `README.md`、`.gitignore`、`.gitattributes`、`.githooks/`（防令牌误提交）。

> **`config.json` 不在仓库里** —— 它装的是你的令牌，只应存在于你本机。
> 本仓只放技能本体，不含任何服务端实现。

---

## 自检（改完代码或怀疑环境时先跑这个）

```bash
cd imagegen-relay
python scripts/selfcheck.py        # 72 项，10 秒，零成本
python scripts/selfcheck.py -v     # 带细节
```

覆盖：各命令 dry-run 冒烟、4 条路由 × 2 种契约的参数注入矩阵、tiers 两步结构、
setup 持久化与清除、`[pipeline]` 上报、11 类错误码、参数校验、BOM 处理、dry-run 零副作用。
退出码 0 = 全绿。

---

## 排障

| 现象 | 怎么办 |
|---|---|
| 出图很慢（1 分钟以上） | 可能走了回退通道。看输出里的 `[pipeline]` 行有没有 `degraded` |
| 报 `content_refusal` / `content_safety` | 内容安全判定。重试或换参考图；没有"审核开关" |
| `CODE=no_image_data` | 服务端 HTTP 200 但没给图，偶发；再发一次通常就出来 |
| `CODE=bad_request` / `webpipe_invalid_request` | 参数或 slug/档位与当前模式不匹配；跑 `tiers --mode <chat\|work>` 核对实测清单 |
| `CODE=webpipe_blocked` | 网页管线触发上游 403/风控，不要连重；稍后再试或切 `--model codex-gpt-image-2` |
| 提示 `Output already exists` | 加 `--force`，或换输出路径 |
| 完全不出图 | 先跑 `setup --show --json` 确认配置；再跑 `selfcheck.py` |
| `Python 3 was not found` | 装 Python 3.9+，或确认 `python` 在 PATH 里 |
| `SSL: UNEXPECTED_EOF_WHILE_READING` / 连不上 | 如果挂了代理：把服务域名加进 DIRECT 规则；最省事是访问本站时不挂代理 |
| `CODE=token_invalid` | 令牌填错或已禁用；检查 `config.json` 的 `api_key` |

---

## 变更记录

**v1.6.1（2026-11-24）**

- **聊天模式新增 `GPT-6.2`**：实测对齐上游真实 slug `gpt-6-2-thinking`（`standard`/`extended`）与 `gpt-6-2-instant`（`none`），小数点误写 `gpt-6.2-thinking` 提前告警并由服务端自动归一
- **彻底修复参考图改比例误切 `reference` 的路由 bug**：`SKILL.md` 移除 `reference` 主路由，任何带参考图的生图/改图（无论目标 3:4、16:9、9:16 是否与参考图同比例）一律走网页管线 `edit --image`；内联比例表补齐 `3:4` 与 `2:3`；`_edit()` multipart 同步透传显式 `size`/`quality`
- **锁死「原生仅作网页不可用后路」边界**（配合服务端 `shim/server.mjs` 与 `webpipe.mjs` 官方参考图抓包对齐）：网页管线内容安全/政策拒绝直接返回，严禁重开新对话或降级切原生；仅网页管线连接故障/不可用或用户明确点名时才走 `codex-gpt-image-2`
- 自检 77 项 → **78 项**

**v1.6.0（2026-11-24）**

- **双管线职责明确分离**：网页管线（`gpt-image-2`，默认）定位为纯直通发射器，CLI 默认 `--no-augment` 原样直发用户提示词（由远端 ChatGPT 网页大脑负责理解与构思）；原生管线（`codex-gpt-image-2`）保持自动 `--augment` 结构化，互不干扰
- **极速减轮优化**：`SKILL.md` 精简并内联 `chat`/`work` 速查表（免去生图前调 `tiers`、免去无故 `--dry-run` / 默认 `view_image`）；合并写提示词与启动为单条命令；拉高 `yield_time_ms: 120000`；`image_gen.py` 写盘后直接输出 `Wrote <绝对路径> (<宽>x<高>, <大小>KB)` 免去额外验图 shell；`run.ps1` 惰性探测 Pillow
- **对齐网页端会话续接与原地改图**（配合服务端 `shim/server.mjs` 修复）：`edit` 默认优先走 `openai-multipart`（透传 `web_conv_key / web_mode / web_model / web_effort / background` 且免去 base64 编码，遇不支持 multipart 时延迟回退 `codex-json`）；同一 Codex 对话默认对应同一网页对话，修改刚生成的图直接走 `generate` 原地追问（不重传原图 `--image`）
- 自检 72 项 → **77 项**

**v1.5.3（2026-11-23）**

- 彻底封堵 `high` / `medium` / `low` / `minimal` 档位别名触发上游 422：`high→extended`、`medium→standard`、`work` 下 `minimal/low→min`、`chat` 思考档下 `minimal/low/min→standard`（CLI 与 DMIT 服务端双端对齐）
- 模型与档位选择拆分为两道独立单选，不再合并成 `(1,2)` / `(1,3)` 坐标配对；`tiers` 新增 `--models`
- 自检 66 项 → **72 项**

**v1.5.2（2026-11-23）**

- 新对话生图策略升级为「全局兜底 + 按对话生效（方案 A）」：新增 `setup --ask-policy confirm|remember`（默认 `confirm`），新对话首图 1 句轻确认，换模型仅作用于当前对话且不污染 `config.json`（同一对话后续不再复问）
- `relay_errors.py` 人话化内容安全报错保留 `· 网页管线先降级：...` 线索并修正分号类别边界
- 自检 65 项 → **66 项**

**v1.5.1（2026-11-23）**

- 对照上游真实 `/backend-api/models` 校准 `chat` 模型 slug：`GPT-5.6 Sol` 由 `gpt-5.6-sol-thinking` 修正为 `gpt-5-6-thinking`（即时档 `gpt-5-6-instant`，GPT-6 即时档 `gpt-6-instant`）
- 校准 `work` 模式真实 5 档：`轻度(min) / 中(standard) / 高(extended) / 极高(xhigh) / 最高(max)`，`EFFORT_ORDER` 补入 `min`，历史写法 `minimal` 自动映射为 `min` 并告警
- 错用旧 slug、`-instant` 搭思考档或跨模式混用 `-wm` 时提前告警
- `relay_errors.py` 新增 `webpipe_invalid_request`（`conduit_token` / `422`）与 `webpipe_blocked`（403 / 风控），均标 `RETRYABLE=no`；降级告警附带诊断建议，`--report-json` 补独立 `degraded_reason` 字段；`slow_after` 随短 `--timeout` 缩放
- 自检 58 项 → **65 项**

**v1.5.0（2026-11-23）**

- 修复 11 个 `@4k` 预设全部报错（像素上限对齐服务端）
- 修复 `generate-batch` 读 JSONL 未去 BOM
- 错误分类收敛为单一真相源；修 503 被当可重试、修 `no_image_data` 匹配不到、补
  `timeout` / `deadline_exceeded` / `network_error` / `rate_limited` / `no_channel`
- `RAW=` 与 `--report-json` 自动脱敏 `sk-*` / `Bearer` / `api_key`
- 去掉全局可变状态（batch 并发不再串扰）；`--dry-run` 严格零磁盘副作用
- 新增 `setup --show [--json]`（安全查看配置，不回显 key）、`--strict-n`、`--skip-existing`、
  付费调用前 Fail-Fast 预检、原子写盘
- 修复 `n>1` 多余产物命名；`--report-json` 记录真实落盘路径
- 分层超时：常规单图 150s/240s；重任务（`n>1` / `work` / `extended+` / `reference`）300s/480s
- `SKILL.md` 877 行 → ~270 行（与官方骨架对齐），实测数据下沉 `references/safety-refusals.md`
- 自检 31 项 → **58 项**

**v1.4.0（2026-11-19）**

- 网页管线三件套：`--web-mode` / `--effort` / `--agent-model`（走 `style.mssk_*` 透传）
- 失败可编程：`CODE=` / `RETRYABLE=` / `HINT=` + `--report-json`
- 会话钥匙按项目隔离（`<cwd>/.imagegen/conv-<hash>`）

**v1.3.0（2026-11-06）**

- 动图 GIF 原字节保存 + sprite sheet + `--gif-speed` 8 档
- 一次多图全部落盘

**v1.2.0（2026-10-16）**

- 图像请求直连订阅端点（快 2～3 倍），失败自动回退
- 多张参考图、透明背景、精确交付尺寸

**v1.1.x 及更早**：见 git 历史
