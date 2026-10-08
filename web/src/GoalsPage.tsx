import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import { EmptyState, ErrorNotice, JsonView, Status, time } from "./components";
import { executionLabel } from "./AgentExecution";
import type {
  ArtifactReference,
  Goal,
  JsonObject,
  PlatformState,
  Task,
  TaskLink,
} from "./types";

type GoalAction =
  | { kind: "create" }
  | { kind: "edit" | "dispatch" | "status"; goal: Goal }
  | { kind: "review" | "retry"; task: Task; link: TaskLink };

function selectedFromHash(): string | null {
  const match = location.hash.match(/^#goals\/([^/]+)$/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function lines(value: FormDataEntryValue | null): string[] {
  return String(value ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

const reviewNames: Record<TaskLink["review_status"], string> = {
  pending: "待验收",
  accepted: "验收通过",
  changes_requested: "需要返工",
};

function latestDependency(id: string, links: TaskLink[]): string {
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

export default function GoalsPage({
  state,
  refresh,
}: {
  state: PlatformState;
  refresh: () => void;
}): ReactNode {
  const [selected, setSelected] = useState<string | null>(selectedFromHash);
  const [action, setAction] = useState<GoalAction | null>(null);
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const [resumeNotice, setResumeNotice] = useState<string | null>(null);
  const goals = state.goals ?? [];
  const links = state.task_links ?? [];
  const goal =
    goals.find((item) => item.id === selected) ??
    (selected ? null : goals[0]) ??
    null;
  const goalLinks = links.filter((link) => link.goal_id === goal?.id);
  const tasks = state.tasks.filter((task) =>
    goalLinks.some((link) => link.task_id === task.id),
  );
  const superseded = new Set(
    goalLinks.map((link) => link.retry_of).filter(Boolean),
  );
  const currentLinks = goalLinks.filter(
    (link) => !superseded.has(link.task_id),
  );
  const accepted = currentLinks.filter(
    (link) => link.review_status === "accepted",
  ).length;

  useEffect(() => {
    const changed = (): void => setSelected(selectedFromHash());
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);

  function choose(id: string): void {
    setSelected(id);
    setResumeError(null);
    setResumeNotice(null);
    location.hash = `goals/${encodeURIComponent(id)}`;
  }

  async function coordinate(current: Goal): Promise<void> {
    setResuming(true);
    setResumeError(null);
    setResumeNotice(null);
    try {
      await api.operate("update_goal", {
        goal_id: current.id,
        status: "active",
      });
      refresh();
      setResumeNotice("已请求 Chiliarch AI 继续协调。");
    } catch (failure) {
      setResumeError(
        failure instanceof Error ? failure.message : String(failure),
      );
    } finally {
      setResuming(false);
    }
  }

  return (
    <div className="management-page goals-page">
      <header className="page-header">
        <h1>目标</h1>
        <div className="actions">
          <button onClick={refresh} aria-label="刷新目标">
            刷新
          </button>
          <button
            className="primary"
            onClick={() => setAction({ kind: "create" })}
          >
            ＋ 新建目标
          </button>
        </div>
      </header>
      <div className={`goals-content${goals.length ? "" : " is-empty"}`}>
        {goals.length === 0 ? (
          <EmptyState title="尚无目标">
            创建一个目标，或让 Chiliarch AI 安排任务与验收。
          </EmptyState>
        ) : (
          <div className="goals-layout">
            <nav className="goal-list" aria-label="目标列表">
              {goals.map((item) => (
                <button
                  key={item.id}
                  className={item.id === goal?.id ? "is-selected" : ""}
                  aria-current={item.id === goal?.id ? "true" : undefined}
                  onClick={() => choose(item.id)}
                >
                  <strong>{item.title}</strong>
                  <span>
                    <Status value={item.status} />
                    <time>{time(item.updated_at)}</time>
                  </span>
                </button>
              ))}
            </nav>
            {goal ? (
              <section
                className="goal-detail"
                aria-label={`目标 ${goal.title}`}
                key={goal.id}
              >
                <div className="goal-heading">
                  <div>
                    <Status value={goal.status} />
                    <h2>{goal.title}</h2>
                  </div>
                  <button onClick={() => setAction({ kind: "edit", goal })}>
                    编辑目标
                  </button>
                </div>
                <p className="goal-objective">{goal.objective}</p>
                <div className="actions goal-coordinate-actions">
                  {["active", "blocked"].includes(goal.status) && (
                    <button
                      className="primary"
                      disabled={resuming}
                      onClick={() => void coordinate(goal)}
                    >
                      {resuming ? "正在请求…" : "继续协调"}
                    </button>
                  )}
                  {resumeNotice && (
                    <span className="muted small" role="status">
                      {resumeNotice}
                    </span>
                  )}
                </div>
                <ErrorNotice error={resumeError} />
                {goal.conversation_id && (
                  <a
                    className="goal-conversation-link"
                    href={`#ai/${encodeURIComponent(goal.conversation_id)}`}
                  >
                    查看协调会话 ↗
                  </a>
                )}
                <div className="goal-requirements">
                  <Criteria
                    title="约束"
                    items={goal.constraints}
                    empty="尚未设置约束"
                  />
                  <Criteria
                    title="验收条件"
                    items={goal.acceptance_criteria}
                    empty="尚未设置验收条件"
                  />
                </div>
                {goal.summary && (
                  <section className="goal-summary">
                    <h3>进展与结论</h3>
                    <p>{goal.summary}</p>
                  </section>
                )}
                <div className="goal-task-heading">
                  <div>
                    <h3>任务与交付</h3>
                    <p className="muted small">
                      {accepted} / {currentLinks.length} 项当前任务已验收
                    </p>
                  </div>
                  <button
                    className="primary"
                    disabled={["completed", "cancelled"].includes(goal.status)}
                    onClick={() => setAction({ kind: "dispatch", goal })}
                  >
                    派发任务
                  </button>
                </div>
                {tasks.length ? (
                  <ol className="goal-task-list">
                    {tasks.map((task) => {
                      const link = goalLinks.find(
                        (item) => item.task_id === task.id,
                      )!;
                      const retry = goalLinks.find(
                        (item) => item.retry_of === task.id,
                      );
                      const agent = state.agents.find(
                        (item) => item.id === task.agent_id,
                      );
                      const canRetry =
                        [
                          "failed",
                          "interrupted",
                          "cancelled",
                          "expired",
                        ].includes(task.status) ||
                        (task.status === "completed" &&
                          link.review_status === "changes_requested");
                      return (
                        <li
                          key={task.id}
                          id={`goal-task-${task.id}`}
                          className={retry ? "is-superseded" : ""}
                        >
                          <article aria-label={`任务 ${task.instruction}`}>
                            <div className="goal-task-title">
                              <h4>{task.instruction}</h4>
                              <Status value={task.status} />
                            </div>
                            <p className="goal-task-meta">
                              {agent
                                ? `${agent.name} · ${agent.machine}`
                                : task.agent_id}
                              <span>{reviewNames[link.review_status]}</span>
                            </p>
                            {agent && (
                              <p className="small muted">
                                {executionLabel(agent)}
                              </p>
                            )}
                            {link.depends_on.length > 0 && (
                              <div className="goal-dependencies">
                                <span className="muted">依赖验收：</span>
                                {link.depends_on.map((id) => {
                                  const latestId = latestDependency(id, links);
                                  const dependency = state.tasks.find(
                                    (item) => item.id === latestId,
                                  );
                                  const dependencyLink = links.find(
                                    (item) => item.task_id === latestId,
                                  );
                                  return (
                                    <a
                                      href={`#goals/${encodeURIComponent(goal.id)}`}
                                      key={id}
                                      onClick={(event) => {
                                        event.preventDefault();
                                        document
                                          .getElementById(
                                            `goal-task-${latestId}`,
                                          )
                                          ?.scrollIntoView({
                                            block: "center",
                                            behavior: "smooth",
                                          });
                                      }}
                                    >
                                      {dependency?.instruction ?? latestId} ·{" "}
                                      {dependencyLink
                                        ? reviewNames[
                                            dependencyLink.review_status
                                          ]
                                        : "未知"}
                                    </a>
                                  );
                                })}
                              </div>
                            )}
                            {link.retry_of && (
                              <p className="small muted">
                                重试自：
                                {state.tasks.find(
                                  (item) => item.id === link.retry_of,
                                )?.instruction ?? link.retry_of}{" "}
                                · {link.retry_of}
                              </p>
                            )}
                            {retry && (
                              <p className="small muted">
                                已由新尝试接续：{retry.task_id}
                              </p>
                            )}
                            {task.message && <p>{task.message}</p>}
                            {task.error && (
                              <p className="goal-task-error">{task.error}</p>
                            )}
                            {link.review_note && (
                              <p className="goal-review-note">
                                验收记录：{link.review_note}
                              </p>
                            )}
                            {link.acceptance_criteria.length > 0 && (
                              <Criteria
                                title="任务验收条件"
                                items={link.acceptance_criteria}
                              />
                            )}
                            {task.result !== null &&
                              task.result !== undefined && (
                                <details className="goal-result">
                                  <summary>执行结果</summary>
                                  <JsonView value={task.result} />
                                </details>
                              )}
                            <Artifacts artifacts={link.artifacts} />
                            {!retry && (
                              <div className="actions goal-task-actions">
                                {task.status === "completed" && (
                                  <button
                                    onClick={() =>
                                      setAction({ kind: "review", task, link })
                                    }
                                  >
                                    {link.review_status === "accepted"
                                      ? "更新验收记录"
                                      : "验收 / 返工"}
                                  </button>
                                )}
                                {canRetry && (
                                  <button
                                    onClick={() =>
                                      setAction({ kind: "retry", task, link })
                                    }
                                  >
                                    核查后重试
                                  </button>
                                )}
                              </div>
                            )}
                          </article>
                        </li>
                      );
                    })}
                  </ol>
                ) : (
                  <p className="empty-line">
                    尚未派发任务。可以选择不同 agent，并设置前后依赖。
                  </p>
                )}
                <footer className="goal-footer">
                  <span className="muted small">
                    更新于 {time(goal.updated_at)}
                  </span>
                  <button onClick={() => setAction({ kind: "status", goal })}>
                    更新目标状态
                  </button>
                </footer>
              </section>
            ) : (
              <EmptyState title="目标不存在">
                这个目标可能已不在当前列表中。
              </EmptyState>
            )}
          </div>
        )}
      </div>
      {action && (
        <GoalDialog
          action={action}
          state={state}
          onClose={() => setAction(null)}
          onSaved={(id) => {
            refresh();
            setAction(null);
            if (id) choose(id);
          }}
        />
      )}
    </div>
  );
}

function Criteria({
  title,
  items,
  empty,
}: {
  title: string;
  items: string[];
  empty?: string;
}): ReactNode {
  return (
    <section className="goal-criteria">
      <h3>{title}</h3>
      {items.length ? (
        <ul>
          {items.map((item, index) => (
            <li key={index}>{item}</li>
          ))}
        </ul>
      ) : (
        empty && <p className="small muted">{empty}</p>
      )}
    </section>
  );
}

function Artifacts({
  artifacts,
}: {
  artifacts: ArtifactReference[];
}): ReactNode {
  if (!artifacts.length) return null;
  return (
    <ul className="goal-artifacts" aria-label="交付产物">
      {artifacts.map((artifact, index) => {
        let link = false;
        try {
          const uri = new URL(artifact.uri);
          link =
            ["http:", "https:"].includes(uri.protocol) &&
            !uri.username &&
            !uri.password;
        } catch {
          /* Local paths and non-web URIs remain text. */
        }
        return (
          <li key={index}>
            <span>{artifact.kind} · </span>
            {link ? (
              <a href={artifact.uri} target="_blank" rel="noreferrer">
                {artifact.name} ↗
              </a>
            ) : (
              <span>
                {artifact.name} · <code>{artifact.uri}</code>
              </span>
            )}
            {artifact.digest && <small>{artifact.digest}</small>}
          </li>
        );
      })}
    </ul>
  );
}

function GoalDialog({
  action,
  state,
  onClose,
  onSaved,
}: {
  action: GoalAction;
  state: PlatformState;
  onClose: () => void;
  onSaved: (id?: string) => void;
}): ReactNode {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [decision, setDecision] = useState("accepted");
  const [artifacts, setArtifacts] = useState<ArtifactReference[]>(
    action.kind === "review" ? action.link.artifacts : [],
  );
  const title = {
    create: "新建目标",
    edit: "编辑目标",
    dispatch: "派发目标任务",
    review: "验收任务",
    retry: "核查后重试",
    status: "更新目标状态",
  }[action.kind];
  const goal = "goal" in action ? action.goal : null;
  const eligibleTasks = state.tasks.filter(
    (task) =>
      (state.task_links ?? []).some(
        (link) => link.task_id === task.id && link.goal_id === goal?.id,
      ) && !(state.task_links ?? []).some((link) => link.retry_of === task.id),
  );
  useEffect(() => {
    const element = dialog.current;
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    element?.showModal();
    element?.querySelector<HTMLElement>("input, textarea, select")?.focus();
    return () => {
      element?.close();
      previousFocus?.focus();
    };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy) return;
    const data = new FormData(event.currentTarget);
    const field = (name: string): string => String(data.get(name) ?? "").trim();
    setBusy(true);
    setError(null);
    try {
      let operation: string;
      let args: JsonObject;
      if (action.kind === "create" || action.kind === "edit") {
        if (
          !field("title") ||
          (action.kind === "create" && !field("objective"))
        )
          throw new Error("请填写目标名称和目标说明。");
        operation = action.kind === "create" ? "create_goal" : "update_goal";
        args = {
          title: field("title"),
          constraints: lines(data.get("constraints")),
          acceptance_criteria: lines(data.get("criteria")),
          ...(action.kind === "create"
            ? { objective: field("objective") }
            : { goal_id: action.goal.id, summary: field("summary") }),
        };
      } else if (action.kind === "dispatch") {
        if (!field("agent") || !field("instruction"))
          throw new Error("请选择 agent 并填写任务说明。");
        operation = "dispatch_task";
        args = {
          goal_id: action.goal.id,
          agent_id: field("agent"),
          instruction: field("instruction"),
          parameters: {},
          depends_on: data.getAll("dependency").map(String),
          acceptance_criteria: lines(data.get("criteria")),
        };
      } else if (action.kind === "review") {
        if (!field("note")) throw new Error("请填写验收依据或返工原因。");
        operation = "review_task";
        args = {
          task_id: action.task.id,
          decision,
          note: field("note"),
          artifacts: artifacts.map((artifact) => ({
            ...artifact,
            name: artifact.name.trim(),
            uri: artifact.uri.trim(),
            kind: artifact.kind.trim() || "reference",
          })),
        };
      } else if (action.kind === "retry") {
        if (!field("recovery"))
          throw new Error("请先核查之前的执行结果并填写核查说明。");
        operation = "retry_task";
        args = {
          task_id: action.task.id,
          recovery_note: field("recovery"),
          ...(field("instruction")
            ? { instruction: field("instruction") }
            : {}),
        };
      } else if (action.kind === "status") {
        operation = "update_goal";
        args = {
          goal_id: action.goal.id,
          status: field("status"),
          summary: field("summary"),
        };
      } else {
        throw new Error("不支持的目标操作。");
      }
      const result = await api.operate(operation, args);
      const savedGoal = result.goal;
      onSaved(
        action.kind === "create" &&
          savedGoal &&
          typeof savedGoal === "object" &&
          !Array.isArray(savedGoal) &&
          typeof savedGoal.id === "string"
          ? savedGoal.id
          : undefined,
      );
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={dialog}
      className="agent-pairing-dialog goal-dialog"
      aria-labelledby="goal-dialog-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <div className="agent-dialog-header">
        <h2 id="goal-dialog-title">{title}</h2>
        <button
          type="button"
          className="plain"
          aria-label={`关闭${title}`}
          disabled={busy}
          onClick={onClose}
        >
          ×
        </button>
      </div>
      <form className="goal-form" onSubmit={(event) => void submit(event)}>
        {(action.kind === "create" || action.kind === "edit") && (
          <>
            <label>
              目标名称
              <input
                name="title"
                maxLength={256}
                defaultValue={goal?.title ?? ""}
                required
              />
            </label>
            {action.kind === "create" && (
              <label>
                目标说明
                <textarea name="objective" rows={3} required />
              </label>
            )}
            <label>
              约束（每行一条）
              <textarea
                name="constraints"
                rows={3}
                defaultValue={goal?.constraints.join("\n") ?? ""}
              />
            </label>
            <label>
              验收条件（每行一条）
              <textarea
                name="criteria"
                rows={3}
                defaultValue={goal?.acceptance_criteria.join("\n") ?? ""}
              />
            </label>
            {action.kind === "edit" && (
              <label>
                进展与结论
                <textarea
                  name="summary"
                  rows={3}
                  defaultValue={goal?.summary ?? ""}
                />
              </label>
            )}
          </>
        )}
        {action.kind === "dispatch" && (
          <>
            <label>
              执行 agent
              <select name="agent" required defaultValue="">
                <option value="" disabled>
                  选择 agent
                </option>
                {state.agents
                  .filter((agent) => !agent.revoked)
                  .map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name} · {agent.machine} · {executionLabel(agent)}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              任务说明
              <textarea name="instruction" rows={4} required />
            </label>
            {eligibleTasks.length > 0 && (
              <fieldset className="goal-dependency-select">
                <legend>等待这些任务验收后执行</legend>
                {eligibleTasks.map((task) => (
                  <label key={task.id}>
                    <input name="dependency" type="checkbox" value={task.id} />
                    <span>{task.instruction}</span>
                  </label>
                ))}
              </fieldset>
            )}
            <label>
              任务验收条件（每行一条）
              <textarea name="criteria" rows={3} />
            </label>
          </>
        )}
        {action.kind === "review" && (
          <>
            <p className="muted small">{action.task.instruction}</p>
            <label>
              验收决定
              <select
                value={decision}
                onChange={(event) => setDecision(event.target.value)}
              >
                <option value="accepted">验收通过</option>
                <option
                  value="changes_requested"
                  disabled={action.link.review_status === "accepted"}
                >
                  需要返工
                </option>
              </select>
            </label>
            <label>
              验收依据 / 返工原因
              <textarea
                name="note"
                rows={3}
                defaultValue={action.link.review_note}
                required
              />
            </label>
            <fieldset className="goal-artifact-inputs">
              <legend>交付产物</legend>
              {artifacts.map((artifact, index) => (
                <div key={index} className="goal-artifact-input">
                  <label>
                    产物 {index + 1} 名称
                    <input
                      value={artifact.name}
                      required
                      onChange={(event) =>
                        setArtifacts((items) =>
                          items.map((item, i) =>
                            i === index
                              ? { ...item, name: event.target.value }
                              : item,
                          ),
                        )
                      }
                    />
                  </label>
                  <label>
                    产物 {index + 1} 地址
                    <input
                      value={artifact.uri}
                      required
                      placeholder="PR、提交、报告链接或路径"
                      onChange={(event) =>
                        setArtifacts((items) =>
                          items.map((item, i) =>
                            i === index
                              ? { ...item, uri: event.target.value }
                              : item,
                          ),
                        )
                      }
                    />
                  </label>
                  <button
                    type="button"
                    className="plain"
                    onClick={() =>
                      setArtifacts((items) =>
                        items.filter((_, i) => i !== index),
                      )
                    }
                  >
                    移除产物 {index + 1}
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={() =>
                  setArtifacts((items) => [
                    ...items,
                    { name: "", uri: "", kind: "reference" },
                  ])
                }
              >
                添加产物引用
              </button>
            </fieldset>
          </>
        )}
        {action.kind === "retry" && (
          <>
            <p className="muted small">
              先核查原执行是否已产生改动。重试会创建新任务，保留原记录与依赖。
            </p>
            <label>
              核查说明
              <textarea
                name="recovery"
                rows={3}
                placeholder="已检查哪些产物、改动或执行记录；为什么可以再次执行。"
                required
              />
            </label>
            <label>
              新的任务说明（可选）
              <textarea
                name="instruction"
                rows={3}
                placeholder={action.task.instruction}
              />
            </label>
          </>
        )}
        {action.kind === "status" && (
          <>
            <label>
              目标状态
              <select name="status" defaultValue={action.goal.status}>
                <option value="active">进行中</option>
                <option value="blocked">受阻</option>
                <option value="completed">已完成</option>
                <option value="cancelled">已取消</option>
              </select>
            </label>
            <p className="muted small">
              完成目标前，所有当前任务都必须执行完成并验收通过。取消目标会请求取消尚未结束的任务。
            </p>
            <label>
              进展与结论
              <textarea
                name="summary"
                rows={4}
                defaultValue={action.goal.summary}
              />
            </label>
          </>
        )}
        <ErrorNotice error={error} />
        <div className="actions goal-dialog-actions">
          <button type="button" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button className="primary" disabled={busy}>
            {busy
              ? "保存中…"
              : action.kind === "dispatch"
                ? "确认派发"
                : action.kind === "retry"
                  ? "确认重试"
                  : "保存"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
