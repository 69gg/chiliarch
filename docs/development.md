# 开发参考

日常安装和使用从[用户手册](index.md)开始。本文汇集源码开发、插件实现和发布所需的技术资料。

| 你要开发什么 | 参考 |
| --- | --- |
| 了解平台模块和执行边界 | [架构](architecture.md) |
| 用其他语言实现连接客户端 | [协议与 JSON Schema](protocol.md) |
| 添加 agent 适配、操作或执行器 | [扩展接口](extensions.md) |
| 构建源码、运行测试、发布包 | [开发与发版](releasing.md) |
| 遵循仓库开发约定 | [AGENTS.md](../AGENTS.md) |

所有 agent 连接必须由 agent 主动出站建立，不能新增平台主动拨号、反向回调或机器接管。协议 ACK 表示持久接收，任务终态与验收分别确认；外部副作用结果未知时，不自动重做。

扩展应使用统一操作服务完成参数校验、审批、身份验证和幂等处理。协议模型保持独立，变更时同步 JSON Schema、文档与行为测试。更多规范见 AGENTS.md。


## GitHub 与原生交互测试

仓库执行器接受可注入的 argv runner。测试使用受控 GitHub 响应和临时本地 Git 仓库验证实际 worktree、提交/推送、幂等、未知副作用和签名/版本门禁；不能使用开发者真实 GitHub 仓库测试变更。原生协议用假 CLI 进程验证并发请求、stdin 序列化、取消/清理和 turn/steer，不启动真实 Codex、Claude 或 OpenCode。

仓库消息和原生请求共用 WS、SSE＋POST、长轮询与持久身份。改协议后重新导出 `src/chiliarch/protocol/schema.json`。新业务工具必须包含完整字段说明，并通过实际模型请求 schema 测试；阶段恢复、项目资源范围和审批不能只测 handler 的局部返回值。
