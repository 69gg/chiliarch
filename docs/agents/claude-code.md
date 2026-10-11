# Claude Code 接入

把下面整段发给当前 Claude Code。它会读取指南，询问缺少的信息并完成接入。

```text
你负责把你自己接入 Chiliarch。
请获取并阅读 https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/onboarding.md，按文档完成接入。
缺少的配置请主动向我询问。
```

## 安装与配对

先安装并按自己的方式登录原生 `claude`。按[通用接入步骤](index.md#安装接入工具)先检查当前用户守护进程，健康则复用；没有时用 uv 从 PyPI 安装 Chiliarch，再安装用户服务。

默认无人值守：守护进程收到调度后启动独立 Claude Code；当前会话也可通过 MCP 查询和协调。仅希望自己手动领取时使用 manual。

在 WebUI 创建一次性注册码，替换示例地址与路径后运行：

```sh
chiliarch agent pair --server "http://平台主机:8000" --name "工作机 / Claude Code" --adapter claude-code --agent claude-main --state "/你的私有目录/claude-agent.db" --workdir "/已有项目目录"
```

命令私密输入注册码，保存长期凭证，生成 `claude-agent.db.runner.toml` 并登记。仅 manual 模式改用 `--mode manual`，省略 workdir。Windows 路径示例见[接入总览](index.md#配对与本地身份)。

## 当前会话 MCP

先生成真实配置：

```sh
chiliarch agent config --host claude-code --agent claude-main
```

命令只打印，不自动修改 Claude Code。两种作用范围选一种：

- 当前项目：合并到项目根目录的 `.mcp.json`。
- 当前用户：使用原生 `claude mcp add --transport stdio --scope user` 注册。

项目结构示意如下，command 与完整 args 换成生成器输出，保留其他 servers：

```json
{
  "mcpServers": {
    "chiliarch": {
      "type": "stdio",
      "command": "/生成器给出的/chiliarch",
      "args": ["agent", "mcp", "--mode", "daemon", "--daemon-directory", "/实际守护目录", "--agent", "claude-main"]
    }
  }
}
```

用户作用域命令示意：

```sh
claude mcp add --transport stdio --scope user chiliarch -- "/生成器给出的/chiliarch" agent mcp --mode daemon --daemon-directory "/实际守护目录" --agent claude-main
```

`--` 后是 MCP 程序和参数。若生成器给出 Python `-m chiliarch`，用那组实际 command / args。不要覆盖整个用户配置或项目文件，不把平台凭证写进 MCP 配置。

按原生提示重连或打开新会话，在 `/mcp` 检查。项目信任、组织策略和工具权限仍由 Claude 管理。[官方 MCP 配置](https://code.claude.com/docs/en/mcp)

## 托管 runner 配置

默认配对已生成 TOML。需要修改默认项目或固定程序时编辑它：

```toml
kind = "claude-code"
workdir = "/已有项目目录"
# 可选：
# executable = "/实际路径/claude"
```

Windows 路径可写为 `workdir = 'C:\Projects\app'`。其他超时和字节字段见[runner 配置](../configuration.md#本机-runner-与原生配置)。先等当前任务结束，再登记同一身份：

```sh
chiliarch daemon register --state "/你的私有目录/claude-agent.db" --agent claude-main --settings "/你的私有目录/claude-agent.db.runner.toml"
chiliarch agent doctor --agent claude-main
```

## 执行时使用哪些原生设置

Runner 在任务目录运行 `claude --print --output-format stream-json`，并使用流式输出需要的 verbose / partial-message 参数。任务文本通过 stdin 输入。默认新建会话，合法成功会话的后续轮次使用 `--resume`。

原生模型、服务商、登录、权限、已有 MCP、skills、hooks 和 CLAUDE.md 继续按 Claude 的规则加载。Chiliarch 不传模型或权限覆盖，也不使用 strict-mcp-config / bare。任务只追加自己的 mailbox，不替换已有服务或改写全局配置。

是否使用订阅或 API 来源由你的原生配置和服务商决定。查看[服务环境](../daemon.md#原生配置目录与环境)可以核对服务用户、PATH 和 CLAUDE_CONFIG_DIR；平台总管模型与这里无关。

平台审批不替代 Claude 的工具允许规则。Print 模式中需要交互的工具可能被拒绝，Claude 可以在已有权限内继续；单次拒绝不必然意味着整个任务失败，runner 根据最终结构化结果判断。必须由用户回答且无法继续时，在原生宿主处理。

## 汇报、协作与恢复

任务内用专用 mailbox 读信、联系总管、查询同伴和共享产物。托管任务由父 runner 报告终态；manual 身份则先 claim_task，再按实际工作汇报。工具直接说明如何主动读信，见[工具指引](tools.md)。

成功需要原生最终结果与进程正常结束，不能仅凭一段输出判断。结果包括答案、会话 ID、实际目录以及可用用量；费用估算不是扣费凭证。

`continue_task_id` 只续接同身份、同目录、同适配器的最近成功托管任务，不接管用户的交互会话。取消与超时清理原生进程树；断电后结果未知的工作不自动重做。

| 情况 | 处理 |
| --- | --- |
| MCP 没工具 | 核对生成的程序路径、原生信任与权限，重连 |
| 配置与平时不同 | 核对任务目录、服务用户与 CLAUDE_CONFIG_DIR |
| 平台 auto / yolo，原生仍拒绝工具 | 原生权限独立生效，由用户在原生设置或宿主处理 |
| 总管 / 同伴消息未见回复 | 主动 read_messages；pending 保留请求 ID 查询 |
| 任务中断或目录被隔离 | 核对实际改动，再按[恢复流程](../daemon.md#当前会话与托管执行)处理 |

[原生 CLI 参考](https://code.claude.com/docs/en/cli-reference) · [原生程序化运行](https://code.claude.com/docs/en/headless) · [目录与任务](index.md#默认目录与任务目录)。
