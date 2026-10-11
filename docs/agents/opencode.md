# OpenCode v2 与 v1 legacy 接入

把下面整段发给要接入的 OpenCode。它会确认自身版本，再询问缺少的信息。

```text
你负责把你自己接入 Chiliarch。
请获取并阅读 https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/onboarding.md，按文档完成接入。
先按 OpenCode 接入文档核对你当前实际使用的可执行文件及其 --version 输出，判断是 v1 legacy 还是 v2，再选择对应适配器。
缺少的配置请主动向我询问。
```

## 选择对应版本

这里的 v1 / v2 指**正在运行的 OpenCode 主版本**，不是 SDK 导出路径中的 `/v2`。两代都使用本机 ACP 执行，但 MCP 配置结构不同。

| 当前主版本 | adapter / host / runner kind | MCP 配置 |
| --- | --- | --- |
| `1`（legacy） | `opencode-v1` | `mcp.<名称>` |
| `2` | `opencode` | `mcp.servers.<名称>` |

原生安装应提供 `acp` 命令与对应 ACP 能力。Chiliarch 根据显式选择的适配器运行，不会在配对时猜测版本或替你换另一代程序。

### 给 agent 的版本判断步骤

1. 找出当前会话实际使用的程序路径。Linux / macOS 可用 `command -v opencode`、`command -v opencode2` 找候选；Windows 用 `Get-Command opencode, opencode2 -CommandType Application -All -ErrorAction SilentlyContinue | Select-Object Name, Source`。候选列表不一定能证明当前会话用哪一个，结合宿主显示和启动路径确认。
2. 对**同一个已确认的可执行文件**运行 `--version` 和 `acp --help`。它们只查询版本 / 帮助，不发模型请求。Unix 可用 `"/实际路径/opencode" --version`，PowerShell 可用 `& 'C:\实际路径\opencode.exe' --version`，都需换成实际路径。
3. 主版本 `1` 选 opencode-v1，主版本 `2` 选 opencode。名称、PATH 排序、SDK 路径与配置字段不能代替版本输出；无法明确解析、开发构建或其他主版本时询问用户。
4. 多份安装并存时，在 runner TOML 的 `executable` 固定核实过的程序，避免守护进程后来从 PATH 找到另一代。

不要为接入升级、转换或覆盖另一代 OpenCode 的安装与配置。

## 安装、配对与目录

先按[通用安装步骤](index.md#安装接入工具)复用 / 准备本机守护进程。Chiliarch 默认从 PyPI 用 uv 安装；原生 OpenCode 应已安装并按用户自己的方式配置认证。

以下是一行 v2 配对示例，替换地址与本机路径，在 WebUI 创建注册码后私密输入：

```sh
chiliarch agent pair --server "http://平台主机:8000" --name "工作机 / OpenCode" --adapter opencode --agent opencode-main --state "/你的私有目录/opencode-agent.db" --workdir "/已有项目目录"
```

v1 改用 adapter opencode-v1，并选择独立标识和状态库。默认生成 `opencode-agent.db.runner.toml`、登记无人值守。已有 TOML 用 settings 替代 workdir；仅当前会话手动领取时用 `--mode manual`，省略目录参数。

Windows 一行命令使用实际 Windows 路径，通用 PowerShell 示例见[接入总览](index.md#配对与本地身份)。同机两代不能共用同一状态库。

## 当前会话 MCP

v2：

```sh
chiliarch agent config --host opencode --agent opencode-main
```

v1：

```sh
chiliarch agent config --host opencode-v1 --agent opencode-v1-main
```

命令只打印配置。使用输出的完整 command 数组与绝对路径，只合并 Chiliarch 这一项到当前项目或用户的 opencode.json / opencode.jsonc，保留已有注释、模型、权限、插件与其他服务。

v2 结构示意：

```json
{
  "mcp": {
    "servers": {
      "chiliarch": {
        "type": "local",
        "command": ["/实际路径/chiliarch", "agent", "mcp", "--mode", "daemon", "--daemon-directory", "/实际守护目录", "--agent", "opencode-main"],
        "disabled": false,
        "codemode": false
      }
    }
  }
}
```

v1 legacy：

```json
{
  "mcp": {
    "chiliarch": {
      "type": "local",
      "command": ["/实际路径/chiliarch", "agent", "mcp", "--mode", "daemon", "--daemon-directory", "/实际守护目录", "--agent", "opencode-v1-main"],
      "enabled": true
    }
  }
}
```

v2 的 disabled=false 表示连接，codemode=false 将工具直接提供给模型；不要照抄 v1 的 enabled。同名服务冲突时先确认复用或改名。

重载或重新打开原生会话，实际调用 `status`、`read_messages`、`list_agents` 检查返回状态。Legacy ACP 可能忽略 MCP 启动失败，不能把会话创建成功或 doctor 通过当作通信成功。

[官方 v2 MCP 配置](https://opencode.ai/v2/docs/mcp-servers) · [官方 v1 MCP 配置](https://opencode.ai/docs/mcp-servers/)

## 托管 runner 配置

需要固定两代并存时的程序，可编辑生成的文件，或配对时通过 settings 使用已准备好的 TOML：

```toml
kind = "opencode"
workdir = "/已有项目目录"
executable = "/已核实的/opencode"
```

v1 改为 `kind = "opencode-v1"`。Windows 路径可用单引号，例如 `workdir = 'C:\Projects\app'`。超时与字节设置见[runner 配置](../configuration.md#本机-runner-与原生配置)。任务结束后重新登记同一身份：

```sh
chiliarch daemon register --state "/你的私有目录/opencode-agent.db" --agent opencode-main --settings "/你的私有目录/opencode-agent.db.runner.toml"
chiliarch agent doctor --agent opencode-main
```

## 怎样执行与协作

收到调度后，在实际工作目录启动本机 `opencode acp`，新建原生会话并发送任务。当前任务得到自己的 stdio mailbox；原生模型、provider、认证、项目指令、skills、插件、权限和原有 MCP 保持原生规则。

平台不会传总管 Key、覆盖模型或开启自动批准。服务用户、目录和配置环境决定原生设置能否被发现；见[守护进程环境](../daemon.md#原生配置目录与环境)。订阅与 API 计费由原生工具和服务商决定。

任务内 agent 主动读信、联系总管、找同伴和共享产物。Mailbox 不提供提前结束托管任务的工具，父 runner 根据 ACP 成功终态与进程结束报告结果。公开思考与完整原生对话不会直接搬到总管时间线。

最近成功的托管任务可通过 continue_task_id 用 `session/load` 续接，需要原生支持加载会话。只允许同身份、同适配器、同目录的会话，不跨 v1 / v2 转换历史，也不接管用户已有聊天。加载失败会明确报告，不假装新建就是恢复。

## 常见问题

| 情况 | 处理 |
| --- | --- |
| 找不到 ACP 或协议不兼容 | 核对实际主版本、adapter / kind 与 executable |
| MCP 没有工具 | 核对两代配置格式、程序路径与原生权限，实际调用读信工具验证 |
| 平台 yolo，原生仍问权限 | 原生权限独立；当前 runner 不替用户回答原生交互审批 |
| 配置与平时不同 | 核对任务目录、服务用户和 OPENCODE_CONFIG 等环境 |
| 取消仍未确认 | 等待客户端结束进程；离线会排队，不证明已停止 |
| 重启后工作区隔离 | 检查旧进程及副作用，再按[恢复步骤](../daemon.md#当前会话与托管执行)处理 |

[工具与通信指引](tools.md) · [消息与协作](../collaboration.md) · [原生 v2 ACP](https://opencode.ai/v2/docs/cli/acp/) · [原生 v1 ACP](https://opencode.ai/docs/acp/)。
