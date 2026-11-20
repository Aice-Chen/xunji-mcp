# Xunji MCP

个人使用的训记 Open API → MCP 适配器。非训记官方项目。通过 Streamable HTTP 和 GitHub OAuth 提供训练、计划、饮食、身体数据及 Agent 个人模板工具。

## 运行方式

Python 3.14、FastMCP 4.2.3；依赖由 `uv.lock` 和带哈希的 `requirements.txt` 固定。发布标签触发 GitHub Actions 测试并构建 Linux amd64 镜像 `ghcr.io/aice-chen/xunji-mcp:v0.1.0`。

- `/mcp`：需 OAuth，数字用户 ID 和登录名必须同时匹配白名单。
- `/health`：匿名返回 `ok`，不调用训记；不能证明上游 Key 有效。
- `/auth/callback`：GitHub OAuth App 回调地址。
- `/data/oauth`：加密的文件型 OAuth 状态；固定密钥及挂载目录后支持重建恢复。单实例运行；后台每小时清理过期文件。
- 以 UID/GID `10001:10001` 运行，宿主机挂载目录须允许该用户写入。容器内部监听 8000，由 HTTPS 反向代理访问。

环境变量：`BASE_URL`、`DATA_DIR`、`ALLOWED_GITHUB_LOGIN`、`ALLOWED_GITHUB_ID`、`GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`JWT_SIGNING_KEY`、`STORAGE_ENCRYPTION_SECRET`、`XUNJI_TRAIN_KEY`、`XUNJI_FOOD_KEY`、`XUNJI_FOOD_SEARCH_KEY`、`XUNJI_BODY_KEY`、`XUNJI_TEMPLATE_KEY`。

签名与存储密钥分别使用至少 48 字符的高强度随机字符串。凭据只在运行环境提供，不能提交到 Git。`ENABLE_WRITES` 默认为 `false`；完成客户端批准行为验收后才能改为 `true`。

## 工具与写入约束

8 个读取工具：`training_get`、`plans_list`、`plan_get`、`food_records_query`、`food_search`、`body_query`、`templates_sync`、`movement_names_search`。

5 个预览工具：`training_preview`、`body_preview`、`food_preview`、`custom_food_preview`、`templates_preview`。返回具体写入内容和 15 分钟有效的一次性 `confirmation_id`。`cancel_write` 取消预览；`commit_write` 只能提交绑定内容，同一预览不能并发或重复执行。

**工具标注不是用户同意的证明。** 客户端必须把 `commit_write` 设置为每次询问，并验证批准弹窗显示待提交的实际内容。模型必须先展示完整预览，用户拒绝后调用取消工具。初次部署禁用真正写入；接入不具备可靠批准流程的客户端时保持禁用。

训练更新自动保留旧字段、组次和 note 元数据；首版禁止缩短数组、重排/替换已有动作以及修改已有训练时间。预览与提交分别读取基线；发现变化则拒绝提交。上游没有训练条件写入接口，因此最后一次检查与写入之间仍有竞争窗口，确认期间避免同时在 App 编辑同一训练。

身体数据预览调用文档明确支持的 `dry_run: true`。训练、饮食和自定义食物预览先做本地校验，未确认这些接口的 dry-run 语义前不会把预览发送给写入接口。模板使用版本号保护；仅支持公开 `movements` 结构，修改旧模板须给出完整的目标结构，用户需确认动作和递增规则变化。

饮食模板列表和套用缺少请求/响应格式，v0.1 不开放。食物营养来源、单位换算和身体数据准确性仍需用户核对。真实 API 响应结构及 Key/VIP 权限须在首次只读联调验证。

## 限频、隐私与故障处理

- 缓存只在内存中，读取结果有效 5 分钟，最多 64 条；预览最多 32 份，进程重启后失效。写入成功清空读取缓存。
- 所有 API 地址固定，使用 HTTPS，不接受调用方传入的 URL，不转发重定向。
- 请求按接口/训练日限频，完整训练读取 30 秒、普通读取 15 秒、训练写入 45 秒，其余 15 秒。返回剩余等待时间，不自动循环重试。上游 `retry_after_ms` 优先。
- 写入请求 ID 在预览时固定；不假定所有接口都保证幂等。写入超时会消耗预览，须先读取/App 核对结果，避免重新创建重复写入。
- 响应解压后限制 2 MB；只记录接口名、HTTP 状态及耗时。不记录凭据或健康数据正文，关闭 HTTP/OAuth 库的详细日志及访问日志。
- OAuth 状态备份不等于训记数据备份；回退镜像不能撤销写入训记的内容。

## 开发验证

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check server.py xunji tests
docker build -t xunji-mcp:test .
```

测试只使用虚构数据和 MockTransport，不需要真实 API Key，也不会修改训记数据。

动作名快照来自 [Foveluy/Xunji-movements](https://github.com/Foveluy/Xunji-movements)，固定提交 `61064231da4bf9fcb37d2e23b29737271c0ab921`，出处保存在 `movements.json`；只收录中文名称，不包含内部 key。
