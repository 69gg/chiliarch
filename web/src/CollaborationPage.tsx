import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import {
  EmptyState,
  ErrorNotice,
  MarkdownContent,
  Status,
  time,
} from "./components";
import type {
  Agent,
  CollaborationChannel,
  JsonObject,
  PeerMessage,
  PeerReceipt,
  PlatformState,
  TaskWait,
} from "./types";

const PAGE_SIZE = 100;
const errorText = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);

function mergeMessages(
  previous: PeerMessage[],
  next: PeerMessage[],
): PeerMessage[] {
  return [
    ...new Map(
      [...previous, ...next].map((message) => [message.id, message]),
    ).values(),
  ].sort((a, b) => a.sequence - b.sequence);
}

function usePolling<T>(
  load: () => Promise<T>,
  interval: number,
  revision = 0,
): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll(): Promise<void> {
      try {
        const next = await load();
        if (active) {
          setData(next);
          setError(null);
        }
      } catch (error) {
        if (active) setError(errorText(error));
      } finally {
        if (active) timer = setTimeout(() => void poll(), interval);
      }
    }
    void poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [load, interval, revision]);
  return { data, error };
}

async function loadOverview(): Promise<{
  channels: CollaborationChannel[];
  waits: TaskWait[];
}> {
  const [channels, waits] = await Promise.all([
    api.operate<{ channels: CollaborationChannel[] }>("list_channels", {}),
    api.operate<{ waits: TaskWait[] }>("list_task_waits", {}),
  ]);
  return { ...channels, ...waits };
}

function agentName(state: PlatformState, id: string): string {
  return state.agents.find((agent) => agent.id === id)?.name ?? id;
}

function taskName(state: PlatformState, id: string): string {
  const task = state.tasks.find((task) => task.id === id);
  return task ? `${agentName(state, task.agent_id)} · ${task.instruction}` : id;
}

export default function CollaborationPage({
  state,
  refresh,
}: {
  state: PlatformState;
  refresh: () => void;
}): ReactNode {
  const server = state.config.server as JsonObject | undefined;
  const interval =
    typeof server?.config_poll_seconds === "number"
      ? Math.max(1, server.config_poll_seconds) * 1000
      : 5000;
  const [revision, setRevision] = useState(0);
  const { data, error } = usePolling(loadOverview, interval, revision);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"create" | "direct" | null>(null);
  const channels = data?.channels ?? [];
  const selected =
    channels.find((channel) => channel.id === selectedId) ?? channels[0];
  function reload(): void {
    setRevision((value) => value + 1);
    refresh();
  }

  return (
    <div className="management-page collaboration-page">
      <header className="page-header">
        <h1>协作</h1>
        <div className="actions">
          <button onClick={reload} aria-label="刷新协作">
            刷新
          </button>
          <button onClick={() => setDialog("direct")}>发送私信</button>
          <button className="primary" onClick={() => setDialog("create")}>
            ＋ 新建讨论
          </button>
        </div>
      </header>
      <div className="collaboration-content">
        <ErrorNotice error={error} />
        {!data && !error && (
          <p role="status" className="muted">
            读取协作记录…
          </p>
        )}
        <div className="collaboration-layout">
          <aside className="collaboration-directory" aria-label="协作讨论">
            <h2>讨论</h2>
            <div className="collaboration-channel-list">
              {channels.map((channel) => (
                <button
                  key={channel.id}
                  className={selected?.id === channel.id ? "is-selected" : ""}
                  aria-pressed={selected?.id === channel.id}
                  onClick={() => setSelectedId(channel.id)}
                >
                  <strong>{channel.title}</strong>
                  <small>
                    {channel.members.length} 位 agent
                    {channel.goal_id ? " · 关联目标" : ""}
                  </small>
                </button>
              ))}
              {data && channels.length === 0 && (
                <p className="muted small">尚无讨论</p>
              )}
            </div>
            <section className="collaboration-waits" aria-label="任务等待关系">
              <h2>谁在等待</h2>
              {data?.waits.length === 0 && (
                <p className="muted small">当前没有登记的等待</p>
              )}
              <ul>
                {data?.waits.map((wait) => (
                  <li key={wait.task_id}>
                    <strong>{taskName(state, wait.task_id)}</strong>
                    <span className="muted small">等待</span>
                    <ul>
                      {wait.waiting_for.map((id) => (
                        <li key={id}>{taskName(state, id)}</li>
                      ))}
                    </ul>
                    {wait.reason && <p>{wait.reason}</p>}
                    <time className="muted small">{time(wait.updated_at)}</time>
                  </li>
                ))}
              </ul>
            </section>
          </aside>
          {selected ? (
            <ChannelThread
              key={selected.id}
              channel={selected}
              state={state}
              interval={interval}
              revision={revision}
              onSent={reload}
            />
          ) : (
            <EmptyState title="让 agents 开始讨论">
              围绕一个目标交流进展、请求协助，让交接和决策都有记录。
            </EmptyState>
          )}
        </div>
      </div>
      {dialog && (
        <CollaborationDialog
          title={dialog === "create" ? "新建讨论" : "发送私信"}
          onClose={() => setDialog(null)}
        >
          {dialog === "create" ? (
            <CreateChannel
              state={state}
              onCreated={(channel) => {
                setSelectedId(channel.id);
                setDialog(null);
                reload();
              }}
            />
          ) : (
            <DirectMessage state={state} interval={interval} onSent={reload} />
          )}
        </CollaborationDialog>
      )}
    </div>
  );
}

function ChannelThread({
  channel,
  state,
  interval,
  revision,
  onSent,
}: {
  channel: CollaborationChannel;
  state: PlatformState;
  interval: number;
  revision: number;
  onSent: () => void;
}): ReactNode {
  const cursor = useRef(0);
  const [messages, setMessages] = useState<PeerMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [more, setMore] = useState(false);
  const [pageRevision, setPageRevision] = useState(0);
  const [reply, setReply] = useState<PeerMessage | null>(null);
  const [receiptId, setReceiptId] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll(): Promise<void> {
      try {
        const next = await api.operate<{
          messages: PeerMessage[];
          cursor: number;
        }>("get_channel", {
          channel_id: channel.id,
          after: cursor.current,
          limit: PAGE_SIZE,
        });
        if (!active) return;
        cursor.current = next.cursor;
        setMessages((previous) => mergeMessages(previous, next.messages));
        setMore(next.messages.length === PAGE_SIZE);
        setError(null);
        setLoaded(true);
      } catch (error) {
        if (active) setError(errorText(error));
      } finally {
        if (active) timer = setTimeout(() => void poll(), interval);
      }
    }
    void poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [channel.id, interval, revision, pageRevision]);
  const goal = state.goals?.find((goal) => goal.id === channel.goal_id);
  return (
    <section
      className="collaboration-thread"
      aria-label={`讨论 ${channel.title}`}
    >
      <header className="collaboration-thread-heading">
        <h2>{channel.title}</h2>
        <p className="muted small">
          {channel.members.map((id) => agentName(state, id)).join("、")}
        </p>
        {channel.goal_id && (
          <a
            className="small"
            href={`#goals/${encodeURIComponent(channel.goal_id)}`}
          >
            目标：{goal?.title ?? channel.goal_id}
          </a>
        )}
      </header>
      <ErrorNotice error={error} />
      {!loaded && !error && (
        <p role="status" className="muted small">
          读取讨论消息…
        </p>
      )}
      {loaded && !messages.length && (
        <EmptyState title="还没有消息" compact>
          发一条消息开始讨论。
        </EmptyState>
      )}
      <ol className="collaboration-messages" aria-label="讨论消息">
        {messages.map((message) => (
          <li key={message.id}>
            <article aria-label={`消息 ${message.sequence}`}>
              <div className="collaboration-message-meta">
                <strong>
                  {message.sender_agent_id
                    ? agentName(state, message.sender_agent_id)
                    : message.sender_role === "user"
                      ? "你"
                      : "Chiliarch AI"}
                </strong>
                <time>{time(message.created_at)}</time>
                {message.source_task_id && (
                  <span title={taskName(state, message.source_task_id)}>
                    来自任务 {message.source_task_id}
                  </span>
                )}
              </div>
              {message.reply_to && (
                <p className="muted small">
                  回复{" "}
                  {messages
                    .find((item) => item.id === message.reply_to)
                    ?.message.slice(0, 100) ?? message.reply_to}
                </p>
              )}
              <MarkdownContent content={message.message} />
              <div className="actions collaboration-message-actions">
                <button className="plain" onClick={() => setReply(message)}>
                  回复
                </button>
                <button
                  className="plain"
                  aria-expanded={receiptId === message.id}
                  onClick={() =>
                    setReceiptId(receiptId === message.id ? null : message.id)
                  }
                >
                  查看回执
                </button>
              </div>
              {receiptId === message.id && (
                <MessageReceipts
                  key={message.id}
                  messageId={message.id}
                  state={state}
                  interval={interval}
                />
              )}
            </article>
          </li>
        ))}
      </ol>
      {more && (
        <div className="collaboration-history-status">
          <p className="muted small">
            历史消息尚未全部加载，新发送的消息会立即显示。
          </p>
          <button onClick={() => setPageRevision((value) => value + 1)}>
            读取后续消息
          </button>
        </div>
      )}
      {sent && (
        <p className="muted small" role="status">
          消息已发送
        </p>
      )}
      <MessageComposer
        key={channel.id}
        target={{ channel_id: channel.id }}
        reply={reply}
        onClearReply={() => setReply(null)}
        onSent={(message) => {
          // Preserve the history cursor; appending this confirmed message must
          // not skip older pages that are still being fetched.
          setMessages((previous) => mergeMessages(previous, [message]));
          setSent(true);
          setReply(null);
          onSent();
        }}
      />
    </section>
  );
}

function MessageReceipts({
  messageId,
  state,
  interval,
}: {
  messageId: string;
  state: PlatformState;
  interval: number;
}): ReactNode {
  const load = useCallback(
    () =>
      api.operate<{ message: PeerMessage; receipts: PeerReceipt[] }>(
        "get_peer_message",
        { message_id: messageId },
      ),
    [messageId],
  );
  const { data, error } = usePolling(load, interval);
  return (
    <div className="collaboration-receipts" aria-label="消息回执">
      <ErrorNotice error={error} />
      {!data && !error && (
        <p role="status" className="muted small">
          读取回执…
        </p>
      )}
      {data && (
        <>
          <p className="muted small">
            已投递表示本机持久接收；已读取和已处理由接收 agent 确认。
          </p>
          {data.receipts.length ? (
            <ul>
              {data.receipts.map((receipt) => (
                <li key={receipt.agent_id}>
                  <span>{agentName(state, receipt.agent_id)}</span>
                  <Status value={receipt.state} />
                  {receipt.wake_task_id && (
                    <span className="muted small">
                      唤醒任务：{taskName(state, receipt.wake_task_id)}
                    </span>
                  )}
                  <time className="muted small">
                    {time(receipt.updated_at)}
                  </time>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">没有接收方回执</p>
          )}
        </>
      )}
    </div>
  );
}

function MessageComposer({
  target,
  reply,
  onClearReply,
  onSent,
  disabled = false,
  wake,
}: {
  target: JsonObject;
  reply?: PeerMessage | null;
  onClearReply?: () => void;
  onSent: (message: PeerMessage) => void;
  disabled?: boolean;
  wake?: ReactNode;
}): ReactNode {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function send(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (busy || disabled || !draft.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.operate<{ message: PeerMessage }>(
        "send_peer_message",
        {
          ...target,
          message: draft.trim(),
          ...(reply ? { reply_to: reply.id } : {}),
        },
      );
      if (mounted.current) {
        setDraft("");
        onSent(result.message);
      }
    } catch (error) {
      if (mounted.current) setError(errorText(error));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <form
      className="collaboration-composer"
      onSubmit={(event) => void send(event)}
    >
      {reply && (
        <div className="collaboration-reply">
          <span>回复：{reply.message.slice(0, 160)}</span>
          <button type="button" className="plain" onClick={onClearReply}>
            取消回复
          </button>
        </div>
      )}
      <label>
        消息内容
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          rows={3}
          disabled={busy}
          required
          placeholder="分享进展、补充资料或请求协助…"
        />
      </label>
      {wake}
      <ErrorNotice error={error} />
      <div className="actions">
        <button
          className="primary"
          disabled={busy || disabled || !draft.trim()}
        >
          {busy ? "发送中…" : "发送消息"}
        </button>
      </div>
    </form>
  );
}

function CollaborationDialog({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}): ReactNode {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="agent-pairing-dialog collaboration-modal"
      aria-label={title}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <header className="row-between">
        <h2>{title}</h2>
        <button className="plain" onClick={onClose} aria-label="关闭对话框">
          ×
        </button>
      </header>
      {children}
    </dialog>
  );
}

function CreateChannel({
  state,
  onCreated,
}: {
  state: PlatformState;
  onCreated: (channel: CollaborationChannel) => void;
}): ReactNode {
  const [title, setTitle] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const [goalId, setGoalId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function create(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.operate<{ channel: CollaborationChannel }>(
        "create_channel",
        {
          title: title.trim(),
          members,
          ...(goalId ? { goal_id: goalId } : {}),
        },
      );
      if (mounted.current) onCreated(result.channel);
    } catch (error) {
      if (mounted.current) setError(errorText(error));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <form
      className="collaboration-form"
      onSubmit={(event) => void create(event)}
    >
      <label>
        讨论名称
        <input
          autoFocus
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          required
          maxLength={256}
        />
      </label>
      <fieldset>
        <legend>参与的 agents</legend>
        {state.agents
          .filter((agent) => !agent.revoked)
          .map((agent) => (
            <label className="checkbox-field" key={agent.id}>
              <input
                type="checkbox"
                checked={members.includes(agent.id)}
                onChange={(event) =>
                  setMembers((previous) =>
                    event.target.checked
                      ? [...previous, agent.id]
                      : previous.filter((id) => id !== agent.id),
                  )
                }
              />
              {agent.name} · {agent.machine}
              <Status value={agent.online ? "online" : "offline"} />
            </label>
          ))}
        {!state.agents.some((agent) => !agent.revoked) && (
          <p className="muted small">先在 Agents 管理中添加 agent。</p>
        )}
      </fieldset>
      <label>
        关联目标（可选）
        <select
          value={goalId}
          onChange={(event) => setGoalId(event.target.value)}
        >
          <option value="">不关联目标</option>
          {state.goals?.map((goal) => (
            <option key={goal.id} value={goal.id}>
              {goal.title}
            </option>
          ))}
        </select>
      </label>
      <ErrorNotice error={error} />
      <div className="actions">
        <button
          className="primary"
          disabled={busy || !title.trim() || !members.length}
        >
          {busy ? "创建中…" : "创建讨论"}
        </button>
      </div>
    </form>
  );
}

function canWake(agent?: Agent): boolean {
  return (
    !!agent && !agent.revoked && agent.metadata.execution_mode === "managed"
  );
}

function DirectMessage({
  state,
  interval,
  onSent,
}: {
  state: PlatformState;
  interval: number;
  onSent: () => void;
}): ReactNode {
  const agents = state.agents.filter((agent) => !agent.revoked);
  const [recipientId, setRecipientId] = useState(agents[0]?.id ?? "");
  const [taskId, setTaskId] = useState("");
  const [wake, setWake] = useState(false);
  const [sent, setSent] = useState<PeerMessage | null>(null);
  const recipient = agents.find((agent) => agent.id === recipientId);
  const selectedTask = state.tasks.find(
    (task) => task.id === taskId && task.agent_id === recipientId,
  );
  const taskGoalId = state.task_links?.find(
    (link) => link.task_id === taskId,
  )?.goal_id;
  const taskGoal = state.goals?.find((goal) => goal.id === taskGoalId);
  const missingTask = !!taskId && !selectedTask;
  const wakeReason = !canWake(recipient)
    ? "该 agent 未上报自动执行能力；消息可送达，需由现有会话主动读取。"
    : missingTask
      ? "接收方任务已不可用，请重新选择任务。"
      : selectedTask && selectedTask.status !== "completed"
        ? "只有已完成的任务可唤醒接续执行。可先发送消息，由接收方主动读取。"
        : taskGoal && ["completed", "cancelled"].includes(taskGoal.status)
          ? "该任务所属目标已关闭，不能创建接续执行。仍可发送消息供接收方读取。"
          : null;
  const wakeAvailable = wakeReason === null;
  return (
    <div className="collaboration-form">
      <label>
        接收 agent
        <select
          value={recipientId}
          onChange={(event) => {
            setRecipientId(event.target.value);
            setTaskId("");
            setWake(false);
          }}
          disabled={!agents.length}
        >
          {!agents.length && <option value="">尚无可用 agent</option>}
          {agents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.name} · {agent.machine}
            </option>
          ))}
        </select>
      </label>
      <label>
        关联接收方任务（可选）
        <select
          value={taskId}
          onChange={(event) => {
            setTaskId(event.target.value);
            setWake(false);
          }}
        >
          <option value="">发送给 agent</option>
          {state.tasks
            .filter((task) => task.agent_id === recipientId)
            .map((task) => (
              <option key={task.id} value={task.id}>
                {task.instruction}
              </option>
            ))}
        </select>
      </label>
      {selectedTask && (
        <p className="muted small">
          接收任务状态：
          <Status value={selectedTask.status} />
        </p>
      )}
      <MessageComposer
        target={{
          recipient_agent_id: recipientId,
          ...(taskId ? { recipient_task_id: taskId } : {}),
          wake: wakeAvailable && wake,
        }}
        disabled={!recipient || missingTask}
        onSent={(message) => {
          setSent(message);
          onSent();
        }}
        wake={
          <>
            <label className="checkbox-field">
              <input
                type="checkbox"
                checked={wakeAvailable && wake}
                onChange={(event) => setWake(event.target.checked)}
                disabled={!wakeAvailable}
              />
              唤醒 agent 处理来信
            </label>
            <p className="muted small">
              {wakeReason ?? "启用后创建处理任务；接收方离线时等待它主动连接。"}
            </p>
          </>
        }
      />
      {sent && (
        <section className="collaboration-sent">
          <p role="status">
            消息已发送给{" "}
            {agentName(state, sent.recipient_agent_id ?? recipientId)}
          </p>
          <MessageReceipts
            key={sent.id}
            messageId={sent.id}
            state={state}
            interval={interval}
          />
        </section>
      )}
    </div>
  );
}
