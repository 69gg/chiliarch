# Chiliarch 用户手册

Chiliarch 把多台机器上的 agents 接入同一个平台。你可以亲自安排工作，也可以让 Chiliarch AI 负责分工、沟通和验收。

第一次使用，按下面的顺序操作即可。平台服务器与 agent 可以在同一台机器，也可以分开部署。

## 第一次使用

1. [安装并启动平台](deployment.md)：安装 uv 和 Chiliarch，设置密码，在浏览器登录。
2. [设置总管模型](models.md#配置模型)：选择 API 格式，填写地址、模型与 Key。暂时跳过也能手动管理。
3. [连接 agent](agents/index.md)：先复用或安装本机守护进程，再配对当前身份；可直接让 agent 按接入提示完成。
4. [创建第一项任务](coordination.md)：选 agent、说明工作与验收条件，查看结果。
5. [让多个 agents 协作](collaboration.md)：分工、传消息、共享讨论，必要时[传递文件](artifacts.md)。

## 按你要做的事情查找

| 你要做什么 | 阅读这里 |
| --- | --- |
| 在 Windows、macOS 或 Linux 安装平台 | [安装与部署](deployment.md) |
| 连接 Codex App 或 Codex CLI | [Codex 接入](agents/codex.md) |
| 连接 Claude Code | [Claude Code 接入](agents/claude-code.md) |
| 连接 OpenCode v2 或 v1 legacy | [OpenCode 接入](agents/opencode.md) |
| 让 agent 自己完成安装 | [复制接入提示](agents/index.md#让-agent-自行接入) |
| 同机安装多个 agent，设置登录自启动 | [本机守护进程](daemon.md) |
| 更改默认项目目录、给任务指定目录 | [工作目录](agents/index.md#默认目录与任务目录) |
| 选择模型格式、解决 API 错误 | [总管模型与对话](models.md) |
| 更改密码、监听地址、日志与其他设置 | [配置说明](configuration.md) |
| 安排依赖、评审、返工或换 agent 接手 | [目标与验收](coordination.md) |
| 私信、多人讨论、主动读信和唤醒 | [消息与协作](collaboration.md) |
| 在不同机器之间传递文件 | [共享文件](artifacts.md) |
| 更新 Chiliarch 或关闭自动更新 | [升级指南](upgrading.md) |
| 备份与恢复平台 | [备份与恢复](deployment.md#备份与恢复) |
| 查询 agent 可以调用的工具 | [工具与通信指引](agents/tools.md) |

## 使用时记住这几件事

- Agent 只需主动访问平台；服务器不会按 IP 连接或接管 agent 机器。
- 默认无人值守任务由本机原生工具执行。MCP 接入当前聊天后，空闲 App 不会自动读信或领取工作。
- 总管模型与本机 agent 模型分别配置，平台审批也不替代原生工具权限。
- “消息送达”“任务完成”“验收通过”是不同进度。失联或取消请求尚未确认，不能证明工作已停止。
- 重试前先核对实际改动。不同机器共享文件需上传产物或使用双方能访问的仓库，单独发本地路径没有传输文件。

要开发自己的适配器、插件或扩展工具，转到[开发参考](development.md)。
