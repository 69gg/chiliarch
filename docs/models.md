# 模型兼容与总管行为

兼容层参考 Undefined 的请求转换、工具生命周期和原生推理回放设计，独立于其机器人上下文。三种 API 通过相同的 Provider 流事件和操作服务驱动总管。

## 配置模型

| 字段 | 用途 |
|---|---|
| api_mode | chat_completions / responses / anthropic |
| model | 服务商模型名称，空值表示未配置 |
| base_url | 覆盖 SDK 默认地址；填写对应 API 根地址，不填写某次请求的最终 endpoint |
| api_key | 认证 Key，可用环境变量覆盖；自建无认证网关可留空 |
| headers | 自定义请求头，支持网关认证或版本标记 |
| request_params | 模型特有请求参数；不能替换平台 transcript、tools、model、stream 等控制字段 |
| timeout / retries | SDK 请求超时与有限重试 |
| max_tokens | 输出上限，Anthropic 必須是正整数 |
| temperature | 可省略，不接受的模型保持空值 |
| reasoning_enabled / reasoning_effort | 请求模型推理及强度；按 API 映射 |
| thinking_enabled / thinking_budget_tokens | thinking 参数与预算；服务商不支持时关闭 |
| reasoning_replay | 下一轮保留服务商兼容的原生推理载体 |
| include_usage | 请求流式 usage，网关不支持时关闭 |
| system_prompt_as_user | Chat 模式中将系统提示词以 user 角色发送，兼容不接受 system 的网关 |
| tool_choice | 可选的服务商工具选择参数 |

不要把业务消息、工具 Schema 或流式控制字段塞进 request_params；它们由平台生成。保留字段冲突会报错，而非静默覆盖。兼容开关按网关能力配置，不按某几个模型名硬编码。

## 三种 API

**Chat Completions** 使用 OpenAI SDK，流式收集文本、公开 reasoning 字段、分片 tool_calls 和 usage。函数参数直到完整 turn 结束且 JSON Schema 校验通过后才执行。reasoning 原字段可用于下一轮兼容回放。

**Responses** 使用 Responses 事件；保持 message、function_call、function_call_output 与 reasoning items 的原始顺序。初版采用完整历史 stateless replay；不用 previous_response_id 把本地生命周期与远端持久状态绑死。加密 reasoning 作为回放元数据保留，不作为可读思考显示。

**Anthropic Messages** 转换为 text、tool_use 和 tool_result blocks，保留 thinking/redacted_thinking 与签名结构。工具结果与调用匹配，不能把显示文本重新拼成服务商签名块。

可读思考内容或摘要与原生签名/密文分开保存。WebUI 只显示服务商明确公开返回的内容；没有 reasoning 的模型不会生成装饰性“思维链”。关闭 reasoning_replay 过滤推理载体，但保留合法工具调用结构。

## 流式、取消与错误

真正的增量事件在模型 turn 完成前已保存并发送。浏览器通过 SSE 接收 text_delta、reasoning_delta、tool_start、tool_end、usage、approval_required 和运行终态。事件有递增 seq，刷新从同一会话快照的 last_event_seq 续传，避免丢帧或重复拼接。

总管对话页面的会话列表默认收起，可按需展开；小屏幕使用可关闭的会话抽屉。工具行展示名称、脱敏参数、结果、许可状态和执行状态，需要许可时在对应工具行下方直接允许或拒绝。审批按持久 approval_id 绑定具体调用，不按工具名匹配，也不会因刷新或切换会话而移到其他调用。批准/拒绝进入统一服务，批准绑定原参数；总管恢复后取已完成工具结果，不再执行一次。

对话页面不常驻显示下级请求、最近任务或全局审批栏。Chiliarch AI 通过工具处理这些业务；需要手动查看时进入 Agents 管理的对应 agent 详情。

关闭浏览器不停止模型 run。取消会撤回尚未执行的审批；已产生外部副作用的工具不能靠取消“撤销”，应检查操作结果。SDK 只允许有限请求重试；已经产生增量事件的中途流失败不会自动重放整个 turn。

进程中断后，正在运行的模型操作标为 interrupted，等待审批的执行可恢复。恢复保留已存的非秘密模型设置，并使用当前密钥/请求头；嵌套敏感请求参数也从当前配置恢复。若 API 模式、模型或 endpoint 已变，要求用户检查状态再重试，不能把旧调用续到不兼容模型。

## 下级来信

agent.contact 和 agent.request 持久进入下级 inbox，并创建可追溯的关联会话。未配置模型时保持 pending；配置后继续处理。同一消息重传不会重复创建来信。请求内容作为下级消息而非系统指令送入总管，响应和具体操作均经过服务端身份、Schema 和模式检查。

初版不包含厂商插件、模型池、RAG、多模态或文本工具封包恢复。不同网关的扩展不可能只凭名称保证兼容；请使用对应模式和实际行为测试验收。
