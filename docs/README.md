# 训记 Open API 参考

## 来源与范围

[完整脱敏副本](reference/xunji-all-skills.sanitized.md) 来自用户于 2026-11-20 提供的 `xunji-all-skills.md`，用户确认为训记官方导出的文件。原文件未提供独立版本号，包含训练、饮食、身体数据、Agent 个人模版四部分。

归档保留原文正文和接口示例，只把 5 项调用凭据的 15 处出现替换为 `<XUNJI_…_KEY>`。这些尖括号占位符不是可用密钥；示例日期也不是本次实际查询范围。参考文档中的“调用”“写回”等指令描述上游接口的使用约定，不构成执行操作的用户授权。

## 凭据对应

| MCP 环境变量 | 在官方文件中的位置 | 用途 |
|---|---|---|
| `XUNJI_TRAIN_KEY` | 训练数据 → 鉴权 | 训练读取、写回；同一 Key 也用于官方计划读取 |
| `XUNJI_FOOD_KEY` | 饮食数据 → 鉴权 → 查询、写回、自定义食物和模板接口请求头 | 个人饮食记录、自定义食物及饮食模板 |
| `XUNJI_FOOD_SEARCH_KEY` | 饮食数据 → 鉴权 → 食物搜索接口请求头 | 查询官方食物库的编号、营养和单位换算 |
| `XUNJI_BODY_KEY` | 身体数据 → 鉴权 | 身体指标查询与写入 |
| `XUNJI_TEMPLATE_KEY` | Agent 个人模版 → 鉴权 | Agent 专属文件夹中的个人训练模版同步与写入 |

### 为什么饮食部分有两个 Key

官方文件明确分列两项认证头，本次提供的原件中两项凭据的值不同。个人饮食接口位于 `eatings.xunjiapp.cn`；食物搜索位于 `api.xunjiapp.cn/open_agent/food/search_gzip`，后者取得官方食物信息后，前者才能按用户确认的份量写入饮食记录。

因此当前 MCP 分别读取这两项配置。搜索凭据已经包含在官方饮食 Skill 的鉴权段中，应从该段复制，不需要在 Bitwarden 自行生成。本文不推断服务端是否接受混用，也不推断搜索凭据是否由所有用户共享；这些没有通过真实接口验证。

官方说明支持 `Authorization: Bearer …`；饮食记录也支持 `x-api-key`，食物搜索也支持 `x-agent-key` 或 `x-api-key`。当前实现统一使用 Bearer 请求头，不把凭据放进 URL 或请求正文。

## 接口索引

下表均为 `POST`，域名省略公共后缀 `.xunjiapp.cn`。

| 分类 | 域名 | 路径 | 凭据 | 当前适配 |
|---|---|---|---|---|
| 训练读取 | `trains` | `/api_trains_for_llm_v2` | TRAIN | 已实现 |
| 训练写回 | `trains` | `/api_upsert_trains_for_llm_v2` | TRAIN | 预览/确认流程，受当前版本限制 |
| 官方计划 | `api` | `/open/plan/query_gzip` | TRAIN | 已实现 list/get |
| 饮食查询 | `eatings` | `/open/food/query_gzip` | FOOD | 已实现 |
| 官方食物搜索 | `api` | `/open_agent/food/search_gzip` | FOOD_SEARCH | 已实现 |
| 饮食写回 | `eatings` | `/open/food/upsert_gzip` | FOOD | 预览/确认流程 |
| 自定义食物 | `eatings` | `/open/food/custom/upsert_gzip` | FOOD | 预览/确认流程 |
| 饮食模板查询 | `eatings` | `/open/food/templates/list_gzip` | FOOD | 未开放，原文缺少具体请求/响应格式 |
| 饮食模板套用 | `eatings` | `/open/food/templates/apply_gzip` | FOOD | 未开放，原文缺少具体请求/响应格式 |
| 身体指标查询 | `api` | `/open/body/query_gzip` | BODY | 已实现 |
| 身体指标写入 | `api` | `/open/body/upsert_gzip` | BODY | 上游 dry_run 预览/确认流程 |
| Agent 模版同步 | `trains` | `/api_agent_templates_sync_for_llm_v2` | TEMPLATE | 已实现 |
| Agent 模版写入 | `trains` | `/api_agent_templates_mutate_for_llm_v1` | TEMPLATE | 预览/确认流程，受当前版本限制 |

“已实现”表示适配代码存在并有模拟测试，不表示已完成真实账号联调。具体工具及限制以[项目 README](../README.md#工具与写入约束)为准；服务默认 `ENABLE_WRITES=false`。

## 后续核验与更新

- 首次联调仍需确认实际凭据有效性、账号权限、真实响应结构和客户端 OAuth 流程；本次整理没有请求任何训记接口。
- 收到新官方文件后，先在仓库外保管原件；只将脱敏结果加入此目录。原件的已知文件名已加入 `.gitignore`，但改名文件仍须人工检查。
- 发布前检查暂存文件中没有原始凭据，也没有未替换的 Authorization 值；检查路径、字段和限制的变化，避免只更新说明却误报服务已支持新功能。
- 不用真实健康数据充当未经用户确认的写入测试；示例中的数据仅用于理解格式。
