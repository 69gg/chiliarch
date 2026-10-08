import { blockedReasonLabel } from "./AgentExecution";
import type { Goal, JsonObject, PlatformState, TaskLink } from "./types";

export interface GoalSignal {
  kind: string;
  label: string;
  message: string;
  taskId?: string;
}

export function latestDependency(id: string, links: TaskLink[]): string {
  const seen = new Set<string>();
  let current = id;
  while (!seen.has(current)) {
    seen.add(current);
    const next = links.find((link) => link.retry_of === current);
    if (!next) break;
    current = next.task_id;
  }
  return current;
}

export function goalSignals(goal: Goal, state: PlatformState): GoalSignal[] {
  if (goal.status === "completed") return [];
  const cancelled = goal.status === "cancelled";
  const links = state.task_links ?? [];
  const superseded = new Set(links.map((link) => link.retry_of));
  const goalLinks = links.filter((link) => link.goal_id === goal.id);
  const current = goalLinks.filter((link) => !superseded.has(link.task_id));
  const signals: GoalSignal[] = [];
  if (goal.status === "blocked")
    signals.push({
      kind: "blocked",
      label: "目标受阻",
      message: "目标已标记为受阻，处理阻碍后可继续协调。",
    });
  for (const link of current) {
    const task = state.tasks.find((item) => item.id === link.task_id);
    if (!task) continue;
    const add = (kind: string, label: string, message: string): void => {
      signals.push({ kind, label, message, taskId: task.id });
    };
    if (task.status === "completed") {
      if (cancelled) continue;
      if (link.review_status === "pending")
        add("review", "待验收", "执行已完成，核对交付产物后进行验收。");
      else if (link.review_status === "changes_requested")
        add(
          "retry",
          "待返工",
          "验收要求返工，核查已有改动后可重试或交给其他 agent。",
        );
      continue;
    }
    if (
      ["failed", "interrupted", "cancelled", "expired"].includes(task.status)
    ) {
      if (cancelled) continue;
      add(
        "recovery",
        "待核查",
        "本次执行未完成，先核查已有改动，再决定是否重试或交接。",
      );
      continue;
    }
    if (["cancellation_requested", "cancel_requested"].includes(task.status)) {
      add(
        "cancellation",
        "取消待确认",
        "尚未确认执行已停止，等待 agent 回报后再安排新尝试。",
      );
      continue;
    }
    if (task.status === "blocked") {
      const pending = link.depends_on.filter((id) => {
        const latest = latestDependency(id, links);
        return (
          !links.some(
            (item) =>
              item.task_id === latest && item.review_status === "accepted",
          ) ||
          !state.tasks.some(
            (item) => item.id === latest && item.status === "completed",
          )
        );
      });
      if (pending.length)
        add(
          "dependency",
          "等待依赖",
          `等待 ${pending.length} 项前置任务完成并通过验收。`,
        );
      else
        add(
          "dispatch",
          "等待调度",
          "依赖已满足，等待平台释放任务；可查看协调会话了解进展。",
        );
    }
    const agent = state.agents.find((item) => item.id === task.agent_id);
    if (!agent || agent.revoked)
      add(
        "agent",
        "身份不可用",
        "执行 agent 的身份不可用，需确认旧执行结果后再安排交接。",
      );
    else if (!agent.online)
      add(
        "offline",
        "agent 离线",
        "等待 agent 主动重新连接；离线不表示执行已经停止。",
      );
    else if (agent.metadata.state === "blocked")
      add(
        "workspace",
        "执行受阻",
        blockedReasonLabel(agent.metadata.blocked_reason),
      );
    else if (
      ["queued", "delivered"].includes(task.status) &&
      agent.metadata.execution_mode === "manual"
    )
      add(
        "claim",
        "等待领取",
        "请在对应 agent 会话中领取任务，平台连接不会自动唤起现有会话。",
      );
  }
  const belongsToGoal = (arguments_: JsonObject): boolean =>
    arguments_.goal_id === goal.id ||
    goalLinks.some((link) => link.task_id === arguments_.task_id);
  const requests = state.requests.filter(
    (request) =>
      request.status === "pending" && belongsToGoal(request.arguments),
  );
  const approvals = state.approvals.filter((approval) => {
    if (approval.status !== "pending") return false;
    if (belongsToGoal(approval.arguments)) return true;
    if (approval.operation !== "resolve_agent_request") return false;
    const request = state.requests.find(
      (item) => item.id === approval.arguments.request_id,
    );
    return Boolean(request && belongsToGoal(request.arguments));
  });
  const awaitingPermission = new Set(
    approvals
      .filter((approval) => approval.operation === "resolve_agent_request")
      .map((approval) => approval.arguments.request_id),
  );
  for (const request of requests) {
    if (awaitingPermission.has(request.id)) continue;
    signals.push({
      kind: "request",
      label: "待处理请求",
      message: `下级请求 ${request.operation} 等待 Chiliarch AI 处理，可在协调会话中查看。`,
    });
  }
  for (const approval of approvals)
    signals.push({
      kind: "approval",
      label: "等待许可",
      message: `操作 ${approval.operation} 等待用户在协调会话中审批。`,
    });
  const pendingWork = requests.length > 0 || approvals.length > 0;
  if (!cancelled && !current.length && !pendingWork)
    signals.push({
      kind: "unassigned",
      label: "待派工",
      message: "尚未安排任务，可派发任务或让 Chiliarch AI 继续协调。",
    });
  else if (
    !cancelled &&
    current.length &&
    current.every(
      (link) =>
        link.review_status === "accepted" &&
        state.tasks.some(
          (task) => task.id === link.task_id && task.status === "completed",
        ),
    ) &&
    !pendingWork
  )
    signals.push({
      kind: "finish",
      label: "待结束目标",
      message: "当前任务均已验收，可核对目标标准并更新最终状态。",
    });
  return signals;
}

export function summarizeSignals(signals: GoalSignal[]): string {
  const counts = new Map<string, number>();
  for (const signal of signals)
    counts.set(signal.label, (counts.get(signal.label) ?? 0) + 1);
  return Array.from(counts, ([label, count]) => `${label} ${count}`).join(
    " · ",
  );
}
