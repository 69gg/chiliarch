# agent 协议 1.0

协议版本 `1.0` 与软件版本 `0.1.0` 独立。权威模型在 `chiliarch.protocol`，只依赖 Pydantic，不导入服务端、数据库或模型 SDK。

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
| task.dispatch | 平台 → agent | `task_id`、`instruction`、`parameters` |
| task.cancel | 平台 → agent | `task_id`、`reason` |
| task.status | agent → 平台 | `task_id`、`status`，可选 progress/result/error/message |
| agent.request | agent → 平台 | `request_id`、`operation`、`arguments`，可选 message |
| agent.contact | agent → 平台 | `message`、`metadata` |
| agent.message | 平台 → agent | `message`、`metadata` |
| request.resolved | 平台 → agent | `request_id`、`status`，可选 result/error |

任务上报状态为 `accepted/running/completed/failed/cancelled/interrupted`；progress 在 0–1。`interrupted` 表示执行停止而外部结果可能未知。agent 只能上报分配给自己的任务；终态不能改成另一终态。服务端专用类型不能由 agent 上传。

扩展消息使用非保留命名空间，如 `example.telemetry`。`task`、`agent`、`request` 保留给协议。扩展消息不会自动变成管理员动作；可通过服务事件监听处理，并为生产扩展增加 payload 校验。扩展操作使用注册工具的 JSON Schema，见扩展文档。

## 传输降级

1. **WS**：agent 建立 `/api/agent/ws`，双方发送完整信封 JSON。平台返回 ACK 信封，收到 ACK 不再回复 ACK。
2. **SSE＋POST**：agent 建立 `GET /api/agent/events` 接收 `event: message`；向 `POST /api/agent/messages` 上传完整信封和 ACK。POST 响应为 `{ack, duplicate}`，ack 可为空。
3. **长轮询**：agent 请求 `GET /api/agent/poll?timeout=<秒>`，获得 `{messages:[...]}`；上行继续使用 POST。

SSE `id/Last-Event-ID` 不是业务 ACK，不能据此删除队列。参考客户端只使用业务 ACK，协议去重在切换传输后继续有效。

heartbeat 与 ACK 是瞬时控制帧，不进入持久业务 inbox；心跳更新活性和元数据，ACK 更新已有 outbox 的确认状态。

网络故障采用退避和抖动，定期重新尝试 WS。401/403 凭证失败、422 协议错误、409 内容冲突或超限等永久错误必须修正或重新配对，不能无限降级。部分网关单独拒绝 WS 时，参考客户端通过主动 HTTP 心跳核验凭证；身份仍有效才继续降级。SSE 吊销事件为 `event: revoked`，客户端应退出并停止使用该凭证。

## 客户端持久化顺序

接收命令 → 持久 inbox 与任务去重记录 → ACK → 报 accepted/running → 执行 → 在同一本地事务保存终态和待上传结果 → 重传结果直至平台 ACK。

必须同时按消息 ID 和任务 ID 去重。收到相同 task ID 的不同指令是冲突，不允许覆盖。客户端执行器必须支持协作取消，并区别用户取消与进程停机。参考实现停止时保留 running ledger，重新启动报告 interrupted；不会自动再次产生外部副作用。

队列受 `queue_limit/message_ttl_seconds/max_message_bytes` 约束，过期和取消显式可见。在线状态由 agent 主动请求和心跳更新；服务端仅发送 SSE keepalive 不证明 agent 活性。
