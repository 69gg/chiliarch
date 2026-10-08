import type { ReactNode } from "react";
import type { Agent } from "./types";

export function executionLabel(agent: Agent): string {
  if (agent.revoked) return "凭证已吊销";
  if (!agent.online) return "离线，等待连接";
  const metadata = agent.metadata;
  if (metadata.state === "blocked") return "执行受阻";
  if (metadata.state === "busy") return "正在执行";
  if (
    metadata.execution_mode === "manual" ||
    metadata.state === "waiting_for_claim"
  )
    return "等待会话领取";
  if (metadata.execution_mode === "managed" && metadata.state === "idle")
    return "可自动执行";
  return "执行能力未上报";
}

export function AgentExecution({ agent }: { agent: Agent }): ReactNode {
  const metadata = agent.metadata;
  const active = Array.isArray(metadata.active_task_ids)
    ? metadata.active_task_ids.filter(
        (id): id is string => typeof id === "string",
      )
    : [];
  return (
    <div className="agent-execution" aria-label="执行状态">
      <strong>{executionLabel(agent)}</strong>
      <dl className="agent-connection-facts">
        {typeof metadata.execution_mode === "string" && (
          <div>
            <dt>执行方式</dt>
            <dd>
              {metadata.execution_mode === "managed"
                ? "自动执行"
                : metadata.execution_mode === "manual"
                  ? "会话主动领取"
                  : metadata.execution_mode}
            </dd>
          </div>
        )}
        {typeof metadata.adapter === "string" && (
          <div>
            <dt>适配器</dt>
            <dd>{metadata.adapter}</dd>
          </div>
        )}
        {typeof metadata.workspace === "string" && (
          <div>
            <dt>工作目录</dt>
            <dd className="mono">{metadata.workspace}</dd>
          </div>
        )}
        {typeof metadata.capacity === "number" && (
          <div>
            <dt>执行容量</dt>
            <dd>{metadata.capacity}</dd>
          </div>
        )}
        {typeof metadata.queued_tasks === "number" && (
          <div>
            <dt>本机排队</dt>
            <dd>{metadata.queued_tasks}</dd>
          </div>
        )}
        {active.length > 0 && (
          <div>
            <dt>正在执行</dt>
            <dd className="mono">{active.join("、")}</dd>
          </div>
        )}
        {typeof metadata.blocked_reason === "string" &&
          metadata.blocked_reason && (
            <div>
              <dt>受阻原因</dt>
              <dd>{metadata.blocked_reason}</dd>
            </div>
          )}
      </dl>
    </div>
  );
}
