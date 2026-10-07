import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import {
  AnimatedDisclosure,
  ErrorNotice,
  MarkdownContent,
  Status,
  time,
  ToolView,
} from "./components";
import { ChatComposer, ChatGlyph, MessageCopy } from "./ChatUI";
import type {
  Conversation,
  ConversationDetail,
  Json,
  PlatformState,
} from "./types";

import { applyChatEvent, messageParts, restoreChatState } from "./ChatTimeline";
import type { ChatState } from "./ChatTimeline";
export { applyChatEvent } from "./ChatTimeline";
export type { ChatState } from "./ChatTimeline";

const ACTIVE_RUN_STATUSES = [
  "running",
  "pending",
  "queued",
  "awaiting_approval",
];

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
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showJump, setShowJump] = useState(false);
  const [mobileSidebar, setMobileSidebar] = useState(
    () => window.matchMedia("(max-width: 700px)").matches,
  );
  const sidebar = useRef<HTMLDialogElement>(null);
  const sidebarButton = useRef<HTMLButtonElement>(null);
  const messageScroll = useRef<HTMLDivElement>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const conversationMenu = useRef<HTMLDetailsElement>(null);
  const drafts = useRef(new Map<string, string>());
  const contentRevision = useRef(0);
  const scrollPinned = useRef(true);
  const localMessageSequence = useRef(0);
  const model = state.config.model as Record<string, Json> | undefined;
  const configured = Boolean(model?.model);
  const supervisor = state.config.supervisor as
    Record<string, Json> | undefined;
  const mode = String(supervisor?.mode ?? "ask");
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
    const query = window.matchMedia("(max-width: 700px)");
    const resize = (): void => setMobileSidebar(query.matches);
    query.addEventListener("change", resize);
    return () => query.removeEventListener("change", resize);
  }, []);
  useEffect(() => {
    if (!sidebarOpen || !sidebar.current) return;
    const dialog = sidebar.current;
    if (mobileSidebar) dialog.showModal();
    else dialog.show();
    const escape = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        event.preventDefault();
        setSidebarOpen(false);
      }
    };
    document.addEventListener("keydown", escape);
    const outside = (event: PointerEvent): void => {
      if (
        !mobileSidebar &&
        event.target instanceof Node &&
        !dialog.contains(event.target) &&
        !sidebarButton.current?.contains(event.target)
      )
        setSidebarOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => {
      document.removeEventListener("keydown", escape);
      document.removeEventListener("pointerdown", outside);
      dialog.close();
      sidebarButton.current?.focus();
    };
  }, [sidebarOpen, mobileSidebar]);
  useEffect(() => {
    const closeMenu = (event: PointerEvent | KeyboardEvent): void => {
      const menu = conversationMenu.current;
      if (!menu?.open) return;
      if (event instanceof KeyboardEvent && event.key === "Escape") {
        menu.open = false;
        menu.querySelector<HTMLElement>("summary")?.focus();
      } else if (
        event.type === "pointerdown" &&
        event.target instanceof Node &&
        !menu.contains(event.target)
      ) {
        menu.open = false;
      }
    };
    document.addEventListener("pointerdown", closeMenu);
    document.addEventListener("keydown", closeMenu);
    return () => {
      document.removeEventListener("pointerdown", closeMenu);
      document.removeEventListener("keydown", closeMenu);
    };
  }, []);
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
        setChat(restoreChatState(result));
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
    const container = messageScroll.current;
    if (scrollPinned.current && container)
      container.scrollTo({ top: container.scrollHeight, behavior: "instant" });
  }, [chat.messages, chat.tools]);

  function focusComposer(): void {
    requestAnimationFrame(() => composerInput.current?.focus());
  }
  function editContent(next: string): void {
    contentRevision.current += 1;
    setContent(next);
  }
  function chooseConversation(id: string | null): void {
    drafts.current.set(selected ?? "new", content);
    setContent(drafts.current.get(id ?? "new") ?? "");
    setSelected(id);
    setSidebarOpen(false);
    scrollPinned.current = true;
    setShowJump(false);
  }
  function jumpToLatest(): void {
    scrollPinned.current = true;
    setShowJump(false);
    const container = messageScroll.current;
    if (container)
      container.scrollTo({
        top: container.scrollHeight,
        behavior: "instant",
      });
  }

  async function createConversation(): Promise<string> {
    const conversation = await api.createConversation();
    setConversations((previous) => [conversation, ...previous]);
    chooseConversation(conversation.id);
    focusComposer();
    return conversation.id;
  }
  async function send(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!content.trim() || busy || activeRun || (selected && !detail)) return;
    scrollPinned.current = true;
    setShowJump(false);
    setBusy(true);
    setError(null);
    const text = content.trim();
    const submittedRevision = contentRevision.current;
    const localMessageId = selected
      ? `local-user:${selected}:${++localMessageSequence.current}`
      : null;
    if (localMessageId) {
      setChat((previous) => ({
        ...previous,
        messages: [
          ...previous.messages,
          {
            id: localMessageId,
            role: "user",
            content: text,
            reasoning: "",
            created_at: Date.now() / 1000,
          },
        ],
      }));
    }
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
        setChat((previous) => {
          if (
            !previous.messages.some((message) => message.id === localMessageId)
          )
            return previous;
          return {
            ...previous,
            messages: previous.messages.map((message) =>
              message.id === localMessageId
                ? { ...message, run_id: result.run_id }
                : message,
            ),
            runs: previous.runs.some((run) => run.id === result.run_id)
              ? previous.runs
              : [
                  ...previous.runs,
                  {
                    id: result.run_id,
                    conversation_id: id,
                    status: "running",
                    created_at: Date.now() / 1000,
                    updated_at: Date.now() / 1000,
                  },
                ],
          };
        });
      if (contentRevision.current === submittedRevision) setContent("");
      drafts.current.delete(selected ?? "new");
      focusComposer();
      void loadConversations();
    } catch (error) {
      if (localMessageId) {
        setChat((previous) => ({
          ...previous,
          messages: previous.messages.filter(
            (message) => message.id !== localMessageId,
          ),
        }));
      }
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
      <header className="page-header chat-header">
        <div className="chat-title">
          <button
            ref={sidebarButton}
            className="chat-sidebar-toggle chat-icon-button"
            type="button"
            title="会话列表"
            aria-label={sidebarOpen ? "收起会话列表" : "展开会话列表"}
            aria-expanded={sidebarOpen}
            aria-controls="chat-conversations"
            onClick={() => setSidebarOpen((open) => !open)}
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              aria-hidden="true"
            >
              <rect x="3" y="4" width="18" height="16" rx="3" />
              <path d="M9 4v16" />
            </svg>
          </button>
          <div className="chat-heading">
            <h1>Chiliarch AI</h1>
            {detail && (
              <span className="chat-conversation-title" title={detail.title}>
                {detail.title || "新会话"}
              </span>
            )}
          </div>
          {selected && (
            <span
              className={`chat-connection${connected ? " is-connected" : ""}`}
              role="status"
              aria-label={connected ? "对话已连接" : "对话正在重连"}
              title={connected ? "对话已连接" : "正在重连"}
            />
          )}
        </div>
        <div className="header-actions chat-header-actions">
          <button
            className="chat-new-button"
            type="button"
            aria-label="新建会话"
            title="新建会话"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void createConversation()
                .catch((failure: unknown) =>
                  setError(
                    failure instanceof Error
                      ? failure.message
                      : String(failure),
                  ),
                )
                .finally(() => setBusy(false));
            }}
          >
            <ChatGlyph type="add" />
            <span>新会话</span>
          </button>
          <label className="mode-label">
            <span className="sr-only">运行模式</span>
            <select
              aria-label="运行模式"
              title="总管运行模式"
              value={mode}
              disabled={busy}
              onChange={(event) => void changeMode(event.target.value)}
            >
              <option value="ask">ask · 变更询问</option>
              <option value="auto">auto · 风险询问</option>
              <option value="yolo">yolo · 自主运行</option>
            </select>
          </label>
          {detail && (
            <details ref={conversationMenu} className="conversation-menu">
              <summary aria-label="管理当前会话" title="管理会话">
                <ChatGlyph type="more" />
              </summary>
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
      </header>
      <div
        className={`chat-layout${sidebarOpen && !mobileSidebar ? " sidebar-open" : ""}`}
      >
        <dialog
          ref={sidebar}
          id="chat-conversations"
          className={`conversation-drawer${mobileSidebar ? " mobile-drawer" : ""}`}
          aria-label="会话列表"
          aria-modal={mobileSidebar ? true : undefined}
          onCancel={(event) => {
            event.preventDefault();
            setSidebarOpen(false);
          }}
          onClick={(event) => {
            if (!mobileSidebar || event.target !== event.currentTarget) return;
            const bounds = event.currentTarget.getBoundingClientRect();
            if (
              event.clientX < bounds.left ||
              event.clientX > bounds.right ||
              event.clientY < bounds.top ||
              event.clientY > bounds.bottom
            )
              setSidebarOpen(false);
          }}
        >
          <div className="conversation-list">
            <div className="conversation-list-heading">
              <h2>最近会话</h2>
              <button
                className="plain"
                type="button"
                aria-label="关闭会话列表"
                onClick={() => setSidebarOpen(false)}
                autoFocus
              >
                ×
              </button>
            </div>
            <button
              className="new-conversation"
              disabled={busy}
              onClick={() => {
                setBusy(true);
                void createConversation()
                  .catch((failure: unknown) =>
                    setError(
                      failure instanceof Error
                        ? failure.message
                        : String(failure),
                    ),
                  )
                  .finally(() => setBusy(false));
              }}
            >
              ＋ 新会话
            </button>
            {conversations.length === 0 && (
              <p className="muted small">尚无会话</p>
            )}
            {conversations.map((conversation) => (
              <button
                key={conversation.id}
                disabled={busy}
                className={`conversation-entry${selected === conversation.id ? " selected" : ""}`}
                onClick={() => chooseConversation(conversation.id)}
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
          </div>
        </dialog>
        <section
          className="conversation-workspace"
          aria-label="与 Chiliarch AI 对话"
        >
          <div
            ref={messageScroll}
            className="message-scroll"
            onScroll={(event) => {
              const target = event.currentTarget;
              scrollPinned.current =
                target.scrollHeight - target.scrollTop - target.clientHeight <
                80;
              setShowJump(!scrollPinned.current);
            }}
          >
            <div
              className={`message-list${chat.messages.length === 0 ? " is-empty" : ""}`}
              key={selected ?? "new"}
            >
              <ErrorNotice error={error} onDismiss={() => setError(null)} />
              <ErrorNotice error={streamError} />
              <ErrorNotice error={failedRun?.error ?? null} />
              {selected && !detail ? (
                <div className="chat-loading" role="status">
                  <span className="chat-loading-dot" />
                  正在读取会话…
                </div>
              ) : chat.messages.length === 0 ? (
                <div className="chat-intro">
                  <span className="chat-intro-mark" aria-hidden="true">
                    <ChatGlyph type="spark" />
                  </span>
                  <h2>{configured ? "开始一段对话" : "连接你的模型"}</h2>
                  <p>
                    {configured
                      ? "查看进度、安排任务，或一起处理 agent 的请求。"
                      : "在配置中连接模型，让 Chiliarch AI 开始协作。"}
                  </p>
                  {configured ? (
                    <div className="chat-suggestions">
                      {[
                        "查看已连接的 agents",
                        "有哪些任务需要关注？",
                        "检查平台配置",
                      ].map((prompt) => (
                        <button
                          key={prompt}
                          type="button"
                          onClick={() => {
                            editContent(prompt);
                            focusComposer();
                          }}
                        >
                          {prompt}
                          <span aria-hidden="true">↗</span>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <button
                      className="chat-configure-button"
                      type="button"
                      onClick={onConfigure}
                    >
                      打开模型配置 <span aria-hidden="true">↗</span>
                    </button>
                  )}
                </div>
              ) : (
                chat.messages.map((message) => {
                  const running =
                    message.role === "assistant" &&
                    activeRun &&
                    activeRun.id === message.run_id
                      ? activeRun
                      : undefined;
                  return (
                    <article
                      key={message.id}
                      className={`chat-message message-${message.role}${running?.status === "running" ? " is-streaming" : ""}`}
                    >
                      <div className="message-heading">
                        {message.role !== "user" && (
                          <span
                            className={`message-avatar${message.role === "agent" ? " is-agent" : ""}`}
                            aria-hidden="true"
                          >
                            <ChatGlyph
                              type={
                                message.role === "agent" ? "arrow" : "spark"
                              }
                            />
                          </span>
                        )}
                        <strong>
                          {message.role === "assistant"
                            ? "Chiliarch AI"
                            : message.role === "agent"
                              ? "agent 来信"
                              : "你"}
                        </strong>
                        <time>{time(message.created_at)}</time>
                      </div>
                      <div className="message-body">
                        {message.role === "agent" && message.source && (
                          <p className="message-source">
                            来源：{message.source}
                          </p>
                        )}
                        {message.role === "assistant" && message.run_id ? (
                          messageParts(message, chat).map((part) =>
                            part.kind === "tool" ? (
                              <ToolView
                                key={part.id}
                                tool={part.tool}
                                approval={state.approvals.find(
                                  (approval) =>
                                    approval.id === part.tool.approval_id,
                                )}
                                onResolved={refresh}
                              />
                            ) : part.kind === "reasoning" ? (
                              <AnimatedDisclosure
                                key={part.id}
                                title="思考过程"
                                className="reasoning"
                                icon={<ChatGlyph type="thought" />}
                              >
                                <MarkdownContent content={part.content} />
                              </AnimatedDisclosure>
                            ) : (
                              <MarkdownContent
                                key={part.id}
                                content={part.content}
                              />
                            ),
                          )
                        ) : (
                          <>
                            {message.reasoning && (
                              <AnimatedDisclosure
                                title="思考过程"
                                className="reasoning"
                                icon={<ChatGlyph type="thought" />}
                              >
                                <MarkdownContent content={message.reasoning} />
                              </AnimatedDisclosure>
                            )}
                            {message.content && (
                              <MarkdownContent content={message.content} />
                            )}
                          </>
                        )}
                        {running && (
                          <div
                            className={`thinking-indicator${running.status === "awaiting_approval" ? " is-waiting" : ""}`}
                            role="status"
                          >
                            {running.status !== "awaiting_approval" && (
                              <span
                                className="thinking-orbit"
                                aria-hidden="true"
                              />
                            )}
                            <Status value={running.status} />
                          </div>
                        )}
                        {message.role === "assistant" &&
                          message.content &&
                          !running && <MessageCopy content={message.content} />}
                      </div>
                    </article>
                  );
                })
              )}
            </div>
          </div>
          <ChatComposer
            inputRef={composerInput}
            content={content}
            onChange={editContent}
            onSubmit={(event) => void send(event)}
            onCancel={() => void cancel()}
            configured={configured}
            busy={busy}
            waitingForConversation={Boolean(selected && !detail)}
            active={Boolean(activeRun)}
            model={typeof model?.model === "string" ? model.model : ""}
            accessory={
              showJump && (
                <button
                  className="jump-to-latest"
                  type="button"
                  onClick={jumpToLatest}
                  aria-label="回到最新消息"
                >
                  <ChatGlyph type="arrow" />
                  <span>回到最新</span>
                </button>
              )
            }
          />
        </section>
      </div>
    </div>
  );
}
