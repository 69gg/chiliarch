import { useEffect, useLayoutEffect, useState } from "react";
import type { FormEvent, ReactNode, RefObject } from "react";

export function ChatGlyph({
  type,
  className,
}: {
  type:
    "spark" | "add" | "arrow" | "stop" | "copy" | "check" | "thought" | "more";
  className?: string;
}): ReactNode {
  const paths = {
    more: (
      <>
        <circle cx="5" cy="12" r="1" fill="currentColor" />
        <circle cx="12" cy="12" r="1" fill="currentColor" />
        <circle cx="19" cy="12" r="1" fill="currentColor" />
      </>
    ),
    spark: (
      <path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z" />
    ),
    add: <path d="M12 5v14M5 12h14" />,
    arrow: <path d="M12 19V5m-6 6 6-6 6 6" />,
    stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
    copy: (
      <>
        <rect x="8" y="8" width="12" height="12" rx="2" />
        <path d="M16 8V4H4v12h4" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    thought: (
      <>
        <path d="M9 18h6m-5 3h4M8 14a6 6 0 1 1 8 0c-1 1-1 2-1 3H9c0-1 0-2-1-3Z" />
      </>
    ),
  };
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[type]}
    </svg>
  );
}

export function ChatComposer({
  inputRef,
  content,
  onChange,
  onSubmit,
  onCancel,
  configured,
  busy,
  waitingForConversation,
  active,
  model,
  accessory,
}: {
  inputRef: RefObject<HTMLTextAreaElement | null>;
  content: string;
  onChange: (content: string) => void;
  onSubmit: (event: FormEvent) => void;
  onCancel: () => void;
  configured: boolean;
  busy: boolean;
  waitingForConversation: boolean;
  active: boolean;
  model: string;
  accessory?: ReactNode;
}): ReactNode {
  useLayoutEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }, [content, inputRef]);
  return (
    <div className="composer-dock">
      {accessory}
      <form
        className={`composer${active ? " is-running" : ""}`}
        onSubmit={onSubmit}
      >
        <label className="sr-only" htmlFor="chat-input">
          发送消息
        </label>
        <textarea
          ref={inputRef}
          id="chat-input"
          value={content}
          onChange={(event) => onChange(event.target.value)}
          placeholder={
            configured ? "向 Chiliarch AI 发送消息…" : "请先配置总管模型"
          }
          disabled={!configured}
          rows={1}
          enterKeyHint="send"
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing &&
              event.nativeEvent.keyCode !== 229
            ) {
              event.preventDefault();
              if (
                !active &&
                !busy &&
                configured &&
                content.trim() &&
                !waitingForConversation
              )
                event.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <div className="composer-footer">
          <span className="composer-model" title={model || "尚未配置模型"}>
            <ChatGlyph type="spark" />
            <span>{model || "未配置模型"}</span>
          </span>
          {active ? (
            <button
              type="button"
              className="composer-send is-stop"
              aria-label="停止运行"
              title="停止运行"
              disabled={busy}
              onClick={onCancel}
            >
              <ChatGlyph type="stop" />
            </button>
          ) : (
            <button
              className="primary composer-send"
              type="submit"
              aria-label={busy ? "发送中…" : "发送 ↑"}
              title="发送消息"
              disabled={
                !configured || busy || !content.trim() || waitingForConversation
              }
            >
              {busy ? (
                <span className="composer-submit-spinner" aria-hidden="true" />
              ) : (
                <ChatGlyph type="arrow" />
              )}
            </button>
          )}
        </div>
      </form>
      <p className="composer-hint">
        {active
          ? "可继续起草，当前运行完成后发送"
          : busy
            ? "正在发送…"
            : "Enter 发送 · Shift + Enter 换行"}
      </p>
    </div>
  );
}

export function MessageCopy({ content }: { content: string }): ReactNode {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1800);
    return () => clearTimeout(timer);
  }, [copied]);
  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setError(false);
    } catch {
      setCopied(false);
      setError(true);
    }
  }
  return (
    <div className="message-actions">
      <button
        className="message-copy"
        type="button"
        aria-label={copied ? "回复已复制" : "复制回复"}
        title={copied ? "已复制" : "复制回复"}
        onClick={() => void copy()}
      >
        <ChatGlyph type={copied ? "check" : "copy"} />
      </button>
      {error && <span role="status">无法访问剪贴板，请手动复制。</span>}
    </div>
  );
}
