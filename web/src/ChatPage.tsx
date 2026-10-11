import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, ApiError } from "./api";
import {
  ErrorNotice,
  MarkdownContent,
  Status,
  time,
  ToolView,
} from "./components";
import { ChatComposer, ChatGlyph, MessageCopy, ReasoningView } from "./ChatUI";
import { ConversationList } from "./ConversationList";
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
const LAST_CONVERSATION_KEY = "chiliarch-last-conversation";

function conversationFromHash(): string | null {
  const match = location.hash.match(/^#ai\/([^/]+)$/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function initialConversation(): string | null {
  return conversationFromHash() ?? localStorage.getItem(LAST_CONVERSATION_KEY);
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
  const [selected, setSelected] = useState<string | null>(initialConversation);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [chat, setChat] = useState<ChatState>({
    messages: [],
    runs: [],
    tools: [],
  });
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [sidebarMode, setSidebarMode] = useState<"hover" | "manual" | null>(
    null,
  );
  const sidebarOpen = sidebarMode !== null;
  const [showJump, setShowJump] = useState(false);
  const [mobileSidebar, setMobileSidebar] = useState(
    () => window.matchMedia("(max-width: 700px)").matches,
  );
  const sidebar = useRef<HTMLDialogElement>(null);
  const sidebarButton = useRef<HTMLButtonElement>(null);
  const messageScroll = useRef<HTMLDivElement>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const drafts = useRef(new Map<string, string>());
  const contentRevision = useRef(0);
  const scrollPinned = useRef(true);
  const localMessageSequence = useRef(0);
  const conversationRevision = useRef(0);
  const restoringConversation = useRef(
    selected === localStorage.getItem(LAST_CONVERSATION_KEY) ? selected : null,
  );
  const model = state.config.model as Record<string, Json> | undefined;
  const configured = Boolean(model?.model);
  const supervisor = state.config.supervisor as
    Record<string, Json> | undefined;
  const mode = String(supervisor?.mode ?? "auto");
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
    const revision = ++conversationRevision.current;
    try {
      const result = await api.conversations();
      if (revision === conversationRevision.current) setConversations(result);
    } catch (error) {
      if (revision === conversationRevision.current)
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
    else if (sidebarMode === "manual")
      dialog.querySelector<HTMLElement>("[data-drawer-close]")?.focus();
    const close = (): void => setSidebarMode(null);
    const escape = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
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
        close();
    };
    document.addEventListener("pointerdown", outside);
    return () => {
      document.removeEventListener("keydown", escape);
      document.removeEventListener("pointerdown", outside);
      const focusInside = dialog.contains(document.activeElement);
      if (mobileSidebar) dialog.close();
      if (focusInside) sidebarButton.current?.focus();
    };
  }, [sidebarOpen, mobileSidebar]);
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setChat({ messages: [], runs: [], tools: [] });
      setError(null);
      setConnected(false);
      setStreamError(null);
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
        localStorage.setItem(LAST_CONVERSATION_KEY, selected);
        restoringConversation.current = null;
        if (!location.hash || location.hash === "#ai")
          history.replaceState(null, "", `#ai/${encodeURIComponent(selected)}`);
        setDetail(result);
        setChat(restoreChatState(result));
        stop = api.streamConversation(
          selected,
          result.last_event_seq ?? 0,
          (event) => {
            if (cancelled) return;
            if (
              event.type === "conversation_updated" &&
              event.data.conversation_id === selected &&
              typeof event.data.title === "string"
            ) {
              const updatedTitle = event.data.title;
              setDetail((previous) =>
                previous ? { ...previous, title: updatedTitle } : previous,
              );
              setConversations((previous) =>
                previous.map((conversation) =>
                  conversation.id === selected
                    ? { ...conversation, title: updatedTitle }
                    : conversation,
                ),
              );
              void loadConversations();
            }
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
        if (cancelled) return;
        if (
          restoringConversation.current === selected &&
          error instanceof ApiError &&
          error.status === 404
        ) {
          restoringConversation.current = null;
          if (localStorage.getItem(LAST_CONVERSATION_KEY) === selected)
            localStorage.removeItem(LAST_CONVERSATION_KEY);
          setSelected(null);
          if (
            !location.hash ||
            location.hash === "#ai" ||
            conversationFromHash() === selected
          )
            history.replaceState(null, "", "#ai");
        } else setError(error instanceof Error ? error.message : String(error));
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
    restoringConversation.current = null;
    if (!id) localStorage.removeItem(LAST_CONVERSATION_KEY);
    drafts.current.set(selected ?? "new", content);
    setContent(drafts.current.get(id ?? "new") ?? "");
    setSelected(id);
    setSidebarMode(null);
    scrollPinned.current = true;
    setShowJump(false);
    const nextHash = id ? `#ai/${encodeURIComponent(id)}` : "#ai";
    if (location.hash !== nextHash) history.replaceState(null, "", nextHash);
  }
  useEffect(() => {
    const changed = (): void => {
      if (location.hash === "#ai" || location.hash.startsWith("#ai/")) {
        const explicit = conversationFromHash();
        const next = explicit ?? localStorage.getItem(LAST_CONVERSATION_KEY);
        if (next !== selected) {
          chooseConversation(next);
          if (next === localStorage.getItem(LAST_CONVERSATION_KEY))
            restoringConversation.current = next;
        }
      }
    };
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  });
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

  async function manageConversation(
    id: string,
    nextTitle?: string,
  ): Promise<void> {
    setBusy(true);
    try {
      await api.operate(
        nextTitle === undefined ? "delete_conversation" : "rename_conversation",
        nextTitle === undefined
          ? { conversation_id: id }
          : { conversation_id: id, title: nextTitle },
      );
      conversationRevision.current += 1;
      if (nextTitle === undefined) {
        if (localStorage.getItem(LAST_CONVERSATION_KEY) === id)
          localStorage.removeItem(LAST_CONVERSATION_KEY);
        setConversations((previous) =>
          previous.filter((conversation) => conversation.id !== id),
        );
        if (selected === id) {
          chooseConversation(null);
          setDetail(null);
          setChat({ messages: [], runs: [], tools: [] });
        }
        drafts.current.delete(id);
      } else {
        setConversations((previous) =>
          previous.map((conversation) =>
            conversation.id === id
              ? { ...conversation, title: nextTitle }
              : conversation,
          ),
        );
        setDetail((previous) =>
          previous?.id === id ? { ...previous, title: nextTitle } : previous,
        );
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="chat-page">
      <header className="page-header chat-header">
        <div className="chat-title">
          <div className="chat-heading">
            <h1>Chiliarch AI</h1>
            {detail && (
              <span className="chat-conversation-title" title={detail.title}>
                {detail.title || "新会话"}
              </span>
            )}
          </div>
          {selected &&
            (state.goals ?? []).some(
              (goal) => goal.conversation_id === selected,
            ) && (
              <div className="chat-goal-links" aria-label="关联目标">
                {(state.goals ?? [])
                  .filter((goal) => goal.conversation_id === selected)
                  .map((goal) => (
                    <a
                      key={goal.id}
                      href={`#goals/${encodeURIComponent(goal.id)}`}
                    >
                      {goal.title} ↗
                    </a>
                  ))}
              </div>
            )}
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
          <button
            ref={sidebarButton}
            className="chat-sidebar-toggle chat-icon-button"
            type="button"
            title="会话列表"
            aria-label={sidebarOpen ? "收起会话列表" : "展开会话列表"}
            aria-expanded={sidebarOpen}
            aria-controls="chat-conversations"
            onClick={() =>
              setSidebarMode((mode) => (mode === "manual" ? null : "manual"))
            }
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              aria-hidden="true"
            >
              <rect x="3" y="4" width="18" height="16" rx="3" />
              <path d="M15 4v16" />
            </svg>
          </button>
        </div>
      </header>
      <div
        className={`chat-layout${sidebarOpen && !mobileSidebar ? " sidebar-open" : ""}`}
        onPointerMove={(event) => {
          if (
            mobileSidebar ||
            event.pointerType !== "mouse" ||
            sidebarMode === "manual"
          )
            return;
          if (
            event.target instanceof Node &&
            sidebar.current?.contains(event.target)
          )
            return;
          const bounds = event.currentTarget.getBoundingClientRect();
          if (event.clientX >= bounds.right - 12) setSidebarMode("hover");
          else if (sidebarMode === "hover" && sidebar.current) {
            const drawerBounds = sidebar.current.getBoundingClientRect();
            if (
              event.clientX >= drawerBounds.left &&
              event.clientY >= drawerBounds.top &&
              event.clientY <= drawerBounds.bottom
            )
              return;
            if (!sidebar.current.contains(document.activeElement))
              setSidebarMode(null);
          } else if (!sidebar.current?.contains(document.activeElement))
            setSidebarMode(null);
        }}
        onPointerLeave={() => {
          if (
            sidebarMode === "hover" &&
            !sidebar.current?.contains(document.activeElement)
          )
            setSidebarMode(null);
        }}
      >
        <dialog
          ref={sidebar}
          id="chat-conversations"
          className={`conversation-drawer${mobileSidebar ? " mobile-drawer" : ""}`}
          open={!mobileSidebar && sidebarOpen}
          aria-label="会话列表"
          aria-modal={mobileSidebar ? true : undefined}
          onBlur={(event) => {
            if (
              sidebarMode === "hover" &&
              !event.currentTarget.contains(event.relatedTarget)
            )
              setSidebarMode(null);
          }}
          onCancel={(event) => {
            event.preventDefault();
            setSidebarMode(null);
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
              setSidebarMode(null);
          }}
        >
          <ConversationList
            conversations={conversations}
            selected={selected}
            busy={busy}
            active={Boolean(activeRun)}
            onChoose={chooseConversation}
            onCreate={() => {
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
            onRename={(id, nextTitle) => manageConversation(id, nextTitle)}
            onDelete={(id) => manageConversation(id)}
            onClose={() => setSidebarMode(null)}
            onPin={() => setSidebarMode("manual")}
            pinned={sidebarMode === "manual" || mobileSidebar}
          />
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
                              ? message.source === "coordination"
                                ? "协调事件"
                                : "agent 来信"
                              : "你"}
                        </strong>
                        <time>{time(message.created_at)}</time>
                      </div>
                      <div className="message-body">
                        {message.role === "agent" &&
                          message.source &&
                          message.source !== "coordination" && (
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
                              <ReasoningView
                                key={part.id}
                                content={part.content}
                                streaming={running?.status === "running"}
                              />
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
                              <ReasoningView
                                content={message.reasoning}
                                streaming={running?.status === "running"}
                              />
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
                            {chat.context?.run_id === running.id &&
                            chat.context.compacting ? (
                              "正在压缩上下文…"
                            ) : (
                              <Status value={running.status} />
                            )}
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
            contextInfo={
              chat.context && (
                <span
                  className="composer-context"
                  title={`${chat.context.estimated ? "估算" : "服务商用量"}：${chat.context.tokens.toLocaleString()} / ${chat.context.contextWindow.toLocaleString()} tokens；90% 时自动压缩`}
                >
                  {chat.context.compacting
                    ? "压缩中…"
                    : `上下文 ${chat.context.estimated ? "约 " : ""}${((chat.context.tokens / chat.context.contextWindow) * 100).toFixed(1)}%`}
                </span>
              )
            }
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
