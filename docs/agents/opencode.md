# OpenCode v2 / v1 legacy

将下面整段发给要接入的 OpenCode，无需预填配置。它会读取 [Agent 自助接入指南](onboarding.md)，先确认自己的版本和已有守护进程，再向你询问缺少的信息并完成接入。

```text
你负责把你自己接入 Chiliarch。
请获取并阅读 https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/onboarding.md，按文档完成接入。
缺少的配置请主动向我询问。
```

## 版本选择与安装

Chiliarch 支持官方 OpenCode 的 v2 新版及 v1 legacy。两代都提供 `opencode acp`，但原生配置结构不同；这里的 v2 指 OpenCode 运行时，不是 legacy 代码中使用的 `@opencode-ai/sdk/v2` 导出路径。

| 原生宿主 | `--adapter` / `--host` / runner `kind` | 当前会话 MCP 配置位置 | 默认执行入口 |
| --- | --- | --- | --- |
| OpenCode v2 | `opencode` | `mcp.servers.<名称>` | `opencode acp` |
| OpenCode v1 legacy | `opencode-v1` | `mcp.<名称>` | legacy `opencode acp` |

支持基于 ACP 协议版本 1、`session/new`、`session/prompt`、stdio MCP 注入，以及续接时的 `loadSession` 能力。适配依据官方 v1 1.18.34 源码及 v2 ACP 文档；不承诺未提供这些能力的早期预览版本。缺少命令或协议能力时应更新对应原生工具，不将失败的任务静默换成另一个 agent 执行。[v1 ACP](https://opencode.ai/docs/acp/) · [v2 ACP](https://opencode.ai/v2/docs/cli/acp/)

先用 `chiliarch daemon status` 检查当前用户守护进程；健康时复用，已安装但未运行时 `chiliarch daemon start`。尚未安装 Chiliarch 时：

```bash
uv tool install chiliarch
chiliarch daemon install
```

已有 Chiliarch 使用 `uv tool upgrade chiliarch` 更新，先结束正在执行的托管任务，再按 [守护进程维护](../daemon.md) 重启。安装默认登记当前用户登录后自启动，支持 Windows、macOS、Linux。原生 OpenCode 必须已经安装并按用户自己的方式登录；Chiliarch 不读取或上传其认证文件，不安装模型服务，也不转换两代 OpenCode 配置。[通用安装说明](index.md#安装接入工具)

同一机器并存两代时，先通过原生版本与 `acp --help` 确认程序路径；可在 runner TOML 中配置 `executable`。v2 官方包也提供 `opencode2` 入口，是否存在以本机安装为准。不要仅凭 PATH 上的 `opencode` 名称判断版本，或覆盖另一代的安装及配置。

## 配对与默认工作目录

在 WebUI 的“Agents 管理”创建一次性注册码，然后在 OpenCode 所在机器执行；以下为 v2 示例，替换全部占位符：

```bash
chiliarch agent pair \
  --server "http://平台主机:8000" \
  --name "机器名 / OpenCode" \
  --adapter opencode \
  --agent opencode-main \
  --workdir "/绝对路径/已有项目目录" \
  --state "/绝对路径/私有目录/opencode-agent.db"
chiliarch agent config --host opencode --agent opencode-main
chiliarch agent doctor --agent opencode-main
```

v1 legacy 把 `--adapter` 和 `--host` 改为 `opencode-v1`，并选择独立的本机标识和状态库。注册码交互输入；长期凭证保存在状态库，不进入 MCP 配置、聊天或 shell 历史。配对自动生成状态库旁的 `.runner.toml` 并登记无人值守执行；省略 `--workdir` 时交互询问默认目录，已有配置可改用 `--settings <TOML>`。仅希望在当前会话手动领取任务时显式选择 `--mode manual`，并省略目录参数。

当前会话的 MCP 配置只包含本地 daemon 标识；即使默认使用 managed 身份，也能查询工作、联系总管和协调同伴。任务的领取和最终状态由 runner 负责，不由当前会话抢先报告。为每个身份保存独立状态库；两个 OpenCode 版本不能并发复用同一状态库。

## 当前会话 MCP

使用上面的 `agent config` 生成真实路径与参数，再合并到作用域对应的 `opencode.json` / `opencode.jsonc`；命令只打印配置，不会修改原生文件。保留已有 JSONC 注释、模型、权限、插件和其他 MCP，不将整个文件替换为生成片段。

v2 结构如下；命令数组以本机生成器输出为准：

```json
{
  "mcp": {
    "servers": {
      "chiliarch": {
        "type": "local",
        "command": ["chiliarch", "agent", "mcp", "--mode", "daemon", "--agent", "opencode-main"],
        "disabled": false,
        "codemode": false
      }
    }
  }
}
```

v1 legacy 使用直接的名称映射：

```json
{
  "mcp": {
    "chiliarch": {
      "type": "local",
      "command": ["chiliarch", "agent", "mcp", "--mode", "daemon", "--agent", "opencode-v1-main"],
      "enabled": true
    }
  }
}
```

v2 默认启用服务，停用字段为 `disabled`，不能照抄 v1 的 `enabled`。同名 `chiliarch` 已存在时先与用户确认复用或改名，不覆盖别的连接。按原生要求重载或打开新会话，再检查 MCP 工具；不运行真实模型推理做安装验收。[v1 MCP 配置](https://opencode.ai/docs/mcp-servers/) · [v2 MCP 配置](https://opencode.ai/v2/docs/mcp-servers/)

在已打开的当前会话内实际调用 `read_messages`、`list_agents`，检查工具是否可用以及返回的连接／请求状态；离线排队或 `pending` 不能当作已完成查询。legacy 的 ACP 实现可能忽略 MCP 启动错误，因此 `session/new` 成功、配置生成或 `doctor` 通过都不能证明任务内通信工具已连接。执行任务前先按任务提示读取协作消息；需要的 mailbox 工具不可用时报告阻塞，联系用户核对本机 MCP 配置与原生权限，不声称已完成通信验收。

## 托管 runner 配置

默认配对已创建并登记本地 TOML。需要指定两代并存时的程序或调整默认目录，可修改生成的文件，或在配对时通过 `--settings` 使用已有文件：

```toml
kind = "opencode"
workdir = "/绝对路径/已有项目目录"
timeout_seconds = 1800
terminate_grace_seconds = 2
max_input_bytes = 1048576
max_output_bytes = 524288
max_line_bytes = 262144

# v1 legacy 改为 kind = "opencode-v1"。
# 可选：指定实际安装的原生入口；省略时查找 opencode。
# executable = "/绝对路径/opencode2"
```

Windows TOML 路径可用单引号，例如 `workdir = 'C:\Users\你的用户名\Projects\app'`。默认目录必须已存在；修改后等当前任务结束，再用实际路径更新同一身份：

```bash
chiliarch daemon register \
  --state "/绝对路径/私有目录/opencode-agent.db" \
  --agent opencode-main \
  --settings "/绝对路径/私有目录/opencode-agent.runner.toml"
chiliarch agent doctor --agent opencode-main
```

WebUI、总管及下级 `dispatch_task.workdir` 可选择本轮所在机器的其他已有绝对目录；留空使用本机默认目录，不改写 TOML。成功会话续接留空目录时保留原会话目录，不能借续接切换项目。工作区互斥、重试和交接规则见 [目录配置](index.md#默认目录与任务目录)。

## 原生设置与执行过程

收到任务后，守护进程在实际工作目录启动独立 `opencode acp` 子进程，通过 stdio JSON-RPC 完成 `initialize`、`session/new` 和 `session/prompt`。每个任务向 ACP 传入名称唯一的 stdio mailbox MCP；原生全局与项目配置继续由 OpenCode 加载，不写临时覆盖配置，不替换用户已有 MCP。v2 的 ACP 使用自己的私有服务，不连接用户的共享 OpenCode 后台服务；legacy 的内部服务限制在本机回环和临时端口。

模型、provider、认证来源、原生权限、项目指令、skills、插件及其他工具设置按本机 OpenCode 规则加载。Chiliarch 不传模型覆盖、不启用自动批准或跳过权限开关，不把平台总管的 API Key 发给 OpenCode。原生配置能否被发现取决于服务用户、实际工作目录及环境；自启动保存的配置路径见 [守护进程环境](../daemon.md#原生配置目录与环境)。API 或订阅计费由原生工具和服务商决定，usage 不是扣费凭证。

平台 ask/auto/yolo 只控制平台业务操作。原生要求 `session/request_permission` 或交互输入时，当前 runner 不替用户回答或批准，任务会明确受阻或失败。需要交互的工作可用 manual 身份在当前 OpenCode 会话内处理；用户已有的原生权限继续生效。

runner 从 ACP `session/update` 累计当前主会话正文作为最终答案，工具活动转为任务进度；不上传原生思考内容或子会话正文，也不把原生完整对话展示为总管聊天时间线。完成必须有当前 `session/prompt` 的 `stopReason="end_turn"`，并确认原生进程结束。`cancelled`、输出限制、拒绝、RPC 错误、无终态、损坏的协议输出或非零退出不会冒充成功。执行结果提供答案、原生 session ID、实际 workdir 和可用的数值用量；不会直接上传第三方原始错误或 stderr。

`dispatch_task.continue_task_id` 可指向同身份最近成功完成的受管任务。runner 从本地持久记录取原生 session ID，用 `session/load` 恢复并追加本轮 mailbox，忽略加载时回放的旧消息，再发送新一轮 prompt。平台身份、服务地址、适配器和目录必须一致；不接受远端任意 session ID，不接管用户已有的交互会话，也不跨 v1/v2 转换历史。会话被删除或加载失败时明确失败，不静默新建对话。续轮启动后只能继续最新成功任务；失败、取消或未知结果需先人工核查。

任务内 MCP 与其他适配器共用同一能力：查询 agents、联系总管、发送同伴消息、参与频道、等待协作任务和交换授权产物。读取本任务收件箱时保留请求 ID、游标和 `reply_to`，普通回复不自动唤醒；需要新一轮响应时明确 `wake=true`。mailbox 绑定平台 task ID，不提供领取或提前结束托管任务的工具。详见 [协作工作流](index.md)、[同伴通信](../collaboration.md) 和 [共享产物](../artifacts.md)。

## 取消、离线与诊断

取消时先通知 ACP `session/cancel`，随后按期限结束本轮进程树；超时、协议错误与退出清理使用相同的本机控制。平台发出取消请求不等于客户端已停止。进程清理无法确认时保留工作区隔离标记，不让下一任务在未知状态下继续；断电或异常重启后也不自动重做旧任务。

平台连接始终由 Chiliarch agent 侧发起，使用 WS → SSE＋POST → 长轮询降级，原生 ACP 和 MCP 只在本机通信。断网时本地队列持久保存进度、结果与消息，重连后补发；不需要平台能连接 OpenCode 所在机器的 IP。

`doctor` 只检查本机安装路径、状态库、配置和 daemon 登记，不调用模型，也不证明原生认证、额度、MCP 连接或未来任务必然成功。遇到协议不兼容时核对真实版本、`kind` 与 `executable`；找不到项目设置时核对默认/任务目录及服务环境；权限或输入受阻时在原生宿主处理，不通过修改平台模式绕过。
