# chiliarch

连接、管理与统一调度 agents 的平台。内置 chiliarch AI 总管，提供手动管理、持久对话、工具调用和 ask / auto / yolo 审批模式。

所有连接都由 agent 主动出站发起，服务端永远不主动连接 agent。v0.1.0 完成平台框架、独立协议和参考客户端，尚未包含 Codex、Claude Code、OpenCode 或 Undefined 的实际适配器。

## 快速开始

支持 Python 3.11、3.12、3.13；开发默认使用 3.13。Python 由 [uv](https://docs.astral.sh/uv/) 管理，前端由 pnpm 管理。

在项目目录中：

```bash
uv sync --locked --group dev
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv run chiliarch init
uv run chiliarch serve
```

`init` 交互设置单用户登录密码，创建权限为 0600 的 `chiliarch.toml`。浏览器访问 `http://127.0.0.1:8000`。需要其他机器访问时，在配置中修改监听地址和公开地址，或使用 `serve --host <监听地址>`；HTTPS 可选。

前端构建产物随 Python wheel 和 sdist 发布，安装发布包后无需 Node。正式发布 PyPI 后：

```bash
uv tool install chiliarch
chiliarch init
chiliarch serve
```

首次登录后，在“配置”页面设置模型 API、模型名称、地址和 API Key；也可通过环境变量提供密钥，例如 `CHILIARCH_MODEL__API_KEY`。未配置模型时，手动管理仍然可用，总管不会生成假回复。

## 连接 agent

在 agents 页面创建一次性注册码，然后在 agent 所在机器执行：

```bash
chiliarch pair --server <平台HTTP或HTTPS地址> --name <agent名称> --state ./agent.db
chiliarch demo-agent --state ./agent.db
```

注册码交互输入；长期凭证只保存在本地状态文件，不在终端显示。`demo-agent` 只模拟进度和结果，不运行命令、不操作真实厂商 agent。客户端优先 WS，其次 SSE＋HTTP POST，最后长轮询，支持持久去重、离线队列和重连。

## 总管模式

| 模式 | 查询 | 状态变更 |
|---|---|---|
| ask | 直接执行 | 每项请求用户批准 |
| auto | 直接执行 | 依据风险判断；高风险必须询问 |
| yolo | 直接执行 | 总管自主决定 |

下级 agent 的请求也遵循相同模式。总管不能批准自己的待审批操作；用户在管理页面直接提交的操作视为该次授权。身份验证、吊销、超时和运行上限在所有模式下生效。

## 开发与发版

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
pnpm --dir web typecheck
pnpm --dir web test
pnpm --dir web build
uv run python scripts/bump_version.py 0.2.0 --dry-run
uv run chiliarch schema --output src/chiliarch/protocol/schema.json
uv build
```

版本升级脚本同步 pyproject、uv.lock 和前端元数据；CHANGELOG 需要按最终用户行为单独撰写。`v*` tag 流水线验证、构建并通过 PyPI Trusted Publishing 发布，配置步骤见 [发版文档](docs/releasing.md)。

## 文档

- [部署、初始化与维护](docs/deployment.md)
- [配置、热重载与日志](docs/configuration.md)
- [架构与保证边界](docs/architecture.md)
- [agent 协议与 JSON Schema](docs/protocol.md)
- [扩展操作与参考客户端](docs/extensions.md)
- [模型兼容与总管行为](docs/models.md)
- [开发、测试与发版](docs/releasing.md)
- [CHANGELOG](CHANGELOG.md) · [开发规范](AGENTS.md)

作者：Null · pylindex@qq.com。MIT License。
