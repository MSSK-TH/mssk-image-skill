# MSSK Image（`imagegen-relay`）

给 Codex 用的生图技能，专配 **MSSK 中转站**（`relay.msskwithth.com:9443`）。
装完直接说话就能出图——**不用装 Node.js / Python / Go，不用配环境变量，不用重启客户端**。

> **v1.2.0（2026-10-16）起，本技能替代旧的 `mssk-image2`。**
> 最大变化：图像请求直连上游的**原生图像端点**（和官方 Codex 内置生图走同一条路），
> 速度快 2～3 倍、失败信息带原因；并新增多图、透明背景、精确尺寸。
> 行为规格见 [`SPEC.md`](SPEC.md)，历史决策与实测数据见仓库外 `交接-生图链路与技能-v2.md`。

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

> ⚠️ 这个仓库是 **private**，克隆需要你自己有权限（SSH key 或 token）。

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

打开 `%USERPROFILE%\.codex\skills\imagegen-relay\config.json`：

```json
{
  "api_url": "https://relay.msskwithth.com:9443/v1",
  "api_key": "sk-你的令牌"
}
```

令牌去中转站后台「令牌」页新建：**额度不限、分组留空、永不过期、不要开模型限制**。
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
imagegen-relay/
├── SKILL.md                 技能说明（Codex 读这个决定怎么做事）
├── VERSION.txt              版本戳 + 出厂默认值
├── config.json              你的令牌（安装后自己填）
├── bin/                     run.cmd / run.ps1 / run.sh（把 config.json 注入环境后调 CLI）
├── scripts/image_gen.py     CLI 本体（generate / edit / reference / generate-batch / models）
├── references/              提示词规范、示例、CLI 参考、本站 API 备忘
└── assets/                  图标
```

配套组件：

```
SPEC.md          行为规格：路由矩阵、参数语义、错误语义、不变量、验收测试
```

> 服务端实现（`imagegen-shim`、生图站、网页管线）**不在本仓** —— 在私有仓
> `MSSK-TH/mssk-image-relay`。本仓只放团队要安装的技能本体。

---

## 排障

| 现象 | 怎么办 |
|---|---|
| 出图很慢（1 分钟以上） | 可能走了回退路径。看 shim 日志有没有 `↩️ 回退 /responses` |
| 报 `[moderation_blocked]` | 内容安全判定。**重试**或换参考图；别再找"审核开关"了，没有 |
| 提示 `Output already exists` | 加 `--force`，或换个输出路径 |
| 完全不出图 | 先确认 `config.json` 里的令牌没过期；再看 shim 容器是否在跑 |

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

**v1.2.0（2026-10-16）**

- 图像请求直连上游**原生图像端点**（`/backend-api/codex/images/*`），与官方 Codex 内置生图同路；失败自动回退旧逻辑
- **多张参考图**支持（原来只有最后一张生效）
- **透明背景**可用（原来上游直接 400）
- 新增 `--exact-size WxH` / `--fit cover|contain`（精确交付尺寸）
- 路由建议改回 `edit`（更快、提示词逐字不被改写）
- launcher 检测到 `uv` 时自动带 Pillow（修复 `--max-dim` 原本跑不起来的问题）

**v1.1.x 及更早**：见 git 历史（旧技能名为 `mssk-image2`）
