import type { ReactNode } from "react";
import type { Agent } from "./types";

const blockedReasons: Record<string, { label: string; guidance: string }> = {
  workspace_unavailable: {
    label: "工作目录不存在或无法访问",
    guidance:
      "在 agent 所在机器检查目录是否存在、是否有访问权限，并核对执行器的工作目录配置。",
  },
  workspace_busy: {
    label: "工作目录正由其他任务使用",
    guidance:
      "等待当前任务完成后会自动继续。需要并行工作时，为不同 agent 配置独立目录。",
  },
  workspace_unverified: {
    label: "上次执行的清理结果尚未确认",
    guidance:
      "在 agent 所在机器运行 chiliarch agent workspace-recover --workdir <工作目录> 查看记录。确认相关进程已停止并核对已有改动后，再添加 --confirm-stopped 解除隔离。",
  },
  handler_unavailable: {
    label: "尚未配置任务执行器",
    guidance:
      "检查本机 agent 配置，启动对应执行器；通过现有会话处理任务时使用会话领取模式。",
  },
};

export function blockedReasonLabel(reason: unknown): string {
  return typeof reason === "string" && reason
    ? (blockedReasons[reason]?.label ?? reason)
    : "执行器暂时无法开始任务";
}

function executionGuidance(agent: Agent): string {
  if (agent.revoked)
    return "这个身份不能再接收任务。重新配对后使用新的 agent 身份。";
  if (!agent.online)
    return "在 agent 所在机器检查守护进程和出站连接；平台会等待它重新连接。";
  if (agent.metadata.state === "blocked") {
    const reason = agent.metadata.blocked_reason;
    return typeof reason === "string" && blockedReasons[reason]
      ? blockedReasons[reason].guidance
      : "查看 agent 所在机器的执行器日志，确认原因后恢复运行。";
  }
  if (agent.metadata.execution_mode === "manual")
    return "任务需要由该 agent 的现有会话通过 MCP 主动领取。连接在线不代表会话已经开始处理。";
  if (agent.metadata.state === "busy")
    return "当前任务结束后，执行器会继续处理已排队的任务。";
  return "";
}

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
  const guidance = executionGuidance(agent);
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
              <dd>{blockedReasonLabel(metadata.blocked_reason)}</dd>
            </div>
          )}
      </dl>
      {guidance && (
        <p className="agent-execution-guidance small muted">{guidance}</p>
      )}
    </div>
  );
}
