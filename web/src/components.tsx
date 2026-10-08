import { Children, isValidElement, useEffect, useId, useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import Markdown from "react-markdown";
import type { Components } from "react-markdown";
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
    active: "进行中",
    blocked: "受阻",
    changes_requested: "需要返工",
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
  const [copiedCode, setCopiedCode] = useState<string | null>(null);
  const [copying, setCopying] = useState(false);
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
  const copied = copiedCode === code;
  useEffect(() => {
    if (copiedCode === null || copying) return;
    const timer = window.setTimeout(() => setCopiedCode(null), 2000);
    return () => window.clearTimeout(timer);
  }, [copiedCode, copying]);
  useEffect(() => {
    setError(null);
  }, [code]);
  async function copy(): Promise<void> {
    if (copying) return;
    setCopying(true);
    setError(null);
    try {
      await navigator.clipboard.writeText(code);
      setCopiedCode(code);
    } catch {
      setCopiedCode(null);
      setError("无法访问剪贴板，请手动选择代码。");
    } finally {
      setCopying(false);
    }
  }
  return (
    <div className="code-block">
      <div className="code-bar">
        <span>{language || "代码"}</span>
        <button
          className={`plain code-copy${copied ? " is-copied" : ""}`}
          type="button"
          disabled={copying}
          aria-busy={copying}
          onClick={() => void copy()}
        >
          <svg
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            aria-hidden="true"
          >
            {copied ? (
              <path d="m4 10 4 4 8-8" />
            ) : (
              <>
                <rect x="6" y="6" width="10" height="11" rx="2" />
                <path d="M12 6V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
              </>
            )}
          </svg>
          {copying ? "复制中…" : copied ? "已复制" : "复制代码"}
        </button>
      </div>
      <pre {...props}>{children}</pre>
      <span
        className={`code-copy-feedback${error ? " copy-error" : ""}`}
        role="status"
        aria-live="polite"
      >
        {error || (copied ? "代码已复制" : "")}
      </span>
    </div>
  );
}

const markdownComponents: Components = {
  pre: ({ node: _node, ...props }) => <CodeBlock {...props} />,
  a: ({ node: _node, ...props }) => (
    <a {...props} target="_blank" rel="noreferrer" />
  ),
};

export function MarkdownContent({ content }: { content: string }): ReactNode {
  return (
    <div className="markdown">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={markdownComponents}
      >
        {content}
      </Markdown>
    </div>
  );
}

export function AnimatedDisclosure({
  title,
  children,
  status,
  icon,
  className = "",
  defaultOpen = false,
}: {
  title: ReactNode;
  children: ReactNode;
  status?: ReactNode;
  icon?: ReactNode;
  className?: string;
  defaultOpen?: boolean;
}): ReactNode {
  const [open, setOpen] = useState(defaultOpen);
  const contentId = useId();
  return (
    <details
      className={`animated-disclosure${icon ? " has-disclosure-icon" : ""} ${className}`}
      open={open}
    >
      <summary
        className="disclosure-summary"
        role="button"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={(event) => {
          event.preventDefault();
          setOpen((previous) => !previous);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setOpen((previous) => !previous);
          }
        }}
      >
        {icon && (
          <span className="disclosure-icon" aria-hidden="true">
            {icon}
          </span>
        )}
        <span className="disclosure-title">{title}</span>
        {status && <span className="disclosure-status">{status}</span>}
        <svg
          className="disclosure-chevron"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          aria-hidden="true"
        >
          <path d="m7 4 6 6-6 6" />
        </svg>
      </summary>
      <div id={contentId} className="disclosure-body" hidden={!open}>
        {children}
      </div>
    </details>
  );
}

const TOOL_LABELS: Record<string, string> = {
  get_state: "查看平台状态",
  get_agents: "查看 agents",
  get_tasks: "查看任务",
  get_config: "读取配置",
  get_logs: "读取日志",
  create_pairing_code: "生成配对码",
  rename_agent: "重命名 agent",
  revoke_agent: "吊销 agent",
  dispatch_task: "派发任务",
  cancel_task: "取消任务",
  send_agent_message: "联系 agent",
  resolve_agent_request: "处理 agent 请求",
  approve_operation: "允许操作",
  reject_operation: "拒绝操作",
  update_config: "更新配置",
  change_password: "修改密码",
  create_conversation: "创建会话",
  rename_conversation: "重命名会话",
  delete_conversation: "删除会话",
  send_chat_message: "发送消息",
  cancel_chat_run: "停止运行",
  list_goals: "查看目标",
  get_goal: "查看目标详情",
  create_goal: "创建目标",
  update_goal: "更新目标",
  review_task: "验收任务",
  retry_task: "重试任务",
};

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
    approval.id === tool.approval_id &&
    ["pending", "awaiting_approval"].includes(approval.status);
  const resultStatus =
    tool.result &&
    typeof tool.result === "object" &&
    !Array.isArray(tool.result)
      ? tool.result.status
      : null;
  const status =
    tool.status === "approval"
      ? "awaiting_approval"
      : typeof resultStatus === "string" &&
          ["error", "failed", "rejected"].includes(resultStatus)
        ? resultStatus
        : tool.status;
  const label = TOOL_LABELS[tool.name] || tool.name;
  return (
    <div
      className={`tool-block tool-${status}`}
      role="group"
      aria-label={`工具调用 ${tool.name}`}
    >
      <AnimatedDisclosure
        className="tool-call"
        title={
          <span className="tool-summary-name">
            <strong>{label}</strong>
            {label !== tool.name && <code>{tool.name}</code>}
          </span>
        }
        icon={
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor">
            <rect x="2" y="3" width="16" height="14" rx="3" />
            <path d="m6 7 3 3-3 3M11 13h3" />
          </svg>
        }
        status={<Status value={status === "error" ? "failed" : status} />}
      >
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
      </AnimatedDisclosure>
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
