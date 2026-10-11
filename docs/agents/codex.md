# Codex App 与 Codex CLI 接入

把下面整段发给当前 Codex。它会读取接入指南、检查环境，并主动询问缺少的配置。

```text
你负责把你自己接入 Chiliarch。
请获取并阅读 https://raw.githubusercontent.com/69gg/chiliarch/main/docs/agents/onboarding.md，按文档完成接入。
缺少的配置请主动向我询问。
```

## 选择接入方式

| 你希望怎样工作 | 使用方式 |
| --- | --- |
| 平台派发后自动执行（默认） | Managed，启动同机独立 Codex CLI |
| 当前 App / CLI 会话主动领取 | Manual，当前会话调用 MCP |

Codex App 的托管任务也需要同机已有、已配置认证且支持 app-server 的 `codex` CLI。它不会唤醒、点击或接管 App 中的聊天。CLI 与 App 的当前会话通过 stdio MCP 联系本机守护进程。

## 安装与配对

先按[通用接入步骤](index.md#安装接入工具)复用健康守护进程，没有时用 uv 从 PyPI 安装 Chiliarch 并安装用户服务。原生 Codex 先按自己的方式安装和登录。

下面是一行配对示例，替换平台地址与两个本机路径。在 WebUI 创建注册码后，运行命令并私密输入：

```sh
chiliarch agent pair --server "http://平台主机:8000" --name "工作机 / Codex App" --adapter codex-app --agent codex-app-main --state "/你的私有目录/codex-app.db" --workdir "/已有项目目录"
```

Codex CLI 将 adapter 改为 `codex`。新增独立身份用新的本机标识和状态文件。Windows 改用实际 Windows 路径；[接入总览](index.md#配对与本地身份)给出了 PowerShell 示例。

默认生成 `codex-app.db.runner.toml` 并登记无人值守执行。仅希望当前会话手动执行时，加 `--mode manual` 并省略 workdir；已有 runner 配置可用 settings 替代 workdir。

## 当前会话 MCP

```sh
chiliarch agent config --host codex-app --agent codex-app-main
```

CLI 使用 `--host codex`。命令只打印配置，使用输出中的**实际 command 和 args**，避免 App 的 PATH 与终端不同。

合并到用户 `$CODEX_HOME/config.toml`（默认 `~/.codex/config.toml`）或可信项目的 `.codex/config.toml`。只添加 Chiliarch 一项，保留其他设置；也可在 App 的 MCP 设置中填写生成的程序和参数。结构示意：

```toml
[mcp_servers.chiliarch]
command = "/生成器给出的/chiliarch"
args = ["agent", "mcp", "--mode", "daemon", "--daemon-directory", "/实际守护目录", "--agent", "codex-app-main"]
startup_timeout_sec = 30
tool_timeout_sec = 60
```

示例路径必须换成生成器输出。若它使用 Python 的 `-m chiliarch` 方式，完整保留那组参数。同名服务已存在时先确认复用或另取名称，不覆盖其他连接。

按 App 提示重载 MCP，或重新打开会话。在 CLI 可用 `/mcp` 查看服务。让当前 agent 用 `status`、`read_messages`、`list_agents` 检查；工具返回 pending 时继续查询，不能当作已上线。

## 托管 CLI 执行

收到调度后，守护进程在任务工作目录启动独立 `codex app-server`，经本机 stdio 创建线程并开始任务。它使用原生模型、provider、认证、沙箱、审批、项目指令、skills 和已有 MCP，只额外添加当前任务专用 mailbox。

默认每项任务新建线程。要接着 Chiliarch 最近成功的工作继续，可以派发时使用 `continue_task_id`，runner 经 `thread/resume` 续接。只允许同身份、同目录、同适配器且已确认停止的最近成功会话，不接管你手动创建的聊天。续接按原生语义，可能沿用原线程保存的配置。

需要修改本机默认目录或程序时，编辑生成的 TOML：

```toml
kind = "codex"
workdir = "/已有项目目录"
# 可选，固定已经核实的程序路径：
# executable = "/实际路径/codex"
```

Windows TOML 路径可以用单引号，例如 `workdir = 'C:\Projects\app'`。其余超时和字节设置见[runner 配置](../configuration.md#本机-runner-与原生配置)。等待现有任务结束后，用同一身份重新登记：

```sh
chiliarch daemon register --state "/你的私有目录/codex-app.db" --agent codex-app-main --settings "/你的私有目录/codex-app.db.runner.toml"
chiliarch agent doctor --agent codex-app-main
```

平台的 ask / auto / yolo 不修改 Codex 原生权限。需要原生审批或交互输入且无法继续时，当前 runner 不代替你回答；可由用户在交互会话中处理。实际账户或 API 计费由原生配置决定，不能保证所有调度一定走订阅。

## 使用与排错

长任务通过 mailbox 主动读取来信、汇报与联系同伴。工具说明包含用途和下一步动作，见[通信指引](tools.md)。托管任务由父 runner 确认终态，不让模型提前标记成功。

| 情况 | 处理 |
| --- | --- |
| App 已配置 MCP，但没有自动工作 | 当前聊天不会自动被唤醒；无人值守使用独立 CLI，manual 需主动领取 |
| 找不到 codex 或 app-server 不可用 | 核对 CLI 安装和实际程序路径，不用 App 窗口代替 |
| 原生配置与平时不同 | 核对任务目录、服务用户、CODEX_HOME 与保存的 PATH |
| 原生审批导致受阻 | 在原生宿主处理，平台模式不放宽原生权限 |
| 只有文本输出，任务未完成 | 仍需原生成功终态和进程结束；文字不等于执行成功 |
| 断电或进程清理未确认 | 核对实际改动，再按[工作区恢复](../daemon.md#当前会话与托管执行)处理 |

断网消息排队，重连自动降级 / 恢复传输。取消需要客户端确认，凭证吊销后停止通信。[原生 MCP 参考](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) · [原生 App Server 参考](https://learn.chatgpt.com/docs/app-server)。
