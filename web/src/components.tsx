import { Children, isValidElement, useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Approval, Json, Timestamp, ToolCall } from "./types";
import { api } from "./api";

export function ErrorNotice({
  error,
  onDismiss,
}: {
  error: string | null;
  onDismiss?: () => void;
}): ReactNode {
  if (!error) return null;
  return (
    <div className="notice error" role="alert">
      <span>{error}</span>
      {onDismiss && (
        <button className="plain" onClick={onDismiss} aria-label="关闭错误提示">
          ×
        </button>
      )}
    </div>
  );
}

export function Status({ value }: { value: string }): ReactNode {
  const labels: Record<string, string> = {
    online: "在线",
    offline: "离线",
    revoked: "已吊销",
    pending: "待处理",
    awaiting_approval: "等待许可",
    running: "运行中",
    completed: "已完成",
    succeeded: "已完成",
    cancelled: "已取消",
    canceled: "已取消",
    queued: "队列中",
    delivered: "已投递",
    accepted: "已接收",
    failed: "失败",
    approved: "已允许",
    rejected: "已拒绝",
    expired: "已过期",
    cancel_requested: "请求取消",
    cancellation_requested: "请求取消",
    unknown: "状态未知",
    interrupted: "已中断",
  };
  return (
    <span className={`status status-${value}`}>
      <span className="status-dot" />
      {labels[value] ?? value}
    </span>
  );
}

export function time(value: Timestamp | null | undefined): string {
  if (value === null || value === undefined) return "尚无记录";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString("zh-CN", { hour12: false });
}

export function JsonView({ value }: { value: Json | undefined }): ReactNode {
  return (
    <pre className="json-view">
      <code>{JSON.stringify(value ?? null, null, 2)}</code>
    </pre>
  );
}

function CodeBlock({ children, ...props }: ComponentProps<"pre">): ReactNode {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const child = Children.toArray(children)[0];
  const code = isValidElement<{ children?: ReactNode; className?: string }>(
    child,
  )
    ? String(child.props.children ?? "").replace(/\n$/, "")
    : "";
  const language = isValidElement<{ className?: string }>(child)
    ? child.props.className?.replace("language-", "")
    : undefined;
  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setError(null);
    } catch {
      setError("无法访问剪贴板，请手动选择代码。");
    }
  }
  return (
    <div className="code-block">
      <div className="code-bar">
        <span>{language || "代码"}</span>
        <button className="plain" onClick={() => void copy()}>
          {copied ? "已复制" : "复制代码"}
        </button>
      </div>
      <pre {...props}>{children}</pre>
      {error && <small role="status">{error}</small>}
    </div>
  );
}

export function MarkdownContent({ content }: { content: string }): ReactNode {
  return (
    <div className="markdown">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          pre: ({ node: _node, ...props }) => <CodeBlock {...props} />,
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer" />
          ),
        }}
      >
        {content}
      </Markdown>
    </div>
  );
}

export function ToolView({
  tool,
  approval,
  onResolved,
}: {
  tool: ToolCall;
  approval?: Approval;
  onResolved?: () => void;
}): ReactNode {
  const pendingApproval =
    tool.status === "approval" &&
    approval &&
    ["pending", "awaiting_approval"].includes(approval.status);
  return (
    <div
      className="tool-block"
      role="group"
      aria-label={`工具调用 ${tool.name}`}
    >
      <details className="tool-call">
        <summary>
          <span className="tool-symbol">↗</span>
          <strong>{tool.name}</strong>
          <Status
            value={
              tool.status === "approval" ? "awaiting_approval" : tool.status
            }
          />
        </summary>
        <div className="tool-details">
          <h4>参数</h4>
          <JsonView value={tool.arguments} />
          {tool.result !== undefined && (
            <>
              <h4>结果</h4>
              <JsonView value={tool.result} />
            </>
          )}
        </div>
      </details>
      {pendingApproval && onResolved && (
        <ApprovalItem approval={approval} onResolved={onResolved} compact />
      )}
    </div>
  );
}

export function ApprovalItem({
  approval,
  onResolved,
  compact = false,
}: {
  approval: Approval;
  onResolved: () => void;
  compact?: boolean;
}): ReactNode {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = ["pending", "awaiting_approval"].includes(approval.status);
  async function decide(allow: boolean): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await api.operate(allow ? "approve_operation" : "reject_operation", {
        approval_id: approval.id,
      });
      onResolved();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className={`approval-item${compact ? " tool-approval" : ""}`}
      aria-label={`审批 ${approval.operation}`}
    >
      {!compact && (
        <div className="row-between">
          <strong>{approval.operation}</strong>
          <Status value={approval.status} />
        </div>
      )}
      <p className="muted">
        {approval.reason || "此操作需要你的许可。"}
        {!compact && ` · ${approval.requested_by}`}
      </p>
      {!compact && <JsonView value={approval.arguments} />}
      <ErrorNotice error={error} />
      {pending && (
        <div className="actions">
          <button
            className="primary"
            disabled={busy}
            onClick={() => void decide(true)}
          >
            允许这次操作
          </button>
          <button disabled={busy} onClick={() => void decide(false)}>
            拒绝
          </button>
        </div>
      )}
    </section>
  );
}

export function EmptyState({
  title,
  children,
  compact = false,
}: {
  title: string;
  children?: ReactNode;
  compact?: boolean;
}): ReactNode {
  return (
    <div className={`empty-state ${compact ? "compact" : ""}`}>
      <span className="empty-mark" aria-hidden="true">
        ∴
      </span>
      <h2>{title}</h2>
      {children && <p>{children}</p>}
    </div>
  );
}
