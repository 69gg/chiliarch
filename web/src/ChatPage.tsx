import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import {
  ApprovalItem,
  EmptyState,
  ErrorNotice,
  JsonView,
  MarkdownContent,
  Status,
  time,
  ToolView,
} from "./components";
import type {
  ChatEvent,
  ChatMessage,
  Conversation,
  ConversationDetail,
  Json,
  PlatformState,
  Run,
  ToolCall,
} from "./types";

export interface ChatState {
  messages: ChatMessage[];
  runs: Run[];
  tools: ToolCall[];
}
const ACTIVE_RUN_STATUSES = [
  "running",
  "pending",
  "queued",
  "awaiting_approval",
];
const RUN_EVENT_STATUS: Record<string, string> = {
  run_started: "running",
  approval_required: "awaiting_approval",
  completed: "completed",
  cancelled: "cancelled",
  interrupted: "interrupted",
};

export function applyChatEvent(state: ChatState, event: ChatEvent): ChatState {
  const data = event.data;
  const runId = event.run_id ?? "";
  if (!runId && event.type !== "agent_contact") return state;
  let messages = state.messages;
  let runs = state.runs;
  let tools = state.tools;
  if (["run_started", "text_delta", "reasoning_delta"].includes(event.type)) {
    const id =
      typeof data.message_id === "string"
        ? data.message_id
        : `${runId}:assistant`;
    const existing = messages.find((message) => message.id === id);
    const message: ChatMessage = existing ?? {
      id,
      role: "assistant",
      content: "",
      reasoning: "",
      created_at: Date.now() / 1000,
      run_id: runId,
    };
    const delta = typeof data.delta === "string" ? data.delta : "";
    const next = {
      ...message,
      content: message.content + (event.type === "text_delta" ? delta : ""),
      reasoning:
        message.reasoning + (event.type === "reasoning_delta" ? delta : ""),
    };
    messages = existing
      ? messages.map((item) => (item.id === id ? next : item))
      : [...messages, next];
  }
  if (event.type === "tool_start" || event.type === "approval_required") {
    const id = String(data.id);
    const previous = tools.find(
      (tool) => tool.id === id && tool.run_id === runId,
    );
    const tool: ToolCall = {
      id,
      name: String(data.name),
      arguments: data.arguments ?? previous?.arguments ?? {},
      run_id: runId,
      status: event.type === "approval_required" ? "approval" : "running",
    };
    tools = previous
      ? tools.map((item) =>
          item.id === id && item.run_id === runId ? tool : item,
        )
      : [...tools, tool];
  }
  if (event.type === "tool_end") {
    const id = String(data.id);
    const previous = tools.find(
      (tool) => tool.id === id && tool.run_id === runId,
    );
    const tool: ToolCall = {
      id,
      name: String(data.name),
      arguments: previous?.arguments ?? {},
      result: data.result,
      run_id: runId,
      status: "completed",
    };
    tools = previous
      ? tools.map((item) =>
          item.id === id && item.run_id === runId ? tool : item,
        )
      : [...tools, tool];
  }
  const status =
    event.type === "error"
      ? typeof data.status === "string"
        ? data.status
        : "failed"
      : RUN_EVENT_STATUS[event.type];
  if (status) {
    const previous = runs.find((run) => run.id === runId);
    const run: Run = {
      ...(previous ?? {
        id: runId,
        conversation_id: "",
        created_at: Date.now() / 1000,
      }),
      status,
      updated_at: Date.now() / 1000,
      error:
        event.type === "error" || event.type === "interrupted"
          ? typeof data.message === "string"
            ? data.message
            : undefined
          : undefined,
    };
    runs = previous
      ? runs.map((item) => (item.id === run.id ? run : item))
      : [...runs, run];
  }
  if (
    event.type === "agent_contact" &&
    typeof data.message_id === "string" &&
    !messages.some((message) => message.id === data.message_id)
  ) {
    messages = [
      ...messages,
      {
        id: data.message_id,
        role: "agent",
        content:
          typeof data.content === "string"
            ? data.content
            : JSON.stringify(data.content ?? {}, null, 2),
        reasoning: "",
        created_at: Date.now() / 1000,
        source: String(data.agent_id ?? ""),
      },
    ];
  }
  return { messages, runs, tools };
}

export default function ChatPage({
  state,
  refresh,
  onConfigure,
}: {
  state: PlatformState;
  refresh: () => void;
  onConfigure: () => void;
}): ReactNode {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [chat, setChat] = useState<ChatState>({
    messages: [],
    runs: [],
    tools: [],
  });
  const [content, setContent] = useState("");
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const scrollPinned = useRef(true);
  const model = state.config.model as Record<string, Json> | undefined;
  const configured = Boolean(model?.model);
  const supervisor = state.config.supervisor as
    Record<string, Json> | undefined;
  const mode = String(supervisor?.mode ?? "ask");
  const pending = state.approvals.filter((approval) =>
    ["pending", "awaiting_approval"].includes(approval.status),
  );
  const requests = state.requests.filter((request) =>
    ["pending", "awaiting_approval"].includes(request.status),
  );
  const activeRun = [...chat.runs]
    .reverse()
    .find((run) => ACTIVE_RUN_STATUSES.includes(run.status));
  const failedRun = [...chat.runs]
    .reverse()
    .find(
      (run) =>
        ["failed", "interrupted", "unknown"].includes(run.status) && run.error,
    );

  const loadConversations = useCallback(async (): Promise<void> => {
    try {
      setConversations(await api.conversations());
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    }
  }, []);
  useEffect(() => {
    void loadConversations();
  }, [loadConversations, state]);
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setChat({ messages: [], runs: [], tools: [] });
      return;
    }
    let cancelled = false;
    let stop: (() => void) | undefined;
    setDetail(null);
    setError(null);
    setConnected(false);
    setStreamError(null);
    void api
      .conversation(selected)
      .then((result) => {
        if (cancelled) return;
        setDetail(result);
        setTitle(result.title);
        const hasAgentMessage = result.messages.some(
          (message) => message.role === "agent",
        );
        const restored = (result.events ?? [])
          .filter((event) =>
            ["tool_start", "tool_end", "approval_required"].includes(
              event.type,
            ) || (!hasAgentMessage && event.type === "agent_contact"),
          )
          .reduce(applyChatEvent, {
            messages: result.messages,
            runs: [],
            tools: result.tool_calls ?? [],
          });
        setChat({
          messages: restored.messages,
          runs: result.runs,
          tools: restored.tools,
        });
        stop = api.streamConversation(
          selected,
          result.last_event_seq ?? 0,
          (event) => {
            if (cancelled) return;
            setChat((previous) => applyChatEvent(previous, event));
            if (
              [
                "approval_required",
                "tool_end",
                "completed",
                "cancelled",
                "agent_contact",
                "error",
                "interrupted",
              ].includes(event.type)
            )
              refresh();
            if (
              ["completed", "cancelled", "agent_contact"].includes(event.type)
            )
              void loadConversations();
          },
          (online, error) => {
            if (!cancelled) {
              setConnected(online);
              setStreamError(error?.message ?? null);
            }
          },
        );
      })
      .catch((error: unknown) => {
        if (!cancelled)
          setError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
      stop?.();
    };
  }, [selected, refresh, loadConversations]);
  useEffect(() => {
    if (scrollPinned.current) bottom.current?.scrollIntoView({ block: "end" });
  }, [chat.messages, chat.tools]);

  async function createConversation(): Promise<string> {
    const conversation = await api.createConversation();
    setConversations((previous) => [conversation, ...previous]);
    setSelected(conversation.id);
    return conversation.id;
  }
  async function send(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!content.trim() || busy || activeRun || (selected && !detail)) return;
    scrollPinned.current = true;
    setBusy(true);
    setError(null);
    const text = content.trim();
    try {
      let id = selected;
      const isNew = !id;
      if (!id) {
        const conversation = await api.createConversation();
        setConversations((previous) => [conversation, ...previous]);
        id = conversation.id;
      }
      const result = await api.sendMessage(id, text);
      if (isNew) setSelected(id);
      else
        setChat((previous) => ({
          ...previous,
          messages: [
            ...previous.messages,
            {
              id: `local:${result.run_id}`,
              role: "user",
              content: text,
              reasoning: "",
              created_at: Date.now() / 1000,
            },
          ],
          runs: [
            ...previous.runs,
            {
              id: result.run_id,
              conversation_id: id,
              status: "running",
              created_at: Date.now() / 1000,
              updated_at: Date.now() / 1000,
            },
          ],
        }));
      setContent("");
      void loadConversations();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  async function changeMode(next: string): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await api.operate("update_config", {
        patch: { supervisor: { mode: next } },
      });
      refresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  async function cancel(): Promise<void> {
    if (!activeRun) return;
    setBusy(true);
    setError(null);
    try {
      await api.cancelRun(activeRun.id);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function manageConversation(remove: boolean): Promise<void> {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await api.operate(
        remove ? "delete_conversation" : "rename_conversation",
        remove
          ? { conversation_id: selected }
          : { conversation_id: selected, title: title.trim() },
      );
      if (remove) setSelected(null);
      else
        setDetail((previous) =>
          previous ? { ...previous, title: title.trim() } : previous,
        );
      await loadConversations();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="chat-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">总管</p>
          <h1>chiliarch AI</h1>
        </div>
        <div className="header-actions">
          <label className="mode-label">
            运行模式
            <select
              aria-label="运行模式"
              value={mode}
              disabled={busy}
              onChange={(event) => void changeMode(event.target.value)}
            >
              <option value="ask">ask · 变更询问</option>
              <option value="auto">auto · 风险询问</option>
              <option value="yolo">yolo · 自主运行</option>
            </select>
          </label>
          <button
            className="inspector-toggle"
            onClick={() => setInspectorOpen(!inspectorOpen)}
          >
            请求与审批 {pending.length + requests.length || ""}
          </button>
        </div>
      </header>
      <div className="chat-layout">
        <aside className="conversation-list" aria-label="会话">
          <button
            className="new-conversation"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void createConversation()
                .catch((error: unknown) =>
                  setError(
                    error instanceof Error ? error.message : String(error),
                  ),
                )
                .finally(() => setBusy(false));
            }}
          >
            ＋ 新会话
          </button>
          <h2>最近会话</h2>
          {conversations.length === 0 && (
            <p className="muted small">尚无会话</p>
          )}
          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              className={`conversation-entry ${selected === conversation.id ? "selected" : ""}`}
              onClick={() => setSelected(conversation.id)}
              aria-pressed={selected === conversation.id}
            >
              <span>{conversation.title || "新会话"}</span>
              <small>
                {conversation.source === "agent"
                  ? "来自 agent"
                  : time(conversation.updated_at)}
              </small>
            </button>
          ))}
        </aside>
        <section
          className="conversation-workspace"
          aria-label="与 chiliarch AI 对话"
        >
          <div className="conversation-topline">
            <span>
              {detail?.title || "新会话"}
              {detail?.agent_id &&
                ` · ${state.agents.find((agent) => agent.id === detail.agent_id)?.name ?? detail.agent_id}`}
            </span>
            {selected && (
              <span className="muted small">
                {connected ? "实时连接" : "正在重连"}
              </span>
            )}
            {detail && (
              <details className="conversation-menu">
                <summary aria-label="管理当前会话">···</summary>
                <div className="conversation-menu-panel">
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      void manageConversation(false);
                    }}
                  >
                    <label htmlFor="conversation-title">
                      会话名称
                      <input
                        id="conversation-title"
                        value={title}
                        onChange={(event) => setTitle(event.target.value)}
                        required
                        maxLength={256}
                      />
                    </label>
                    <button disabled={busy || !title.trim()}>保存名称</button>
                  </form>
                  <button
                    className="danger"
                    disabled={busy || Boolean(activeRun)}
                    onClick={() => void manageConversation(true)}
                  >
                    删除当前会话
                  </button>
                  {activeRun && (
                    <p className="small muted">先停止运行，再删除会话。</p>
                  )}
                </div>
              </details>
            )}
          </div>
          <div
            className="message-scroll"
            onScroll={(event) => {
              const target = event.currentTarget;
              scrollPinned.current =
                target.scrollHeight - target.scrollTop - target.clientHeight <
                80;
            }}
          >
            <ErrorNotice error={error} onDismiss={() => setError(null)} />
            <ErrorNotice error={streamError} />
            <ErrorNotice error={failedRun?.error ?? null} />
            {chat.messages.length === 0 ? (
              <EmptyState
                title={configured ? "今天需要调度什么？" : "先连接你的模型"}
              >
                {configured ? (
                  "查看任务进度、派发指令，或回应 agent 发来的请求。"
                ) : (
                  <>
                    在配置中设置总管模型，然后开始第一段会话。
                    <button className="text-link" onClick={onConfigure}>
                      打开模型配置 ↗
                    </button>
                  </>
                )}
              </EmptyState>
            ) : (
              chat.messages.map((message) => (
                <article
                  key={message.id}
                  className={`chat-message message-${message.role}`}
                >
                  <div className="message-heading">
                    <strong>
                      {message.role === "assistant"
                        ? "chiliarch AI"
                        : message.role === "agent"
                          ? "agent 来信"
                          : "你"}
                    </strong>
                    <time>{time(message.created_at)}</time>
                  </div>
                  {message.role === "agent" && message.source && (
                    <p className="muted small">来源：{message.source}</p>
                  )}
                  {message.reasoning && (
                    <details className="reasoning">
                      <summary>思考过程</summary>
                      <MarkdownContent content={message.reasoning} />
                    </details>
                  )}
                  <MarkdownContent content={message.content} />
                  {message.run_id &&
                    chat.tools
                      .filter((tool) => tool.run_id === message.run_id)
                      .map((tool) => <ToolView key={tool.id} tool={tool} />)}
                  {message.role === "assistant" &&
                    activeRun &&
                    activeRun.id === message.run_id && (
                      <div className="thinking-indicator">
                        <span />
                        <span />
                        <span />
                        <Status value={activeRun.status} />
                      </div>
                    )}
                </article>
              ))
            )}
            <div ref={bottom} />
          </div>
          <form className="composer" onSubmit={(event) => void send(event)}>
            <label className="sr-only" htmlFor="chat-input">
              发送消息
            </label>
            <textarea
              id="chat-input"
              value={content}
              onChange={(event) => setContent(event.target.value)}
              placeholder={
                configured ? "向 chiliarch AI 发送消息…" : "请先配置总管模型"
              }
              disabled={!configured}
              rows={3}
              onKeyDown={(event) => {
                if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                  event.preventDefault();
                  void send(event);
                }
              }}
            />
            <div className="composer-footer">
              <small>
                {activeRun ? "运行在后台持续进行" : "Ctrl / ⌘ + Enter 发送"}
              </small>
              {activeRun ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void cancel()}
                >
                  停止运行
                </button>
              ) : (
                <button
                  className="primary"
                  type="submit"
                  disabled={
                    !configured ||
                    busy ||
                    !content.trim() ||
                    Boolean(selected && !detail)
                  }
                >
                  {busy ? "发送中…" : "发送 ↑"}
                </button>
              )}
            </div>
          </form>
        </section>
        <aside
          className={`context-inspector ${inspectorOpen ? "inspector-visible" : ""}`}
          aria-label="请求与审批"
        >
          <div className="row-between">
            <h2>请求与审批</h2>
            <button
              className="plain inspector-close"
              onClick={() => setInspectorOpen(false)}
              aria-label="关闭请求与审批"
            >
              ×
            </button>
          </div>
          <p className="muted small">{pending.length} 个操作等待许可</p>
          {pending.map((approval) => (
            <ApprovalItem
              key={approval.id}
              approval={approval}
              onResolved={refresh}
            />
          ))}
          {pending.length === 0 && (
            <p className="empty-line">当前没有待审批操作</p>
          )}
          <h3>agent 来信</h3>
          {requests.length === 0 && (
            <p className="empty-line">当前没有待处理请求</p>
          )}
          {requests.map((request) => (
            <div className="request-entry" key={request.id}>
              <div className="row-between">
                <strong>
                  {state.agents.find((agent) => agent.id === request.agent_id)
                    ?.name ?? request.agent_id}
                </strong>
                <Status value={request.status} />
              </div>
              <p>{request.message || request.operation}</p>
              <details>
                <summary>查看请求</summary>
                <JsonView value={request.arguments} />
              </details>
            </div>
          ))}
          <h3>最近任务</h3>
          {state.tasks.length === 0 ? (
            <p className="empty-line">尚无任务</p>
          ) : (
            state.tasks.slice(0, 5).map((task) => (
              <div className="task-preview" key={task.id}>
                <p>{task.instruction}</p>
                <Status value={task.status} />
              </div>
            ))
          )}
        </aside>
      </div>
    </div>
  );
}
