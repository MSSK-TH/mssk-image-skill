# MSSK Image（`imagegen-relay`）

给 Codex 用的生图技能，对接内部图像生成服务（地址在你的 `config.json` 里配）。
装完直接说话就能出图——**不用装 Node.js / Go，不用配环境变量，不用重启客户端**。

> **前置条件只有一个：Python 3.9+ 在 PATH 里**（Windows 上是 `python` 或 `py`）。
> 除此之外不需要任何依赖 —— 不用 `pip install`。
> 只有用 `--max-dim` / `--exact-size` / `--output-format` 这几个高级参数时才需要 Pillow
> （装了 `uv` 的话 launcher 会自动带上 Pillow，不用手动装）。

> **v1.5.0（2026-11-23）**：架构收敛 + 现存 bug 全清 + 分层超时 + 文档瘦身。
> 错误分类收敛为单一真相源（`scripts/relay_errors.py`），`RAW=` 与 report-json 自动脱敏
> `sk-*` / `Bearer` / `api_key`；去除全局可变态（batch 并发不再串扰）；`--dry-run` 严格零磁盘副作用；
> 新增 `setup --show`（安全查看配置，绝不回显 key）、`--strict-n`、`--skip-existing`、Fail-Fast 预检；
> 分层超时（常规 150s/240s，重任务 300s/480s）；自检从 31 项扩到 **58 项**。
> `SKILL.md` 从 877 行精简到 ~270 行（与官方骨架对齐），实测数据下沉到 `references/safety-refusals.md`。
>
> **v1.4.0（2026-11-19）**：网页管线三件套（`--web-mode` / `--effort` / `--agent-model`，走 `style.mssk_*` 透传）；
> 失败可编程（`CODE=` / `RETRYABLE=` / `HINT=` + `--report-json`）；会话钥匙按项目隔离。
>
> 更早：v1.3.0 加了 GIF 动图与多图返回；v1.2.0 起图像请求直连原生端点（快 2～3 倍）。
> 行为契约看 `imagegen-relay/SKILL.md` 与 `imagegen-relay/references/relay-api.md`。

---

## 装（3 步）

### 1. 把技能装进去

**方式一：从 GitHub 安装**（推荐——以后 `git pull` 就能更新）

```text
帮我安装 imagegen-relay 这个 Codex 技能：

1. 把 https://github.com/MSSK-TH/mssk-image-skill 克隆到一个临时目录
2. 把里面的 imagegen-relay 文件夹整个拷贝到 "$env:USERPROFILE\.codex\skills\imagegen-relay"
3. 对拷过去的全部文件执行 Unblock-File
4. 克隆用的临时目录用完删掉
5. 完成后告诉我结果
```

> ✅ 这个仓库是 **public**，直接 `git clone` 就行，**不需要任何权限**（不用 SSH key、不用找管理员）。

**方式二：从本地文件夹安装**

拿到 `imagegen-relay` 文件夹后，把下面这段发给 Codex，`<路径>` 换成该文件夹的真实路径：

```text
帮我安装 imagegen-relay 这个 Codex 技能：

1. 把 "<路径>" 这个文件夹整个拷贝到 "$env:USERPROFILE\.codex\skills\imagegen-relay"
2. 对拷过去的全部文件执行 Unblock-File
3. 不要修改里面的任何文件
4. 完成后告诉我结果
```

### 2. 填你自己的令牌

仓库里**没有** `config.json`（防止有人把令牌提交上去）。先复制一份：

```bash
cp imagegen-relay/config.example.json imagegen-relay/config.json
```


打开 `%USERPROFILE%\.codex\skills\imagegen-relay\config.json`：

```json
{
  "api_url": "https://relay.example.com:9443/v1",
  "api_key": "sk-你的令牌"
}
```

令牌去中转站后台「令牌」页新建：**额度不限、分组留空、永不过期、不要开模型限制**。

> ⚠️ **安全**：`config.json` 里的令牌**只属于你**。
> 不要把它提交回仓库、不要发群里、不要把整个技能目录打包外发。
> 本仓自带一个 pre-commit 钩子会拦截"填了令牌的 config.json"：
> ```
> git config core.hooksPath .githooks   # 克隆后跑一次即可生效
> ```
**一人一把，别互相传。**

### 3. 开一个新对话，说

> 用 `$imagegen-relay` 生成一张海边日出的照片，要横版

---

## 怎么用（会说话就行）

| 你说 | 它做 | 实测耗时 |
|---|---|---|
| 「生成一张……的照片」 | 文生图 | **约 15 秒** |
| 「把这张图做成卡牌」（附参考图） | 参考图出图 | **约 25 秒** |
| 「要 1024×1536 的竖版」 | 精确尺寸（见下） | +1 秒 |
| 「要透明底」 | 透明 PNG（RGBA） | 同上 |

**网页管线三件套**（只对网页管线生效，原生端点会忽略）：

| 你想要 | 怎么说 / 怎么传 |
|---|---|
| 切工作模式（做卡片、素材、要反复改） | `--web-mode work`（默认 `chat`，生图通过率更高） |
| 要更细的推理 | `--effort extended`（档位表跑 `image_gen.py tiers`） |
| 想看有哪些档 | `image_gen.py tiers` 或 `tiers --mode work --json` |
| 在同一个对话里继续改 | 同一目录下连续调用即可；多任务并行加 `--conv-key 名字` |

**尺寸怎么控**（重要，三层别混）：

| 你想要 | 怎么说 / 怎么传 | 效果 |
|---|---|---|
| **比例** | 说「16:9」或 `--size 1672x941` | ✅ **精确**（16:9 → 1672×941） |
| **精确像素** | 说「交付要 1024×1536 的图」→ 技能加 `--exact-size 1024x1536` | ✅ 交付文件就是 1024×1536（本地重采样） |
| **`size` 参数本身** | — | ❌ 上游忽略，别指望 |

---

## 能力与限制（都是实测，别信推测）

**能用**

- 文生图 / 参考图出图（**单张或多张参考图**）
- **透明背景**（真 RGBA，不是白底）
- **精确交付尺寸**（`--exact-size`，可选 `--fit cover` 裁切 / `contain` 补边）
- 被拒时**约 27 秒**给出结论，并带**判定阶段 / 类别 / 请求 ID**

**不能指望**

- **内容安全**：上游在**生成之后**判定「性感化」等内容，客户端没有任何开关能绕过。
  `moderation: low` 实测**无效**（设置里那个 IMAGE_MODERATION 只是历史遗留）。
  被拒了怎么办：**重试**（每次都是真渲染，独立随机；单次约 20～33% 通过）、**换一张穿得更保守的参考图**，或去网页版手动出。
- `n>1` 一次多张、`quality` 档位、`mask` 局部重绘：上游不支持（技能不会假装支持）。
- 精确像素只能**本地重采样**得到（上游每个比例只有一个固定尺寸）。

---

## 目录里有什么

```
imagegen-relay/                  ← 一个标准 skill 目录，整体拷进 .codex/skills/ 即可
├── SKILL.md                    技能说明（Codex 读这个决定怎么做事）
├── VERSION.txt                 版本戳 + 出厂默认值
├── LICENSE.txt                 许可证
├── config.example.json         配置模板（复制成 config.json 后填令牌）
├── agents/openai.yaml          技能在客户端里的标题/图标/默认指令
├── bin/                        run.cmd / run.ps1 / run.sh（注入 config.json 后调 CLI）
├── scripts/
│   ├── image_gen.py            CLI 本体（generate / edit / reference / batch / models / tiers）
│   ├── relay_errors.py         失败分类（CODE=/HINT= 来源）
│   └── remove_chroma_key.py    抠图兜底（与官方 skill 逐字节一致）
├── references/                 提示词规范、示例、CLI 参考、本站 API 备忘、网络说明
└── assets/                     图标
```

仓库根只有 `README.md`（本文件）、`.gitignore`、`.gitattributes`、`.githooks/`（防令牌误提交）。

> **`config.json` 不在仓库里** —— 它装的是你的令牌，只应存在于你本机。
> 服务端实现（`imagegen-shim`、生图站、网页管线）也不在本仓，在私有仓 `MSSK-TH/mssk-image-relay`。
> 本仓只放团队要安装的技能本体。

---

## 排障

| 现象 | 怎么办 |
|---|---|
| 出图很慢（1 分钟以上） | 可能走了回退路径。看 shim 日志有没有 `↩️ 回退 /responses` |
| 报 `[moderation_blocked]` | 内容安全判定。**重试**或换参考图；别再找"审核开关"了，没有 |
| 提示 `Output already exists` | 加 `--force`，或换个输出路径 |
| 完全不出图 | 先确认 `config.json` 里的令牌没过期；再看 shim 容器是否在跑 |
| `Python 3 was not found` | 装 Python 3.9+，或确认 `python` 在 PATH 里 |
| `SSL: UNEXPECTED_EOF_WHILE_READING` / 连不上 | **开 Clash 的时候容易踩**：如果服务域名没走 DIRECT，或 DNS 用了 `1.1.1.1`（国内直连不通）→ 解析超时 5 秒 → 打不开。把域名加进 DIRECT 规则并指定国内 DNS；最省事的办法是**访问本站时不挂代理**（本来就该直连，更快） |
| 401 / `invalid token` | 令牌填错，或建令牌时**没留空分组**、开了模型限制 |

运维命令（在 VPS 上）：

```bash
# 看 shim 是否走原生端点
sudo docker logs --tail 20 imagegen-shim | grep -E '原生|回退|images/'

# 临时关掉原生路（秒级回退到老逻辑）
cd /opt/codex-relay/deploy && sudo sed -i 's/NATIVE_IMAGES=1/NATIVE_IMAGES=0/' docker-compose.yml \
  && sudo docker compose up -d imagegen-shim
```

---

## 变更记录

**v1.5.0（2026-11-23）**

- 修复 11 个 `@4k` 预设全挂（`_apply_preset` 上限对齐 8,294,400 像素）
- 修复 `generate-batch` 读 JSONL 未去 BOM（改 `utf-8-sig`）
- 错误分类收敛为单一真相源：删掉 `image_gen.py` 里的重复实现（顺带修掉 `sexual/minors` 被 `/` 误切）、
  修 503 `system cpu overloaded` 被当可重试、修 `no_image_data` 匹配不到 `returned no images`、
  补 `timeout` / `deadline_exceeded` / `network_error` / `rate_limited` / `no_channel`
- `RAW=` 与 `--report-json` 强制脱敏 `sk-*` / `Bearer` / `api_key`
- 消除结构性重复：删重复 `_cmd_setup`、统一 `WireStyle`（`style.mssk_*` → multipart `web_*` 单一映射）、
  去掉 `_LAST_PIPELINE` / `_EXACT_SIZE_REQUEST` 全局态（消除 batch 并发竞态）
- `--dry-run` 严格零磁盘副作用（不再写 `.imagegen/conv-*`）；edit/batch 预览如实含 style
- 新增 `setup --show [--json]`（安全查看配置，不回显 key）、`--strict-n`、`generate-batch --skip-existing`、
  付费调用前 Fail-Fast 预检路径可写性、原子写盘（tempfile + os.replace）
- 修复 `n>1` 多余产物命名（`foo-1..foo-4` 而非 `foo-1-1`）、`--report-json` 记录真实落盘路径
- 分层超时：常规单图 150s/240s；重任务（`n>1` / `work` / `extended+` / `reference`）300s/480s
- 文档：`SKILL.md` 877 行 → ~270 行，与官方骨架对齐；实测数据下沉 `references/safety-refusals.md`
- 自检 31 项 → **58 项**

**v1.4.0（2026-11-19）**

- 网页管线三件套：`--web-mode chat|work`、`--effort`（推理档）、`--agent-model`（上游模型 slug），
  全部走 `style.mssk_*`（顶层字段会被 new-api 丢掉）
- 新增 `tiers` 子命令：列出推理档（与 web 界面同一套中文标签）
- **失败可编程**：stderr 打印 `CODE=` / `RETRYABLE=` / `HINT=` / `RAW=`；`--report-json` 失败时
  也落一份 `{ok:false,error:{...}}`
- **会话钥匙按项目隔离**：`<当前项目>/.imagegen/conv-<hash>`（原来是一个全局文件，
  同机两人或两个项目会互相覆盖）；`--conv-key` 仍可显式指定
- 修复：`--effort none` 不再误传 `thinking_effort`；`edit` 的 multipart 契约补上
  `web_conv_key/web_mode/web_effort`（此前带参考图时这些参数**静默失效**）
- 修文档：SKILL.md 增「参数推断表」「只在三种情况才问用户」「错误码→行动」三节

**v1.3.0（2026-11-06）**

- GIF 动图：按原字节保存，自动改扩展名，写出 `-sprite.png` 并打印帧元数据
- `--gif-speed {0.25..2}` 调播放速度（只改帧时长，文件本身 1x）
- 上游一次返回多张时全部落盘（`base.png → base-1.png …`）
- 新增会话钥匙 `--conv-key` / `--new-conv`

**v1.2.0（2026-10-16）**

- 图像请求直连上游**原生图像端点**（`/backend-api/codex/images/*`），与官方 Codex 内置生图同路；失败自动回退旧逻辑
- **多张参考图**支持（原来只有最后一张生效）
- **透明背景**可用（原来上游直接 400）
- 新增 `--exact-size WxH` / `--fit cover|contain`（精确交付尺寸）
- 路由建议改回 `edit`（更快、提示词逐字不被改写）
- launcher 检测到 `uv` 时自动带 Pillow（修复 `--max-dim` 原本跑不起来的问题）

**v1.1.x 及更早**：见 git 历史（旧技能名为 `mssk-image2`）
