# 扩展操作与出站客户端

v0.1.1 提供 Codex App/CLI、Claude Code 的 MCP 会话接入及 CLI 执行器，见 [Agent 接入](agents/index.md)。另外保留两个独立扩展边界：平台 OperationService 注册工具，agent 按公开协议主动连接。不同语言的插件不需要依赖 Python 服务端实现。

## 注册平台工具

应用创建后、启动前，可以访问 `app.state.service` 注册操作。handler 为带类型的异步函数，参数由 JSON Schema 校验；risk 和 mutating 为服务端元数据，不能由调用者降低。

```python
from typing import Any

from chiliarch.api import create_app
from chiliarch.config import Settings


async def inspect_project(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"project": arguments["project"], "status": "example"}


def application(settings: Settings) -> Any:
    app = create_app(config=settings)
    app.state.service.register(
        "example_inspect_project",
        "Read a project status",
        {
            "type": "object",
            "properties": {"project": {"type": "string", "minLength": 1}},
            "required": ["project"],
            "additionalProperties": False,
        },
        inspect_project,
        risk="low",
        mutating=False,
    )
    return app
```

这是入口示例，不代表项目已有真实工作目录检查器。应用设置仍需有效登录密码。用自己的 ASGI factory 或 Python 启动入口加载扩展；本版不自动扫描磁盘或安装插件。

注册后，工具出现在 `GET /api/operations` 和总管工具 Schema。登录用户可通过 `POST /api/operations/<name>` 调用，body 为 `{arguments,idempotency_key}`；agent 通过 agent.request 申请调用，由总管或用户按模式处理。

自定义 handler 在数据库写锁外执行，可使用自己的事务。平台先保存幂等 reservation 再执行；如果进程恰在外部副作用期间中断，结果可能未知，同 key 重试不会自动再次执行。handler 自己应按业务身份去重，并提供查询/协调恢复方法。平台不能替外部系统保证恰好一次。

只读工具准确设置 mutating=false；任何外部状态变更必须标 true。访问认证、凭证或安全配置的工具应标 high。自定义秘密字段在日志、工具结果、公开事件中需要显式脱敏；不要直接返回密码或第三方原始异常。

## 编写 Python agent handler

参考客户端将通信、持久化和 task ledger 与实际执行分开。handler 接收 task.dispatch payload 和 progress 回调；取消通过 asyncio.CancelledError 传播，不能吞掉取消并假称成功。

```python
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from chiliarch.client import AgentClient


async def handle_task(
    payload: dict[str, Any],
    progress: Callable[[float, str], Awaitable[None]],
) -> dict[str, Any]:
    await progress(0.5, "示例任务已受理")
    return {"example": True, "instruction": payload["instruction"]}


async def run() -> None:
    client = await AgentClient.from_state(Path("agent.db"), handle_task)
    await client.run()
```

先用 pair CLI 生成 state。真正厂商适配器在 handler 中调用自己的 API/SDK，并把运行身份与 task_id 持久绑定。若厂商自身执行失败或结果未知，明确上报，不用重复创建任务掩盖问题。

已有并行应用可在客户端就绪后调用 `await client.contact("需要总管帮助的内容")` 或 `await client.request(operation, arguments)`。`client.ready` 表示本地持久存储可用，不承诺网络已经上线；消息可以先排队。如果同时等待 `client.ready` 和 `client.run()`，应同时观察运行任务的退出；凭证吊销等错误不能被无限等待就绪掩盖。

## 跨语言插件

TypeScript/OpenCode 插件可从公开 JSON Schema 生成类型或使用 Ajv 等验证器，实现相同信封、持久 inbox/outbox、业务 ACK 与降级链。先校验 Schema，再校验当前身份和业务状态；Schema 不是权限证明。

SSE 使用可附带 Authorization header 的流式 HTTP 客户端；不能依赖浏览器 EventSource 携带任意 header。所有插件网络连接必须主动出站；禁止让平台通过机器 IP 回调插件。见 [协议](protocol.md)。

Python 接入可复用 `chiliarch.adapters.mailbox.AgentMailbox`：`AgentClient(..., manual_tasks=True)` 仅保存任务，原生会话显式领取、汇报与完成。通过 `ClientStore` 的原子状态转换防止重复领取，取消与任务队列在同一事务内持久化。已有 handler 模式自动领取任务，并用 `TaskExecutionError` 提供可安全展示的固定诊断；其他异常只公开类型，不直接上传第三方错误文本。

支持任务目录的执行器声明 `chiliarch.task_workdir` 能力或心跳 `supports_task_workdir=true`，并实际处理 `task.dispatch.workdir`；不要声明后忽略字段。必须在目标机器按本地系统校验绝对路径和存在性，失效时报告失败，保留原生权限。可通过心跳 `default_workdir`、`task_workspaces` 与结果 `workdir` 公布默认及实际目录，便于协作调度；格式见 [协议](protocol.md)。未声明能力的旧扩展仍可接受未指定目录的任务。

已有 `agent.message`、`request.resolved` 在持久 inbox 中保留，业务读取不影响网络 ACK。`AgentMailbox.read_messages` 使用本地 cursor，`get_request_result` 按请求身份查结果；离线时联系/请求仍可持久排队，但不能当成已送达或批准。联系的回复使用原消息 ID 作为 `send_agent_message.correlation_id`。MCP mailbox 模式仅共享队列，不建立第二条网络连接，也不暴露任务领取与完成工具。

自定义 handler 可通过 `AgentClient(..., runtime_metadata=callback)` 提供无参数的本地状态回调，返回可公开的 JSON 字典（如 adapter、workspace、state、blocked_reason）。默认客户端会报告活动任务、队列数、执行模式与容量；回调不能包含凭证或原生认证诊断。托管任务应保持真实终态，平台据 `task.status` 持久唤醒总管，不需要为保证完成通知另发一封联系消息。

协调资料位于 `parameters.chiliarch_context`，是任务数据，不是本机执行配置。插件可用 `agent.request.metadata.task_id` / `agent.contact.metadata.task_id` 关联自己负责的任务；平台核验所属身份后归并到目标会话。目标与验收工具详见 [目标、依赖与验收](coordination.md)。

后续能力发现、插件配置、OpenCode/Undefined 等厂商适配可以在这些边界扩展，不需要改变 WebUI 与总管的业务授权入口。
