import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import {
  ApprovalItem,
  EmptyState,
  ErrorNotice,
  JsonView,
  Status,
  time,
} from "./components";
import type { AgentRequest, JsonObject, PlatformState, Task } from "./types";

export function parseObject(text: string, label: string): JsonObject {
  const result: unknown = JSON.parse(text || "{}");
  if (!result || typeof result !== "object" || Array.isArray(result))
    throw new Error(`${label}必须是 JSON 对象。`);
  return result as JsonObject;
}

export default function AgentsPage({
  state,
  refresh,
}: {
  state: PlatformState;
  refresh: () => void;
}): ReactNode {
  const [selected, setSelected] = useState<string | null>(null);
  const [instruction, setInstruction] = useState("");
  const [parameters, setParameters] = useState("{}");
  const [name, setName] = useState("");
  const [ttl, setTtl] = useState("");
  const [pairing, setPairing] = useState<{
    code: string;
    expires_at: number;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const agent = state.agents.find((item) => item.id === selected) ?? null;
  const tasks = agent
    ? state.tasks.filter((task) => task.agent_id === agent.id)
    : state.tasks;
  const requests = agent
    ? state.requests.filter((request) => request.agent_id === agent.id)
    : state.requests;

  async function operate(
    name: string,
    arguments_: JsonObject,
  ): Promise<JsonObject | null> {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.operate(name, arguments_);
      refresh();
      return result;
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      return null;
    } finally {
      setBusy(false);
    }
  }
  async function createPairing(event: FormEvent): Promise<void> {
    event.preventDefault();
    const result = await operate(
      "create_pairing_code",
      ttl ? { ttl_seconds: Number(ttl) } : {},
    );
    if (
      result &&
      typeof result.code === "string" &&
      typeof result.expires_at === "number"
    )
      setPairing({ code: result.code, expires_at: result.expires_at });
  }
  async function dispatch(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!agent) return;
    let parsed: JsonObject;
    try {
      parsed = parseObject(parameters, "任务参数");
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      return;
    }
    const result = await operate("dispatch_task", {
      agent_id: agent.id,
      instruction: instruction.trim(),
      parameters: parsed,
    });
    if (result) {
      setInstruction("");
      setNotice("指令已受理，可在任务列表查看投递与执行状态。");
    }
  }

  async function copyPairing(code: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(code);
      setNotice("注册码已复制。");
    } catch {
      setError("无法访问剪贴板，请手动复制注册码。");
    }
  }

  return (
    <div className="management-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">手动调度</p>
          <h1>agents</h1>
        </div>
        <div className="header-meta">
          <span>
            {state.agents.filter((item) => item.online && !item.revoked).length}{" "}
            在线
          </span>
          <span>{state.agents.length} 已配对</span>
        </div>
      </header>
      <div className="management-content">
        <ErrorNotice error={error} onDismiss={() => setError(null)} />
        {notice && (
          <div className="notice" role="status">
            {notice}
          </div>
        )}
        <section className="pairing-section">
          <div>
            <h2>配对新 agent</h2>
            <p className="muted">
              在 agent 所在机器使用注册码，主动连接此平台。
            </p>
          </div>
          <form
            className="inline-form"
            onSubmit={(event) => void createPairing(event)}
          >
            <label>
              有效期（秒）
              <input
                aria-label="注册码有效期"
                type="number"
                min="1"
                value={ttl}
                onChange={(event) => setTtl(event.target.value)}
                placeholder="默认有效期"
              />
            </label>
            <button className="primary" disabled={busy}>
              创建一次性注册码
            </button>
          </form>
          {pairing && (
            <div className="pairing-result">
              <div>
                <span className="muted small">一次性注册码</span>
                <code>{pairing.code}</code>
              </div>
              <span className="muted small">
                到期：{time(pairing.expires_at)}
              </span>
              <button onClick={() => void copyPairing(pairing.code)}>
                复制
              </button>
            </div>
          )}
        </section>
        <div className="agents-layout">
          <section className="agent-directory" aria-label="agents 列表">
            <div className="row-between">
              <h2>已配对的 agents</h2>
              <button className="plain" onClick={refresh}>
                刷新
              </button>
            </div>
            {state.agents.length === 0 ? (
              <EmptyState compact title="尚未连接 agent">
                创建注册码，然后在 agent 所在机器发起配对。
              </EmptyState>
            ) : (
              <div className="agent-table" aria-label="已配对 agents">
                <div className="table-heading" aria-hidden="true">
                  <span>名称 / 机器</span>
                  <span>状态</span>
                  <span>传输</span>
                </div>
                {state.agents.map((item) => (
                  <button
                    key={item.id}
                    className={`agent-row ${selected === item.id ? "selected" : ""}`}
                    onClick={() => {
                      setSelected(item.id);
                      setName(item.name);
                      setNotice(null);
                    }}
                    aria-pressed={selected === item.id}
                    aria-label={`选择 ${item.name}，机器 ${item.machine || "未提供"}`}
                  >
                    <span>
                      <strong>{item.name}</strong>
                      <small>{item.machine || "未提供机器名称"}</small>
                    </span>
                    <Status
                      value={
                        item.revoked
                          ? "revoked"
                          : item.online
                            ? "online"
                            : "offline"
                      }
                    />
                    <span className="small muted">
                      {item.transport || String(item.metadata.transport ?? "—")}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </section>
          <section className="agent-inspector" aria-label="agent 详情">
            {agent ? (
              <>
                <div className="row-between">
                  <h2>{agent.name}</h2>
                  <Status
                    value={
                      agent.revoked
                        ? "revoked"
                        : agent.online
                          ? "online"
                          : "offline"
                    }
                  />
                </div>
                <dl className="metadata-list">
                  <div>
                    <dt>机器</dt>
                    <dd>{agent.machine || "尚未提供"}</dd>
                  </div>
                  <div>
                    <dt>身份</dt>
                    <dd className="mono">{agent.id}</dd>
                  </div>
                  <div>
                    <dt>最后心跳</dt>
                    <dd>{time(agent.last_seen)}</dd>
                  </div>
                  <div>
                    <dt>{agent.online ? "当前传输" : "上次传输"}</dt>
                    <dd>
                      {agent.transport ||
                        String(agent.metadata.transport ?? "尚无连接")}
                    </dd>
                  </div>
                  <div>
                    <dt>能力</dt>
                    <dd>
                      {agent.capabilities.length
                        ? agent.capabilities.join(" · ")
                        : "尚未声明能力"}
                    </dd>
                  </div>
                </dl>
                <form
                  className="dispatch-form"
                  onSubmit={(event) => void dispatch(event)}
                >
                  <h3>发号指令</h3>
                  <label htmlFor="instruction">任务说明</label>
                  <textarea
                    id="instruction"
                    rows={4}
                    placeholder="说明需要完成的工作…"
                    value={instruction}
                    onChange={(event) => setInstruction(event.target.value)}
                    required
                    disabled={agent.revoked}
                  />
                  <details>
                    <summary>附加参数</summary>
                    <label htmlFor="task-parameters" className="small muted">
                      JSON 对象
                    </label>
                    <textarea
                      id="task-parameters"
                      className="mono"
                      rows={4}
                      value={parameters}
                      onChange={(event) => setParameters(event.target.value)}
                    />
                  </details>
                  <button
                    className="primary"
                    disabled={busy || agent.revoked || !instruction.trim()}
                  >
                    {agent.online ? "派发任务" : "加入离线队列"}
                  </button>
                </form>
                <details className="agent-settings">
                  <summary>名称与凭证</summary>
                  <form
                    className="rename-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void operate("rename_agent", {
                        agent_id: agent.id,
                        name: name.trim(),
                      });
                    }}
                  >
                    <label htmlFor="agent-name">名称</label>
                    <input
                      id="agent-name"
                      value={name}
                      onChange={(event) => setName(event.target.value)}
                      required
                    />
                    <button disabled={busy || !name.trim()}>保存名称</button>
                  </form>
                  {agent.revoked ? (
                    <p className="muted small">此身份的凭证已吊销。</p>
                  ) : (
                    <details className="revoke-section">
                      <summary>吊销长期凭证</summary>
                      <p className="small muted">
                        该 agent 的现有连接会终止，重新连接需要重新配对。
                      </p>
                      <button
                        className="danger"
                        disabled={busy}
                        onClick={() =>
                          void operate("revoke_agent", { agent_id: agent.id })
                        }
                      >
                        吊销 {agent.name} 的凭证
                      </button>
                    </details>
                  )}
                </details>
              </>
            ) : (
              <EmptyState compact title="选择一个 agent">
                查看能力、心跳和连接状态，或手动派发任务。
              </EmptyState>
            )}
          </section>
        </div>
        <section className="task-section">
          <div className="row-between">
            <h2>{agent ? `${agent.name} 的任务` : "全部任务"}</h2>
            {agent && (
              <button className="plain" onClick={() => setSelected(null)}>
                查看全部
              </button>
            )}
          </div>
          {tasks.length === 0 ? (
            <p className="empty-line">尚无任务</p>
          ) : (
            tasks.map((task) => (
              <TaskItem
                key={task.id}
                task={task}
                busy={busy}
                cancel={() => void operate("cancel_task", { task_id: task.id })}
              />
            ))
          )}
        </section>
        <section className="request-section">
          <h2>{agent ? `${agent.name} 的请求` : "agent 请求"}</h2>
          {requests.length === 0 ? (
            <p className="empty-line">尚无请求</p>
          ) : (
            requests.map((request) => (
              <AgentRequestItem
                key={request.id}
                request={request}
                busy={busy}
                decide={(decision) =>
                  void operate("resolve_agent_request", {
                    request_id: request.id,
                    decision,
                  })
                }
              />
            ))
          )}
        </section>
        {state.approvals.some((approval) =>
          ["pending", "awaiting_approval"].includes(approval.status),
        ) && (
          <section>
            <h2>等待许可</h2>
            {state.approvals
              .filter((approval) =>
                ["pending", "awaiting_approval"].includes(approval.status),
              )
              .map((approval) => (
                <ApprovalItem
                  key={approval.id}
                  approval={approval}
                  onResolved={refresh}
                />
              ))}
          </section>
        )}
      </div>
    </div>
  );
}

function TaskItem({
  task,
  busy,
  cancel,
}: {
  task: Task;
  busy: boolean;
  cancel: () => void;
}): ReactNode {
  const terminal = [
    "completed",
    "succeeded",
    "failed",
    "cancelled",
    "canceled",
    "expired",
    "unknown",
    "interrupted",
  ].includes(task.status);
  return (
    <details className="task-entry">
      <summary>
        <span>
          <strong>{task.instruction}</strong>
          <small>{time(task.updated_at)}</small>
        </span>
        <Status value={task.status} />
      </summary>
      <div className="task-body">
        <dl className="metadata-list">
          <div>
            <dt>任务 ID</dt>
            <dd className="mono">{task.id}</dd>
          </div>
          <div>
            <dt>agent</dt>
            <dd className="mono">{task.agent_id}</dd>
          </div>
          <div>
            <dt>来源</dt>
            <dd>{task.requested_by}</dd>
          </div>
        </dl>
        {task.progress !== null &&
          task.progress !== undefined &&
          (typeof task.progress === "number" ? (
            <div className="task-progress">
              <div className="row-between">
                <span>执行进度</span>
                <span>{Math.round(task.progress * 100)}%</span>
              </div>
              <progress aria-label="执行进度" max={1} value={task.progress} />
            </div>
          ) : (
            <>
              <h4>进度</h4>
              <JsonView value={task.progress} />
            </>
          ))}
        {task.message && <p>{task.message}</p>}
        {task.error && <ErrorNotice error={task.error} />}
        {task.result !== null && (
          <>
            <h4>结果</h4>
            <JsonView value={task.result} />
          </>
        )}
        {!terminal && (
          <button disabled={busy} onClick={cancel}>
            请求取消任务
          </button>
        )}
      </div>
    </details>
  );
}

function AgentRequestItem({
  request,
  busy,
  decide,
}: {
  request: AgentRequest;
  busy: boolean;
  decide: (decision: string) => void;
}): ReactNode {
  const pending = ["pending", "awaiting_approval"].includes(request.status);
  return (
    <section className="request-entry">
      <div className="row-between">
        <strong>{request.operation}</strong>
        <Status value={request.status} />
      </div>
      <p>{request.message}</p>
      <p className="muted small">
        来自 {request.agent_id} · {time(request.created_at)}
      </p>
      <details>
        <summary>参数与结果</summary>
        <JsonView value={request.arguments} />
        {request.result !== null && <JsonView value={request.result} />}
      </details>
      {pending && (
        <div className="actions">
          <button
            className="primary"
            disabled={busy}
            onClick={() => decide("approve")}
          >
            允许请求
          </button>
          <button disabled={busy} onClick={() => decide("reject")}>
            拒绝请求
          </button>
        </div>
      )}
    </section>
  );
}
