# Claude Code

```text
请把这台机器的 Claude Code 接入 Chiliarch，先读
https://github.com/69gg/chiliarch/blob/main/docs/agents/claude-code.md。
平台 URL：<完整 HTTP 或 HTTPS 地址>
一次性注册码：<WebUI 生成的注册码>
Agent 名称：<机器名 / 项目用途>
工作目录：<已有目录的绝对路径>
状态路径：<当前用户私有目录中的绝对路径>/claude-agent.db
复用已有 Claude Code 和认证方式，不重新安装或登录 Claude、不读取认证文件或历史会话。
仅安装 Chiliarch 工具，使用 uv 管理 Python。官方仓库 69gg/chiliarch 与 PyPI 当前
可能尚未发布：优先使用我提供的可信本地 checkout，无可用来源时报告阻塞，不换源。
执行 chiliarch agent pair --server <URL> --name <NAME> --adapter claude-code
--state <绝对路径>，保密输入一次性注册码。
用 chiliarch agent config --host claude-code --state <绝对路径> 输出 stdio MCP JSON，
保留已有服务器，只合并 chiliarch；不要覆盖 ~/.claude.json 或项目配置。
运行 chiliarch agent doctor 并说明配置位置、重新加载 MCP 的方法。
我重新打开会话后，要能通过 MCP 主动领取任务、报告进度/结果、联系总管、查询其他
agent、申请协调并读取上级回复。不要用真实推理调用做安装后的自动验收。
只有我明确选择无人值守时，才写 kind="claude-code" 的本地 TOML 并启动 agent run。
先说明 plan 默认权限、需要变更时的工具允许规则、固定目录及可能的模型费用。
不使用 --dangerously-skip-permissions，不默认 --bare，不让远端任务更改本地权限。
```

## 安装与身份

按 [通用安装说明](index.md#安装接入工具) 安装 Chiliarch。官方仓库为 [69gg/chiliarch](https://github.com/69gg/chiliarch)，当前尚未推送、尚未发布 PyPI；现阶段使用可信 checkout，远程安装示例等正式发布后再运行。

Chiliarch 复用本机 `claude` 与其原生认证配置，不读取或上传认证文件，不负责订阅、模型服务或原生安装。交互式 Claude Code 使用当前会话 MCP；无人值守使用独立状态库与托管 runner。两者不要同时占用一个客户端状态库。

```bash
chiliarch agent pair \
  --server "http://平台主机:8000" \
  --name "机器名 / Claude Code" \
  --adapter claude-code \
  --state "/绝对路径/私有目录/claude-agent.db"
```

交互输入一次性注册码；长期凭证存入状态库。保密、备份、吊销和重新配对约定见 [通用说明](index.md#配对与本地身份)。

## 当前会话 MCP

生成与本地安装环境匹配的配置，不会改写 Claude Code 设置：

```bash
chiliarch agent config --host claude-code --state "/绝对路径/私有目录/claude-agent.db"
```

标准 MCP JSON 结构如下。使用生成器给出的真实 `command` 和 `args`，尤其源码环境下的解释器路径：

```json
{
  "mcpServers": {
    "chiliarch": {
      "type": "stdio",
      "command": "chiliarch",
      "args": [
        "agent",
        "mcp",
        "--state",
        "/绝对路径/私有目录/claude-agent.db",
        "--mode",
        "standalone"
      ]
    }
  }
}
```

项目作用域使用项目根目录 `.mcp.json`，只合并 `mcpServers.chiliarch`，不覆盖其他服务；不要把凭证内嵌到 JSON。用户作用域可使用原生注册命令；确认命令在 Claude 的 PATH 中可用时：

```bash
claude mcp add --transport stdio --scope user chiliarch -- \
  chiliarch agent mcp --state "/绝对路径/私有目录/claude-agent.db" --mode standalone
```

这条命令会修改 Claude Code 的 MCP 配置，安装提示词授权接入后再执行。`--` 后是 MCP 服务程序和参数；没有它时原生 CLI 可能把 Chiliarch 参数误当自己的选项。上述两种注册方式选一种。项目 MCP 的信任确认、组织策略和允许/拒绝规则仍由 Claude Code 管理。[官方 MCP 配置](https://code.claude.com/docs/en/mcp)

重新打开 Claude Code 会话或按原生提示重连，在 `/mcp` 中检查工具。之后可以直接告诉它：

```text
用 chiliarch MCP 领取当前机器的待处理任务，汇报进度与完成结果。
不清楚依赖时联系 Chiliarch AI；需要其他 agent 时提交平台查询或协作申请，
保留请求 ID 并检查回复。批准前不要把申请当成已经执行。
没有待处理工作时告诉我状态，不伪造完成记录。
```

MCP 连接本身不会保证空闲原生会话持续自动领取；长期无人值守用下面的 runner。原生 Claude Code 保留自己的工具审批边界。

工具包括 `status`、`claim_task`、`report_progress`、`complete_task`、`fail_task`、`send_message`、`list_agents`、`request_operation`、`get_request_result`、`read_messages` 和 `list_tasks`。按 [完整工作流](index.md#mcp-工具与完整工作流) 先领取、再执行、报告与查回信。保存 `read_messages` 返回的 cursor；申请未批准时不要把 `pending` 当作允许执行。

## 托管 runner 配置

本版 runner 支持 Linux/macOS，需要已安装的 `claude`。创建本地 TOML，目录必须已经存在：

```toml
kind = "claude-code"
workdir = "/绝对路径/已有项目目录"
claude_permission_mode = "plan"
claude_allowed_tools = []
timeout_seconds = 1800
terminate_grace_seconds = 2
max_input_bytes = 1048576
max_output_bytes = 524288
max_line_bytes = 262144

# 可选：复用本机程序和模型；省略时用原生默认值。
# executable = "/绝对路径/claude"
# model = "你的模型名称"
```

先为该 runner 配对一个独立状态库，再执行：

```bash
chiliarch agent doctor \
  --state "/绝对路径/私有目录/claude-runner.db" \
  --settings "/绝对路径/local-agent.toml"

chiliarch agent run \
  --settings "/绝对路径/local-agent.toml" \
  --state "/绝对路径/私有目录/claude-runner.db" \
  --max-concurrent-tasks 1
```

`plan` 默认用于分析和规划，并不适合需要修改文件的调度。机器所有者可以选择 `dontAsk`、`default` 或 `acceptEdits`，并设置 `claude_allowed_tools`。例如只自动允许某些明确工具，按 Claude 官方规则填写数组；不要添加泛化的 shell 通配规则作为默认值。

`dontAsk` 对需要交互但未允许的工具会拒绝；`default` 可能因无人值守时不能回答权限请求而阻塞/失败；`acceptEdits` 对文件编辑的授权更宽，应结合工作目录、项目可信度和原生策略选择。这些本地选项不由远端 task.parameters 控制。[Claude CLI 参数](https://code.claude.com/docs/en/cli-reference)

运行器自动允许 Chiliarch MCP 工具进行平台协作；它们的操作仍经总管/用户和统一操作服务，不会直接越权修改平台。额外原生允许规则与该前缀合并，而不是把总管 yolo 模式映射为 Claude 原生 bypassPermissions。

## 执行与结果

每个调度创建全新的原生会话，使用普通 stdin 输入任务，选择 `--print`、`--output-format stream-json`、`--verbose`、`--include-partial-messages`。临时 `--mcp-config` 配合 `--strict-mcp-config` 只注入当前 Chiliarch mailbox 服务；不改写原生全局配置。认证、原生项目指令和其他本地设置仍可继承，不启用会改变认证来源的 `--bare`。[官方程序化运行](https://code.claude.com/docs/en/headless)

runner 按结构化原生结果汇报：要求最终 `result` 为成功 subtype、`is_error=false` 且进程退出成功。限制超时、输出总量和单行大小；无终态、无效 JSONL、非零退出或原生失败事件均不会假报完成。结果包含答案、原生会话 ID、可解析的用量与费用/权限拒绝信息。[原生终态说明](https://code.claude.com/docs/en/agent-sdk/agent-loop)

任务内 MCP 可联系总管、申请 `list_agents` 查询或 `dispatch_task` 协作；读取上级回信和操作结果时保留各请求 ID。临时 mailbox 通过 `--managed-task` 绑定当前任务，不导出 `claim_task`、`report_progress`、`complete_task`、`fail_task`，不能跨任务冒用身份。任务最终状态由父 runner 在原生过程结束后确认，不应要求任务内模型提前标记完成。输出里的文件路径只是汇报，不自动上传文件。

固定 workdir 约束启动 cwd，不能把它当作操作系统沙盒。Claude Code 能访问哪些文件或执行哪些工具，由原生权限、已有配置、当前系统用户及隔离环境决定。需要更强机器隔离时，应由机器所有者另用专用用户、容器或其他隔离方式配置。

## 取消、断线与恢复

只有 agent 所在机器主动连接平台；代理不支持 WS 时降级 SSE＋POST、长轮询，服务器不联系你的机器 IP。断网消息和结果先入本地队列，重连再上传。

取消与超时会停止当前原生进程组并清理后台进程。平台取消请求尚未被客户端确认时，不能视为已停止。Chiliarch 不保留原生任务启动的后台服务，也不自动 resume 用户现有会话。

异常退出或断电后，上次执行中的任务结果未知，客户端不自动再次执行。先核对本地改动、原生任务记录及平台状态，再人工决定恢复或新派发。凭证吊销后客户端停止通信；同一状态库的单连接规则、诊断和日志见 [接入总览](index.md)。
