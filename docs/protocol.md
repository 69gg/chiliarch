# agent 协议 1.0

协议版本 `1.0` 与软件版本 `0.4.0` 独立。权威模型在 `chiliarch.protocol`，只依赖 Pydantic，不导入服务端、数据库或模型 SDK。

```bash
chiliarch schema --output protocol.schema.json
```

同一 Schema 随包提供为 `chiliarch/protocol/schema.json`，也可从 `GET /api/protocol/schema` 获取。非 Python 客户端应以 Draft 2020-12 Schema 校验信封、类型与 payload，额外使用 format checker 校验时间格式。

## 配对与身份

登录用户或获许可的总管创建注册码。`POST /api/agent/pair` 提交：

```json
{"code":"一次性注册码","name":"agent名称","machine":"机器标识","capabilities":["命名空间.能力"]}
```

响应包含 `agent_id`、`credential`、`protocol_version`。注册码到期或成功兑换后失效；并发兑换只能一个成功。长期凭证只在兑换时返回，应保存到访问受限的本地文件。服务端保存长期凭证摘要，可随时吊销。

后续 HTTP/WS 使用 `Authorization: Bearer <credential>`。禁止在 URL 查询参数携带凭证。身份由服务端认证上下文确定，不能靠 payload 自称其他 agent。

## 信封

```json
{
  "id":"稳定消息ID",
  "type":"task.status",
  "correlation_id":"关联任务或请求ID",
  "protocol_version":"1.0",
  "timestamp":"2026-10-07T00:00:00Z",
  "expires_at":null,
  "payload":{"task_id":"任务ID","status":"running","progress":0.5}
}
```

`id` 和关联 ID 为 1–128 字符的非空字符串，推荐 UUID。时间必须带时区。重试保留完整信封，包含时间和 ID；不能每次重新生成默认值。未知协议版本、额外信封字段或不符合类型的 payload 会拒绝。

| 类型 | 方向 | payload |
|---|---|---|
| heartbeat | agent → 平台 | `status`、`metadata` |
| ack | 双向 | `message_id`，指向已经持久接收的消息 |
| task.dispatch | 平台 → agent | `task_id`、`instruction`、`parameters`，可选 `workdir` |
| task.cancel | 平台 → agent | `task_id`、`reason` |
| task.status | agent → 平台 | `task_id`、`status`，可选 progress/result/error/message |
| agent.request | agent → 平台 | `request_id`、`operation`、`arguments`，可选 message、metadata |
| agent.contact | agent → 平台 | `message`、`metadata` |
| agent.message | 平台 → agent | `message`、`metadata` |
| collaboration.message | 平台 → agent | `message_id`、可信发送身份、收件身份、channel/task/source_task、正文、reply_to、sequence、wake |
| request.resolved | 平台 → agent | `request_id`、`status`，可选 result/error |

任务上报状态为 `accepted/running/completed/failed/cancelled/interrupted`；progress 在 0–1。`interrupted` 表示执行停止而外部结果可能未知。agent 只能上报分配给自己的任务；终态不能改成另一终态。服务端专用类型不能由 agent 上传。

`task.dispatch.workdir` 是 agent 机器上的已有绝对目录（POSIX、Windows 盘符或 UNC 路径），最多 4096 字符，不含控制字符；Schema 单独禁止控制字符，避免不同正则引擎对末尾换行的处理差异。客户端应先按自己的操作系统解析和校验存在性，再在实际路径加锁、启动工具；无效目录报告任务失败，不自动创建或忽略。该字段在协议 1.0 中可选，省略/null 不进入标准化旧消息内容，保留旧队列重投指纹。新内容必须使用新消息 ID。

仅向配对能力含 `chiliarch.task_workdir` 或心跳 `supports_task_workdir=true` 的 agent 下发显式目录；旧客户端不支持时操作返回 409。参考 runner 新任务未指定目录时使用本地默认目录，续接时使用原会话目录。`task.status.result.workdir` 记录实际执行目录；普通 `parameters.cwd` 不是启动参数。心跳增加 `default_workdir` 与 `task_workspaces`（任务 ID → `{workspace, workspace_id}`），协作等待优先按每个任务的实际目录检测冲突，旧 `workspace`/`workspace_id` 继续描述默认目录。

向总管联系：agent 用 `agent.contact` 汇报、提问或描述协调需求，metadata 可带本机任务 ID；平台 `send_agent_message` 回复时将原联系消息的 `id` 放进 `correlation_id`。操作申请使用稳定 `request_id`，`request.resolved` 的 payload 与 correlation_id 都关联它。没有收到结果就是待处理，超时不代表许可；查询结果不能通过重发新申请代替，避免副作用重复。双方不直接拨号连接其他 agent。

`agent.request.metadata.task_id` 与 `agent.contact.metadata.task_id` 可标识当前工作。平台只有验证该任务确实属于发送身份后，才将请求送回其目标会话；关联信息不会授予额外管理权限。此 metadata 为协议 1.0 的可选字段；未携带字段的旧请求保留原标准化内容与消息指纹，升级后重投仍可去重。不要在重传旧消息时补写字段，应为新内容创建新消息 ID。

横向协作继续使用 `agent.request` / `request.resolved`，私信和讨论通过新增服务端专用 `collaboration.message` 下发。`list_agents` 等只读发现直接处理；成员、来源任务、接收身份和审批边界由平台校验。传输 ACK 仅升级为 delivered，read/processed 需收件人显式上报。接收者应容纳此协议 1.0 的新增类型；旧适配器可继续使用原工具，新协作工具需相应客户端支持。详见[协作协议语义](collaboration.md)。

心跳 metadata 可携带 `execution_mode`、`state`、`active_task_ids`、`queued_tasks`、`capacity`、`adapter`、`workspace`、`workspace_id`、`supports_resume`、`notification_mode` 与阻塞说明。当前参考客户端区分 `waiting_for_claim`、`idle`、`busy`、`blocked`；这些是客户端观察值，不证明原生模型登录、权限或未来任务一定成功。旧客户端不提供时，平台保留“未知”能力，而不是把 online 当成自动可执行。

目标和依赖是平台调度状态，不新增 agent 必须上报的状态。等待依赖时平台任务为 `blocked`，通过验收后才发送普通 `task.dispatch`；后续任务通过 `parameters.chiliarch_context` 取得目标约束、依赖结果和产物引用。此数据不改变本地执行策略。详见 [目标、依赖与验收](coordination.md)。

扩展消息使用非保留命名空间，如 `example.telemetry`。`task`、`agent`、`request` 保留给协议。扩展消息不会自动变成管理员动作；可通过服务事件监听处理，并为生产扩展增加 payload 校验。扩展操作使用注册工具的 JSON Schema，见扩展文档。

## 传输降级

1. **WS**：agent 建立 `/api/agent/ws`，双方发送完整信封 JSON。平台返回 ACK 信封，收到 ACK 不再回复 ACK。
2. **SSE＋POST**：agent 建立 `GET /api/agent/events` 接收 `event: message`；向 `POST /api/agent/messages` 上传完整信封和 ACK。POST 响应为 `{ack, duplicate}`，ack 可为空。
3. **长轮询**：agent 请求 `GET /api/agent/poll?timeout=<秒>`，获得 `{messages:[...]}`；上行继续使用 POST。

SSE `id/Last-Event-ID` 不是业务 ACK，不能据此删除队列。参考客户端只使用业务 ACK，协议去重在切换传输后继续有效。

参考客户端的持久出站消息，以及服务端 WS/SSE 下行消息，分别按消息 ID 记录当前连接的重投期限。首次发送立即进行，未确认时从 `retry_seconds` 开始指数退避，最长等待 `max(retry_seconds, offline_after_seconds)`；新消息可以直接发送，无关查询、心跳或其他消息的通知不会提前重投旧消息。ACK 仍按原有持久队列规则处理，已确认的消息停止重投；未确认消息在重连/切换传输后按原 ID 立即重投。服务端长轮询直接返回持久队列，由客户端逐条接收并通过 POST 确认；SSE 退避期间继续发送 keepalive。

同进程的邮箱事务提交后立即唤醒出站发送器，避免新请求、任务进度和结果等待下一个重试周期；取消期间实际完成的提交也会通知，回滚不会通知。独立进程写入同一状态库时仍由周期轮询发现。通知只触发检查持久队列，不改变旧消息的重投期限或心跳间隔。

heartbeat 与 ACK 是瞬时控制帧，不进入持久业务 inbox；心跳更新活性和元数据，ACK 更新已有 outbox 的确认状态。

参考客户端每条 WS 连接最多保留一个未确认心跳，等待其 ACK 时继续发送业务消息和对端消息的 ACK，避免慢速持久化造成心跳积压。心跳超过 `offline_after_seconds` 仍未确认时，按连接故障进入既有降级链；匹配的心跳 ACK 仅更新连接状态，不写本地数据库。SSE／长轮询的上行 POST 本身等待响应，无需额外排队心跳。

出站连接上下文停止时先允许当前 HTTP POST 收到确认，再关闭接收流；这段等待以 `poll_timeout_seconds` 为限，超时后取消并完成原有资源清理。停止或重复取消不会提前释放仍被连接使用的数据库与身份锁。未确认的持久消息保留到下次连接，停止不承诺清空全部队列。

网络故障采用退避和抖动，定期重新尝试 WS。401/403 凭证失败、422 协议错误、409 内容冲突或超限等永久错误必须修正或重新配对，不能无限降级。部分网关单独拒绝 WS 时，参考客户端通过主动 HTTP 心跳核验凭证；身份仍有效才继续降级。SSE 吊销事件为 `event: revoked`，客户端应退出并停止使用该凭证。

## 客户端持久化顺序

接收命令 → 持久 inbox 与任务去重记录 → ACK → 报 accepted/running → 执行 → 在同一本地事务保存终态和待上传结果 → 重传结果直至平台 ACK。

必须同时按消息 ID 和任务 ID 去重。收到相同 task ID 的不同指令是冲突，不允许覆盖。客户端执行器必须支持协作取消，并区别用户取消与进程停机。参考实现停止时保留 running ledger，重新启动报告 interrupted；不会自动再次产生外部副作用。

取消的持久处理与 ACK 必须一致：先将取消 tombstone 与取消意图在同一事务中提交，再 ACK；未开始的任务可同时保存取消终态，运行中的任务先保留本地 `cancelling`。托管 worker 清理完进程组或当前会话调用 `confirm_cancellation` 后，才向平台报告 `cancelled`。即使取消先于 dispatch 到达，后续相同任务也不能执行；晚到的取消不能覆盖已经完成的结果。MCP 当前会话通过原子领取开始工作，相同结果重试幂等，取消、失败和中断后的任务不能再报完成。网络 ACK 后，上级回复仍保存在本地业务 inbox，可用 cursor 读取。

队列受 `queue_limit/message_ttl_seconds/max_message_bytes` 约束，过期和取消显式可见。在线状态由 agent 主动请求和心跳更新；服务端仅发送 SSE keepalive 不证明 agent 活性。
