import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import { AgentExecution, executionLabel } from "./AgentExecution";
import {
  ApprovalItem,
  EmptyState,
  ErrorNotice,
  JsonView,
  Status,
  time,
} from "./components";
import type {
  Agent,
  AgentRequest,
  Approval,
  Goal,
  JsonObject,
  PlatformState,
  Task,
} from "./types";

export function parseObject(text: string, label: string): JsonObject {
  const result: unknown = JSON.parse(text || "{}");
  if (!result || typeof result !== "object" || Array.isArray(result))
    throw new Error(`${label}必须是 JSON 对象。`);
  return result as JsonObject;
}

const pendingStatuses = new Set(["pending", "awaiting_approval"]);
const queuedStatuses = new Set(["queued", "delivered", "accepted"]);

function connectionStatus(agent: Agent): string {
  return agent.revoked ? "revoked" : agent.online ? "online" : "offline";
}

function transportName(agent: Agent): string {
  const value = agent.transport ?? agent.metadata.transport;
  if (typeof value !== "string" || !value) return "尚无连接";
  return (
    ({ ws: "WS", sse: "SSE", poll: "长轮询" } as Record<string, string>)[
      value
    ] ?? value
  );
}

function relatedApproval(
  approval: Approval,
  agentId: string,
  tasks: Task[],
  requests: AgentRequest[],
): boolean {
  return (
    approval.arguments.agent_id === agentId ||
    tasks.some((task) => task.id === approval.arguments.task_id) ||
    requests.some((request) => request.id === approval.arguments.request_id)
  );
}

export default function AgentsPage({
  state,
  refresh,
}: {
  state: PlatformState;
  refresh: () => void;
}): ReactNode {
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [parameters, setParameters] = useState("{}");
  const [workdir, setWorkdir] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const cards = useRef(new Map<string, HTMLButtonElement>());
  const lastSelected = useRef<string | null>(null);
  const agent = state.agents.find((item) => item.id === selected) ?? null;
  const tasks = agent
    ? state.tasks.filter((task) => task.agent_id === agent.id)
    : [];
  const requests = agent
    ? state.requests.filter((request) => request.agent_id === agent.id)
    : [];
  const approvals = state.approvals.filter(
    (approval) =>
      pendingStatuses.has(approval.status) &&
      (!agent || relatedApproval(approval, agent.id, tasks, requests)),
  );

  useEffect(() => {
    if (selected) {
      lastSelected.current = selected;
      heading.current?.focus();
    } else if (lastSelected.current) {
      cards.current.get(lastSelected.current)?.focus();
      lastSelected.current = null;
    }
  }, [selected]);

  async function operate(
    operation: string,
    arguments_: JsonObject,
  ): Promise<JsonObject | null> {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.operate(operation, arguments_);
      refresh();
      return result;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
      return null;
    } finally {
      setBusy(false);
    }
  }

  function chooseAgent(item: Agent): void {
    setSelected(item.id);
    setName(item.name);
    setInstruction("");
    setParameters("{}");
    setWorkdir("");
    setError(null);
    setNotice(null);
  }

  async function dispatch(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!agent || agent.revoked || !instruction.trim()) return;
    let parsed: JsonObject;
    try {
      parsed = parseObject(parameters, "任务参数");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
      return;
    }
    const result = await operate("dispatch_task", {
      agent_id: agent.id,
      instruction: instruction.trim(),
      parameters: parsed,
      ...(workdir.trim() ? { workdir: workdir.trim() } : {}),
    });
    if (result) {
      setInstruction("");
      setNotice(
        agent.online
          ? "指令已受理，可在任务列表查看投递与执行状态。"
          : "指令已加入离线队列，agent 上线后接收。",
      );
    }
  }

  return (
    <div className="management-page agents-page">
      <header className="page-header agents-header">
        <div>
          {agent && (
            <button
              className="plain agent-back"
              onClick={() => {
                setSelected(null);
                setError(null);
                setNotice(null);
              }}
            >
              ← 返回 Agents 管理
            </button>
          )}
          <h1 ref={heading} tabIndex={-1}>
            {agent ? agent.name : "Agents 管理"}
          </h1>
        </div>
        <div className="agents-header-actions">
          {agent ? (
            <Status value={connectionStatus(agent)} />
          ) : (
            <span className="muted small">
              {
                state.agents.filter((item) => item.online && !item.revoked)
                  .length
              }{" "}
              在线 · {state.agents.length} 已配对
            </span>
          )}
          <button onClick={refresh} aria-label="刷新 agents">
            刷新
          </button>
          <button className="primary" onClick={() => setAdding(true)}>
            ＋ 添加 agent
          </button>
        </div>
      </header>
      <div
        className={`management-content agents-content${state.agents.length === 0 ? " is-empty" : ""}`}
      >
        <ErrorNotice error={error} onDismiss={() => setError(null)} />
        {notice && (
          <div className="notice" role="status">
            {notice}
          </div>
        )}
        {!agent ? (
          <section className="agent-fleet" aria-label="agents 列表">
            {state.agents.length === 0 ? (
              <EmptyState title="尚未连接 agent">
                点击添加 agent，在它所在的机器完成配对。
              </EmptyState>
            ) : (
              <div className="agent-card-grid">
                {state.agents.map((item) => {
                  const agentTasks = state.tasks.filter(
                    (task) => task.agent_id === item.id,
                  );
                  const agentRequests = state.requests.filter(
                    (request) => request.agent_id === item.id,
                  );
                  const running = agentTasks.filter(
                    (task) => task.status === "running",
                  ).length;
                  const queued = agentTasks.filter((task) =>
                    queuedStatuses.has(task.status),
                  ).length;
                  const pendingRequests = agentRequests.filter((request) =>
                    pendingStatuses.has(request.status),
                  ).length;
                  const pendingApprovals = state.approvals.filter(
                    (approval) =>
                      pendingStatuses.has(approval.status) &&
                      relatedApproval(
                        approval,
                        item.id,
                        agentTasks,
                        agentRequests,
                      ),
                  ).length;
                  const pending = pendingRequests + pendingApprovals;
                  return (
                    <button
                      key={item.id}
                      className={`agent-machine-card ${item.revoked ? "is-revoked" : ""}`}
                      aria-label={`查看 ${item.name}，机器 ${item.machine || "未提供"}`}
                      aria-describedby={`agent-${item.id}-status agent-${item.id}-facts agent-${item.id}-pending`}
                      onClick={() => chooseAgent(item)}
                      ref={(node) => {
                        if (node) cards.current.set(item.id, node);
                        else cards.current.delete(item.id);
                      }}
                    >
                      <div className="agent-card-heading">
                        <strong>{item.name}</strong>
                        <span id={`agent-${item.id}-status`}>
                          <Status value={connectionStatus(item)} />
                        </span>
                      </div>
                      <p className="agent-card-machine">
                        {item.machine || "未提供机器名称"}
                      </p>
                      <p className="agent-execution-label">
                        {executionLabel(item)}
                      </p>
                      <dl
                        className="agent-card-facts"
                        id={`agent-${item.id}-facts`}
                      >
                        <div>
                          <dt>
                            {item.online && !item.revoked
                              ? "当前传输"
                              : "上次传输"}
                          </dt>
                          <dd>{transportName(item)}</dd>
                        </div>
                        <div>
                          <dt>运行任务</dt>
                          <dd>
                            {running}
                            <small>
                              {queued ? `${queued} 待执行` : "无排队任务"}
                            </small>
                          </dd>
                        </div>
                      </dl>
                      <div
                        className="agent-card-footer"
                        id={`agent-${item.id}-pending`}
                      >
                        <span
                          className={pending ? "agent-pending-label" : "muted"}
                        >
                          {pending > 0 && (
                            <span
                              className="agent-pending-badge"
                              aria-label={`${pending} 项待处理：${pendingRequests} 个请求，${pendingApprovals} 个审批`}
                            >
                              {pending}
                            </span>
                          )}
                          {pending
                            ? `${pendingRequests} 请求 · ${pendingApprovals} 审批`
                            : "没有待处理事项"}
                        </span>
                        <span aria-hidden="true">查看详情 ↗</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </section>
        ) : (
          <div className="agent-detail" aria-label="agent 详情">
            <AgentCockpit
              agent={agent}
              tasks={tasks}
              requests={requests}
              approvals={approvals}
            />
            {!agent.online && !agent.revoked && (
              <p className="agent-offline-note" role="status">
                此 agent
                当前离线；任务状态为最后一次上报。新指令进入离线队列，等待它主动连接。
              </p>
            )}
            <div className="agent-detail-workspace">
              <section
                className="agent-dispatch"
                aria-labelledby="dispatch-title"
              >
                <div className="agent-section-heading">
                  <h2 id="dispatch-title">派发任务</h2>
                  <p className="muted">
                    向 {agent.machine || agent.name} 发出指令。
                  </p>
                </div>
                <form
                  className="dispatch-form"
                  onSubmit={(event) => void dispatch(event)}
                >
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
                  <label htmlFor="task-workdir">工作目录（可选）</label>
                  <input
                    id="task-workdir"
                    value={workdir}
                    onChange={(event) => setWorkdir(event.target.value)}
                    placeholder="留空使用 agent 的默认目录；填写 agent 机器上的绝对路径"
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
                      disabled={agent.revoked}
                    />
                  </details>
                  <button
                    className="primary"
                    disabled={busy || agent.revoked || !instruction.trim()}
                  >
                    {agent.online ? "派发任务" : "加入离线队列"}
                  </button>
                </form>
              </section>
              <section
                className="agent-credentials"
                aria-labelledby="credentials-title"
              >
                <div className="agent-section-heading">
                  <h2 id="credentials-title">名称与凭证</h2>
                  <p className="muted">凭证控制这台机器的连接权限。</p>
                </div>
                <form
                  className="rename-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    if (name.trim() && !agent.revoked)
                      void operate("rename_agent", {
                        agent_id: agent.id,
                        name: name.trim(),
                      });
                  }}
                >
                  <label htmlFor="agent-name">
                    名称
                    <input
                      id="agent-name"
                      value={name}
                      onChange={(event) => setName(event.target.value)}
                      required
                      disabled={agent.revoked}
                    />
                  </label>
                  <button
                    disabled={
                      busy ||
                      agent.revoked ||
                      !name.trim() ||
                      name.trim() === agent.name
                    }
                  >
                    保存名称
                  </button>
                </form>
                {agent.revoked ? (
                  <p className="agent-revoked-note">
                    此身份的凭证已吊销。重新连接需要创建注册码并重新配对。
                  </p>
                ) : (
                  <details className="revoke-section">
                    <summary>吊销长期凭证</summary>
                    <p className="small muted">
                      现有连接会终止；重新连接需要重新配对。已执行的工作不会撤回。
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
              </section>
            </div>
            <section
              className="agent-history-section"
              aria-labelledby="agent-tasks-title"
            >
              <div className="row-between">
                <h2 id="agent-tasks-title">任务</h2>
                <span className="muted small">{tasks.length} 项</span>
              </div>
              {tasks.length === 0 ? (
                <p className="empty-line">尚无任务</p>
              ) : (
                tasks.map((task) => (
                  <TaskItem
                    key={task.id}
                    task={task}
                    goal={state.goals?.find(
                      (goal) =>
                        goal.id ===
                        state.task_links?.find(
                          (link) => link.task_id === task.id,
                        )?.goal_id,
                    )}
                    busy={busy || agent.revoked}
                    cancel={() =>
                      void operate("cancel_task", { task_id: task.id })
                    }
                  />
                ))
              )}
            </section>
            <section
              className="agent-history-section"
              aria-labelledby="agent-requests-title"
            >
              <div className="row-between">
                <h2 id="agent-requests-title">agent 请求</h2>
                <span className="muted small">
                  {
                    requests.filter((request) =>
                      pendingStatuses.has(request.status),
                    ).length
                  }{" "}
                  待处理
                </span>
              </div>
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
          </div>
        )}
        {agent && approvals.length > 0 && (
          <section className="agent-history-section agent-approvals">
            <h2>等待许可</h2>
            {approvals.map((approval) => (
              <ApprovalItem
                key={approval.id}
                approval={approval}
                onResolved={refresh}
              />
            ))}
          </section>
        )}
      </div>
      {adding && (
        <PairingDialog
          state={state}
          refresh={refresh}
          onClose={() => setAdding(false)}
        />
      )}
    </div>
  );
}

function AgentCockpit({
  agent,
  tasks,
  requests,
  approvals,
}: {
  agent: Agent;
  tasks: Task[];
  requests: AgentRequest[];
  approvals: Approval[];
}): ReactNode {
  const running = tasks.filter((task) => task.status === "running");
  const pending = requests.filter((request) =>
    pendingStatuses.has(request.status),
  );
  const telemetry = Object.fromEntries(
    Object.entries(agent.metadata).filter(
      ([key]) => !["transport", "status"].includes(key),
    ),
  );
  return (
    <section className="agent-cockpit" aria-label={`${agent.name} 驾驶舱`}>
      <div className="agent-cockpit-connection">
        <p className="eyebrow">连接状态</p>
        <div className="agent-cockpit-status">
          <Status value={connectionStatus(agent)} />
          <span className="mono">{transportName(agent)}</span>
        </div>
        <dl className="agent-connection-facts">
          <div>
            <dt>机器</dt>
            <dd>{agent.machine || "尚未提供"}</dd>
          </div>
          <div>
            <dt>最后活跃 / 心跳</dt>
            <dd>{time(agent.last_seen)}</dd>
          </div>
          <div>
            <dt>配对时间</dt>
            <dd>{time(agent.created_at)}</dd>
          </div>
          <div>
            <dt>身份</dt>
            <dd className="mono">{agent.id}</dd>
          </div>
        </dl>
        <AgentExecution agent={agent} />
        <div className="agent-capabilities">
          <span className="muted small">已声明能力</span>
          {agent.capabilities.length ? (
            <ul>
              {agent.capabilities.map((capability) => (
                <li key={capability} className="mono">
                  {capability}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">尚未声明能力</p>
          )}
        </div>
        {Object.keys(telemetry).length > 0 && (
          <details className="agent-telemetry">
            <summary>agent 上报信息</summary>
            <JsonView value={telemetry} />
          </details>
        )}
      </div>
      <div className="agent-cockpit-work">
        <div className="agent-work-heading">
          <div>
            <span className="muted small">运行任务</span>
            <strong>{running.length}</strong>
          </div>
          <div>
            <span className="muted small">待处理事项</span>
            <strong>{pending.length + approvals.length}</strong>
            <small className="muted">
              {pending.length} 请求 · {approvals.length} 审批
            </small>
          </div>
        </div>
        {running.length ? (
          <ul className="agent-running-list">
            {running.map((task) => (
              <li key={task.id}>
                <span>{task.instruction}</span>
                {typeof task.progress === "number" && (
                  <div className="agent-running-progress">
                    <progress
                      max={1}
                      value={task.progress}
                      aria-label={`${task.instruction}执行进度`}
                    />
                    <span>{Math.round(task.progress * 100)}%</span>
                  </div>
                )}
                {task.message && (
                  <small className="muted">{task.message}</small>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="agent-idle muted">当前没有运行任务</p>
        )}
      </div>
    </section>
  );
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

function PairingDialog({
  state,
  refresh,
  onClose,
}: {
  state: PlatformState;
  refresh: () => void;
  onClose: () => void;
}): ReactNode {
  const dialog = useRef<HTMLDialogElement>(null);
  const expiryInput = useRef<HTMLInputElement>(null);
  const [ttl, setTtl] = useState("");
  const [pairing, setPairing] = useState<{
    code: string;
    expires_at: number;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const server = state.config.server;
  const publicUrl =
    server &&
    typeof server === "object" &&
    !Array.isArray(server) &&
    typeof server.public_url === "string" &&
    server.public_url
      ? server.public_url
      : window.location.origin;
  const command = pairing
    ? `chiliarch pair --server ${shellQuote(publicUrl)} --code ${shellQuote(pairing.code)} --name '<agent 名称>' --machine '<机器标识>'`
    : "";

  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    if (!element) return;
    element.showModal();
    expiryInput.current?.focus();
    return () => {
      element.close();
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus();
    };
  }, []);

  async function createPairing(event: FormEvent): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const arguments_: JsonObject = {};
      if (ttl.trim()) {
        const seconds = Number(ttl);
        if (!Number.isInteger(seconds) || seconds < 60 || seconds > 86400)
          throw new Error("注册码有效期必须是 60 到 86400 之间的整数秒。");
        arguments_.ttl_seconds = seconds;
      }
      const result = await api.operate("create_pairing_code", arguments_);
      if (
        typeof result.code !== "string" ||
        typeof result.expires_at !== "number"
      )
        throw new Error("服务器未返回有效注册码，请检查日志。");
      setPairing({ code: result.code, expires_at: result.expires_at });
      refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }

  async function copy(value: string, message: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      setNotice(message);
      setError(null);
    } catch {
      setError("无法访问剪贴板，请手动选择并复制。");
    }
  }

  return (
    <dialog
      ref={dialog}
      className="agent-pairing-dialog"
      aria-labelledby="pairing-dialog-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          onClose();
        }
      }}
    >
      <header className="agent-dialog-header">
        <div>
          <p className="eyebrow">主动出站配对</p>
          <h2 id="pairing-dialog-title">添加 agent</h2>
        </div>
        <button
          className="plain"
          type="button"
          onClick={onClose}
          aria-label="关闭添加 agent"
        >
          ✕
        </button>
      </header>
      <p className="muted">
        注册码在 agent 所在机器兑换凭证；平台等待它主动上线。
      </p>
      <ErrorNotice error={error} onDismiss={() => setError(null)} />
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <form
        className="agent-pairing-form"
        onSubmit={(event) => void createPairing(event)}
      >
        <label>
          注册码有效期（秒）
          <input
            ref={expiryInput}
            aria-label="注册码有效期"
            type="number"
            min="60"
            max="86400"
            step="1"
            placeholder="使用服务端默认"
            value={ttl}
            onChange={(event) => setTtl(event.target.value)}
          />
        </label>
        <button className="primary" disabled={busy}>
          {busy ? "生成中…" : pairing ? "重新生成注册码" : "创建一次性注册码"}
        </button>
      </form>
      {pairing && (
        <div className="agent-pairing-result">
          <div className="agent-code-heading">
            <span className="muted small">一次性注册码</span>
            <button
              type="button"
              className="plain"
              onClick={() => void copy(pairing.code, "注册码已复制。")}
            >
              复制注册码
            </button>
          </div>
          <code className="agent-registration-code">{pairing.code}</code>
          <p className="small muted">
            到期：{time(pairing.expires_at)} · 兑换成功后立即失效
          </p>
          <div className="agent-code-heading">
            <span className="muted small">在 agent 机器运行</span>
            <button
              type="button"
              className="plain"
              onClick={() => void copy(command, "配对命令已复制。")}
            >
              复制配对命令
            </button>
          </div>
          <pre className="agent-pairing-command">
            <code>{command}</code>
          </pre>
          <p className="muted small">
            将名称与机器标识替换为实际值；机器必须能够访问上面的平台地址。
          </p>
        </div>
      )}
      <ol className="agent-pairing-steps">
        <li>生成注册码，并在有效期内传给 agent 所在机器。</li>
        <li>使用已安装的 chiliarch 参考客户端或兼容插件完成配对。</li>
        <li>
          保持 agent
          的出站连接，返回列表查看状态并派发任务。随包参考客户端需在配对后运行
          <code className="mono"> chiliarch demo-agent</code>
          ；它只演示调度，不执行真实工作。
        </li>
      </ol>
      <footer className="agent-dialog-footer">
        <button type="button" onClick={onClose}>
          完成并关闭
        </button>
      </footer>
    </dialog>
  );
}

function TaskItem({
  task,
  goal,
  busy,
  cancel,
}: {
  task: Task;
  goal?: Goal;
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
        {goal && (
          <p className="small">
            <a href={`#goals/${encodeURIComponent(goal.id)}`}>
              目标：{goal.title} ↗
            </a>
          </p>
        )}
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
          <div>
            <dt>任务工作目录</dt>
            <dd className="mono">{task.workdir || "使用 agent 默认目录"}</dd>
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
