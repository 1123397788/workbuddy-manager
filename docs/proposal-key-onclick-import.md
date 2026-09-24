# 提案：密钥创建后一键导入 cc-switch / ZCode（供上游作者评审）

日期：2026-09-24。状态：**设计提案 + 本地可行性验证结论**，尚未实现。
提出者：本地部署用户（面板 v1.0.69 + 上游 workbuddy2api 47d0c39）。

## 1. 需求

在面板「密钥」页新建密钥成功后，允许把这把密钥**一键导入**到本机的客户端工具：

- **cc-switch**（Claude Code / Codex 多供应商切换器，SQLite 存储）
- **ZCode**（`~/.zcode/v2/provider_config.json`，JSON 存储）

目标体验：**真一键**——点一下按钮，客户端里立刻多出一个可直接用的供应商配置，用户无需手动复制密钥、填写 baseUrl、挑选模型。

## 2. 决定性约束（决定了功能只能落在"创建时"）

面板密钥**只存哈希**：

- `server/keysvc.py:211` — `token = TOKEN_PREFIX + secrets.token_urlsafe(32)`
- 同文件 `:230` — `out['key'] = token  # 仅此一次返回明文`
- 数据库 `api_keys` 表只有 `key_hash` 与 `prefix`（`server/db.py:76-99`）
- 前端 `web/app/(main)/keys/page.tsx:710` —「一次性展示新密钥」弹窗，之后无法再取回

**因此：密钥列表的操作列（按已有行的 `prefix`）无法生成有效配置**——前缀拼不出完整密钥。一键导入必须发生在**创建成功、明文仍可用的那一刻**。

> 这是安全设计的正确结果，本提案不建议为了该功能改动存储方式。若维护者希望支持存量密钥导入，可另开「重置并导入」（重新签发新密钥、旧密钥立即失效）作为独立特性，但那会改变用户手中已分发的密钥，需单独讨论。

## 3. 落点（UI）

`web/app/(main)/keys/page.tsx` 的「一次性展示新密钥」弹窗（`:710` 起），在现有 `CopyButton`（`:722`）旁新增两个按钮：

```
┌─ 新密钥已创建（仅此一次展示）─────────────┐
│  wbk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx        │  ← 现有：明文 + 复制
│  [复制]  [导入 cc-switch ▾]  [导入 ZCode ▾] │  ← 新增
└──────────────────────────────────────────┘
```

下拉/二次确认里可选**导入到哪个客户端身份**（Claude / Codex；ZCode 通常只有一个），避免误写。

## 4. 两种实现路径（建议同时提供）

因为"真一键"要求面板能写用户本机文件，故必须区分部署形态：

### 路径 A：本机可写（本地面板，真一键）

面板与客户端同机时，后端直接落盘/入库。

**A-1 cc-switch**（`~/.cc-switch/cc-switch.db`，SQLite）

`providers` 表字段（已实测）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 与 `app_type` 组成复合主键（UUID 或固定名如 `claude-official`） |
| `app_type` | TEXT PK | `claude` / `codex` / `gemini` … |
| `name` | TEXT | 显示名 |
| `settings_config` | TEXT | **JSON 字符串**，格式见下 |
| `website_url` / `category` / `icon` / `icon_color` | TEXT | 显示元数据（`category` 可用 `custom`） |
| `created_at` / `sort_index` | INTEGER | 排序 |
| `is_current` | BOOLEAN | 置 1 表示立即启用（**需把同 app_type 的其它行置 0**） |
| `cost_multiplier` / `limit_daily_usd` / `limit_monthly_usd` / `provider_type` | TEXT | 可选 |

`settings_config` 实测样例：

```jsonc
// app_type = claude
{"env": {
  "ANTHROPIC_AUTH_TOKEN": "<面板密钥>",
  "ANTHROPIC_BASE_URL": "http://<面板地址>/v1",
  "ANTHROPIC_MODEL": "<默认模型>",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "<模型>",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "<模型>",
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "<模型>"
}}

// app_type = codex
{"auth": {"OPENAI_API_KEY": "<面板密钥>"},
 "config": "model_provider = \"custom\"\nmodel = \"<默认模型>\"\n[model_providers]\n[model_providers.custom]\nname = \"custom\"\nwire_api = \"responses\"\nbase_url = \"http://<面板地址>/v1\"\n"}
```

要点：
- **写入前必须关闭/退出 cc-switch**，或在其运行时通过它的接口写入——直接改 SQLite 有被应用内存态覆盖的风险（与下文 ZCode 同类问题）。
- `provider_endpoints` 表另存 URL 列表（`provider_id` + `app_type` + `url`），建议同步写入，保持 UI 中「端点」一致。
- 建议 `id` 用稳定 UUID 存到面板侧（例如 `settings` 表记 `ccswitch_provider_id`），重复导入时**更新而非追加**，避免堆重复项。

**A-2 ZCode**（`~/.zcode/v2/provider_config.json`）

结构（已实测，本机已有同类配置）：

```jsonc
{
  "schemaVersion": 1,
  "config": {
    "providerOrder": ["<providerId>", ...],
    "providerConfigRules": {"providerRules": [{
      "providerId": "<uuid 或稳定串>",
      "providerName": "<供应商名>",
      "config": {
        "group": "standard-personal",
        "access": {"type": "api-key", "apiKey": "<面板密钥>"},
        "api": {"type": "openai-responses", "baseUrl": "http://<面板地址>/v1"},
        "personalModelIds": ["<模型 id>", ...],
        "modelOrder": ["<模型 id>", ...]
      }
    }]},
    "modelConfigRules": {"providerModelRules": [
      {"modelId": "<id>", "providerId": "<同上>", "config": {"properties": {"contextWindow": 1000000}}}
    ]}
  }
}
```

要点：
- **ZCode 运行时会重写该文件**（用户改配置即落盘），因此程序化写入存在竞态；理想做法是让 ZCode 提供导入入口（见 §5）。
- `personalModelIds` 与 `modelOrder` 应一致，并按密钥的**模型白名单**裁剪（见 §6）。
- `contextWindow` 取自网关 `/v1/models` 的 `context_length`（本机实测 44 个模型全部带该字段）。

### 路径 B：远程面板（降级，非真一键）

面板部署在服务器时无法写用户本机。退化为：

- 后端返回**配置片段**（`GET /api/keys/{id}/export?client=ccswitch|zcode`），前端提供「下载 .json / 复制」；
- 或生成 **cc-switch / ZCode 的导入深链**（若客户端支持）

用户体验为"一键生成配置 + 手动导入一次"，仍比手工填表好，但不满足"真一键"。

## 5. 上游/客户端侧的建议（供作者判断贡献方向）

要让"真一键"对所有用户都成立，最干净的是**客户端提供导入能力**，面板只负责生成标准配置：

1. **ZCode**：希望提供 `zcode://import?payload=<base64(json)>` 或 CLI（`zcode provider add --file -`）——面板按钮直接触发即可，避免抢写配置文件。
2. **cc-switch**：希望提供 CLI/URL scheme（如 `ccswitch://import?...`）或「从剪贴板/文件导入」，并允许指定 `app_type` 与是否设为当前。
3. **面板侧**（本项目可自主实现，与作者无关）：新增导出端点 + 落点按钮。

> 若客户端暂无导入能力，建议**先合入路径 B（导出片段）+ 路径 A（本机写入，带"客户端未运行"检测）**，并在文档中标注适用条件。

## 6. 配置内容如何生成（两客户端通用）

| 字段 | 取值 |
|---|---|
| 密钥 | 创建时返回的明文（仅此一次） |
| baseUrl | 面板对外地址 + `/v1`；**必须来自面板自身配置**（`WB_PUBLIC_BASE_URL` 或请求 HOST），不能硬编码 `127.0.0.1` |
| 默认模型 | 用户在下拉里选（候选来自 `/v1/models`，按密钥 `models` 白名单过滤）；未选则用网关 default |
| 模型列表 | 密钥 `models` 白名单非空时取交集；为空（全部模型）时取网关全量（本机 44 个） |
| 版本（realm） | 密钥的 `realm`（`cn`/`global`）决定前缀（`cn:` / `global:`），与网关 `/v1/models` 的 id 一致 |
| 供应商名 | 默认「WorkBuddy <密钥名>」，便于多密钥区分 |

**重要**：本机实测网关 44 个模型的 id 带 `cn:` 前缀，而面板模型目录（`/api/model-catalog`）返回的是**裸名**——生成配置时必须用**网关口径的 id**（带前缀），否则客户端调用会 404。这是最容易踩的坑，建议在实现里加断言。

## 7. 风险与边界

| 风险 | 处理建议 |
|---|---|
| 客户端未运行时写文件/库 | 检测进程；未运行则提示"请先启动客户端或改用导出文件"；不要盲写 |
| 客户端运行时抢写（ZCode 明确存在） | 优先走客户端导入接口；否则提示用户先退出，并在写入后让其重启 |
| 明文密钥落盘 | 面板侧不保存明文；客户端侧本就是明文存储（cc-switch/ZCode 现状如此），属既有事实；导入动作需**记入审计日志**（`audit_logs` 表已存在） |
| 重复导入产生重复供应商 | 以「密钥 id / 前缀 + 客户端」为键做 upsert；面板 `settings` 表存映射 |
| 面板在服务器（远程部署） | 走路径 B；UI 上明确区分「导入到本机」与「导出配置」两种动作 |
| 密钥含 IP 白名单 | 导入后从本机调用会受白名单限制；若白名单不含本机 IP，应在导入前校验并警告 |

## 8. 本地验证结论（已完成，未改任何客户端数据）

- ✅ cc-switch 为 SQLite（`~/.cc-switch/cc-switch.db`，16 张表），`providers.settings_config` 结构已实测（claude 用 `env.ANTHROPIC_*`；codex 用 `auth.OPENAI_API_KEY` + `config` TOML 文本）
- ✅ ZCode 为 JSON（`~/.zcode/v2/provider_config.json`），结构与写入方式已实测（本机已成功写入 44 个模型）
- ✅ 网关 `/v1/models` 返回 44 个模型，字段含 `context_length` / `supports_reasoning` / `supports_images`，可直接用于生成配置
- ⚠️ 两客户端均为"运行时重写自身配置"型应用，程序化写入需处理竞态（§7）
- ⚠️ 本机 cc-switch 与 ZCode 均处于运行状态，本次**未执行任何写入**，仅读取结构

## 9. 建议的实现顺序（供作者取舍）

1. **后端**：`GET /api/keys/{id}/export?client=ccswitch|zcode&app_type=claude|codex` 返回配置片段（**只读、无副作用**，可先合入）
2. **前端**：一次性展示弹窗加两个按钮 → 先接导出（复制/下载）
3. **本机写入（可选特性，默认关闭）**：`POST /api/keys/{id}/import-local`，仅在检测到本机存在客户端且未运行时执行；写审计日志；失败保留回滚
4. **上游客户端配合**：向 cc-switch / ZCode 提议导入 scheme 或 CLI（§5），面板按钮随之升级为真一键
