import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import ProjectDialog from "./ProjectDialog";
import { ProjectInsights } from "./ProjectInsights";
import { errorText, latestFacts } from "./projectUtils";
import { EmptyState, ErrorNotice, JsonView, Status, time } from "./components";
import type { JsonObject, PlatformState } from "./types";
import type {
  Project,
  ProjectCommand,
  ProjectDetail,
  ProjectDelivery,
  ProjectAction,
  ProjectWorkItem,
} from "./projectTypes";

type Tab = "work" | "issues" | "nodes" | "facts" | "policy";

function selectedFromHash(): string | null {
  const match = location.hash.match(/^#projects\/([^/]+)$/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function projectUrl(project: Project): string | null {
  if (!/^[a-z\d.-]+$/i.test(project.github_host)) return null;
  if (!/^[\w.-]+\/[\w.-]+$/.test(project.repository)) return null;
  return `https://${project.github_host}/${project.repository}`;
}

export default function ProjectsPage({
  state,
  refresh,
}: {
  state: PlatformState;
  refresh: () => void;
}): ReactNode {
  const [selected, setSelected] = useState<string | null>(selectedFromHash);
  const [projects, setProjects] = useState<Project[]>([]);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [tab, setTab] = useState<Tab>("work");
  const [action, setAction] = useState<ProjectAction | null>(null);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const projectId = selected ?? projects[0]?.id ?? null;
  const current = detail?.project.id === projectId ? detail : null;
  const project = current?.project;
  const facts = current ? latestFacts(current.facts) : [];
  const pollSeconds = Number(
    (state.config.server as JsonObject | undefined)?.config_poll_seconds ?? 5,
  );

  useEffect(() => {
    const changed = (): void => setSelected(selectedFromHash());
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  useEffect(() => {
    const timer = setInterval(
      () => setRevision((value) => value + 1),
      pollSeconds * 1000,
    );
    return () => clearInterval(timer);
  }, [pollSeconds]);
  useEffect(() => {
    let active = true;
    void api
      .operate<{ projects: Project[] }>("list_projects", {})
      .then((result) => {
        if (active) {
          setProjects(result.projects);
          setListError(null);
        }
      })
      .catch((failure: unknown) => {
        if (active) setListError(errorText(failure));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [revision]);
  useEffect(() => {
    if (!projectId) return;
    let active = true;
    setLoadingDetail(true);
    void api
      .operate<ProjectDetail>("get_project", { project_id: projectId })
      .then((result) => {
        if (active) {
          setDetail(result);
          setDetailError(null);
        }
      })
      .catch((failure: unknown) => {
        if (active) setDetailError(errorText(failure));
      })
      .finally(() => {
        if (active) setLoadingDetail(false);
      });
    return () => {
      active = false;
    };
  }, [projectId, revision]);

  function reload(): void {
    setRevision((value) => value + 1);
  }
  function choose(id: string): void {
    setSelected(id);
    setDetailError(null);
    setNotice(null);
    setError(null);
    location.hash = `projects/${encodeURIComponent(id)}`;
  }
  async function operate(name: string, args: JsonObject): Promise<void> {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.operate<{
        command?: ProjectCommand;
        status?: string;
      }>(name, args);
      setNotice(
        result.command
          ? `操作已登记（${result.command.id}），等待节点执行；当前状态：${commandLabel(result.command.status)}。`
          : result.status === "awaiting_approval"
            ? "操作正在等待许可。"
            : "操作已保存。页面将持续更新执行结果。",
      );
      reload();
      refresh();
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }
  const visibleProjects = projects.filter((item) =>
    `${item.name} ${item.repository}`
      .toLowerCase()
      .includes(query.trim().toLowerCase()),
  );

  return (
    <div className="management-page projects-page">
      <header className="page-header">
        <h1>项目</h1>
        <div className="actions">
          <button
            onClick={reload}
            disabled={loading || loadingDetail}
            aria-label="刷新项目"
          >
            刷新
          </button>
          <button
            className="primary"
            onClick={() => setAction({ kind: "create" })}
          >
            ＋ 登记项目
          </button>
        </div>
      </header>
      <div className="projects-content">
        <ErrorNotice error={listError ?? detailError} />
        <ErrorNotice error={error} onDismiss={() => setError(null)} />
        {notice && (
          <p className="notice" role="status">
            {notice}
          </p>
        )}
        {loading ? (
          <p className="muted" role="status">
            读取项目…
          </p>
        ) : projects.length === 0 ? (
          <EmptyState title="把仓库变成协作项目">
            登记 GitHub 仓库，让 agents 在各自的分支和工作区里协作交付。
          </EmptyState>
        ) : (
          <div className="projects-layout">
            <aside className="project-browser" aria-label="项目列表">
              <label>
                查找项目
                <input
                  type="search"
                  placeholder="名称或仓库"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </label>
              <div className="project-list">
                {visibleProjects.map((item) => (
                  <button
                    key={item.id}
                    className={item.id === projectId ? "is-selected" : ""}
                    aria-pressed={item.id === projectId}
                    onClick={() => choose(item.id)}
                  >
                    <strong>{item.name}</strong>
                    <small>{item.repository}</small>
                    <span>{item.target_branch}</span>
                  </button>
                ))}
              </div>
              {visibleProjects.length === 0 && (
                <p className="empty-line">没有匹配的项目</p>
              )}
            </aside>
            <section className="project-detail" aria-label="项目详情">
              {!current || !project ? (
                <p role="status" className="muted">
                  {loadingDetail
                    ? "读取项目详情…"
                    : "无法读取这个项目，请刷新或选择其他项目。"}
                </p>
              ) : (
                <>
                  <div className="project-heading">
                    <div>
                      <p className="muted small">{project.github_host}</p>
                      <h2>{project.name}</h2>
                      {projectUrl(project) ? (
                        <a
                          href={projectUrl(project)!}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {project.repository} ↗
                        </a>
                      ) : (
                        <span>{project.repository}</span>
                      )}
                    </div>
                    <span className="project-target">
                      目标分支 <code>{project.target_branch}</code>
                    </span>
                  </div>
                  <nav className="project-tabs" aria-label="项目内容">
                    {(
                      [
                        ["work", "工作与交付"],
                        ["issues", "Issues"],
                        ["nodes", "执行节点"],
                        ["facts", "共同事实"],
                        ["policy", "流程策略"],
                      ] as const
                    ).map(([id, label]) => (
                      <button
                        key={id}
                        aria-current={tab === id ? "page" : undefined}
                        onClick={() => setTab(id)}
                      >
                        {label}
                      </button>
                    ))}
                  </nav>
                  {tab === "work" && (
                    <>
                      <div className="project-section-heading">
                        <div>
                          <h3>工作项</h3>
                          <p className="muted small">
                            分支、执行尝试和交付证据保留在同一个工作项中。
                          </p>
                        </div>
                        <button
                          onClick={() => setAction({ kind: "work", project })}
                        >
                          新建工作项
                        </button>
                      </div>
                      {current.work_items.length ? (
                        <div className="project-work-list">
                          {current.work_items.map((work) => (
                            <WorkItem
                              key={work.id}
                              work={work}
                              detail={current}
                              state={state}
                              busy={busy}
                              onStart={() =>
                                setAction({ kind: "start", project, work })
                              }
                              onDeliver={() =>
                                setAction({ kind: "deliver", project, work })
                              }
                              onCommand={() =>
                                setAction({ kind: "command", project, work })
                              }
                              onCleanup={() =>
                                setAction({ kind: "cleanup", project, work })
                              }
                              onFacts={() =>
                                setAction({ kind: "work_facts", project, work })
                              }
                              onDispatchReview={(delivery) =>
                                setAction({
                                  kind: "dispatch_review",
                                  project,
                                  delivery,
                                })
                              }
                              onReview={(delivery) =>
                                setAction({ kind: "review", project, delivery })
                              }
                              onMerge={(delivery) =>
                                setAction({ kind: "merge", project, delivery })
                              }
                              onInspect={(delivery) =>
                                void operate("inspect_project_delivery", {
                                  delivery_id: delivery.id,
                                })
                              }
                            />
                          ))}
                        </div>
                      ) : (
                        <p className="empty-line">
                          还没有工作项。创建后可以指定执行 agent 和代码基线。
                        </p>
                      )}
                      <div className="actions project-work-tools">
                        <button
                          onClick={() =>
                            setAction({ kind: "finalize", project })
                          }
                        >
                          创建目标总 PR
                        </button>
                        <button
                          disabled={!project.policy.allow_release}
                          onClick={() =>
                            setAction({ kind: "release", project })
                          }
                        >
                          发起发布
                        </button>
                        <button
                          onClick={() =>
                            setAction({ kind: "inspect_release", project })
                          }
                        >
                          核对发布结果
                        </button>
                      </div>
                      <CommandList
                        commands={current.commands}
                        state={state}
                        busy={busy}
                        onReconcile={(command) =>
                          void operate("reconcile_project_command", {
                            command_id: command.id,
                          })
                        }
                      />
                      <ProjectInsights
                        key={project.id}
                        projectId={project.id}
                        revision={revision}
                      />
                    </>
                  )}
                  {tab === "issues" && (
                    <>
                      <div className="project-section-heading">
                        <div>
                          <h3>GitHub Issues</h3>
                          <p className="muted small">
                            按编号读取或导入一个已有 Issue，记录进展并关联目标。
                          </p>
                        </div>
                        <button
                          onClick={() =>
                            setAction({ kind: "issue_read", project })
                          }
                        >
                          查看 / 导入 Issue
                        </button>
                      </div>
                      {current.issues.length ? (
                        current.issues.map((issue) => (
                          <article
                            className="project-record"
                            key={issue.id}
                            aria-label={`Issue ${issue.number}`}
                          >
                            <div className="row-between">
                              <h4>
                                {typeof issue.observation.url === "string" &&
                                safeLink(issue.observation.url) ? (
                                  <a
                                    href={safeLink(issue.observation.url)}
                                    target="_blank"
                                    rel="noreferrer"
                                  >
                                    #{issue.number} ·{" "}
                                    {String(issue.observation.title ?? "Issue")}{" "}
                                    ↗
                                  </a>
                                ) : (
                                  `#${issue.number} · ${String(issue.observation.title ?? "Issue")}`
                                )}
                              </h4>
                              <Status
                                value={String(
                                  issue.observation.state ?? "unknown",
                                ).toLowerCase()}
                              />
                            </div>
                            <p className="muted small">
                              最近核对：{time(issue.observed_at)}
                            </p>
                            {issue.goal_id && (
                              <p className="small">
                                <a
                                  href={`#goals/${encodeURIComponent(issue.goal_id)}`}
                                >
                                  查看关联目标 ↗
                                </a>
                              </p>
                            )}
                            <details>
                              <summary>Issue 正文与标签</summary>
                              <p className="project-prose">
                                {String(issue.observation.body ?? "")}
                              </p>
                              <p className="muted small">
                                标签：
                                {Array.isArray(issue.observation.labels)
                                  ? issue.observation.labels
                                      .map((label) =>
                                        typeof label === "string"
                                          ? label
                                          : label &&
                                              typeof label === "object" &&
                                              !Array.isArray(label)
                                            ? String(label.name ?? "")
                                            : "",
                                      )
                                      .filter(Boolean)
                                      .join("、") || "无"
                                  : "无"}
                              </p>
                            </details>
                            <div className="actions">
                              <button
                                onClick={() =>
                                  setAction({
                                    kind: "issue_comment",
                                    project,
                                    issue,
                                  })
                                }
                              >
                                添加进展评论
                              </button>
                              <button
                                onClick={() =>
                                  setAction({
                                    kind: "issue_update",
                                    project,
                                    issue,
                                  })
                                }
                              >
                                修改 Issue 状态 / 标签
                              </button>
                            </div>
                          </article>
                        ))
                      ) : (
                        <p className="empty-line">
                          尚未读取 Issue。填写仓库中的 Issue 编号即可开始。
                        </p>
                      )}
                      <CommandList
                        commands={current.commands.filter((command) =>
                          [
                            "inspect_issue",
                            "comment_issue",
                            "update_issue",
                          ].includes(command.action),
                        )}
                        state={state}
                        busy={busy}
                        onReconcile={(command) =>
                          void operate("reconcile_project_command", {
                            command_id: command.id,
                          })
                        }
                      />
                    </>
                  )}
                  {tab === "nodes" && (
                    <>
                      <div className="project-section-heading">
                        <div>
                          <h3>执行节点</h3>
                          <p className="muted small">
                            在节点上使用已有 Git、GitHub 登录与签名设置。
                          </p>
                        </div>
                        <button
                          onClick={() => setAction({ kind: "bind", project })}
                        >
                          绑定节点
                        </button>
                      </div>
                      {current.bindings.length ? (
                        current.bindings.map((binding) => {
                          const agent = state.agents.find(
                            (item) => item.id === binding.agent_id,
                          );
                          return (
                            <article
                              className="project-record"
                              key={binding.agent_id}
                              aria-label={`节点 ${agent?.name ?? binding.agent_id}`}
                            >
                              <div className="row-between">
                                <h4>
                                  {agent?.name ?? binding.agent_id}
                                  <small>{agent?.machine}</small>
                                </h4>
                                <Status
                                  value={agent?.online ? "online" : "offline"}
                                />
                              </div>
                              <p className="small">
                                {binding.repo_path ? (
                                  <>
                                    已有仓库 <code>{binding.repo_path}</code>
                                  </>
                                ) : (
                                  "由守护进程准备专用副本和独立 worktree"
                                )}
                              </p>
                              <p className="small muted">
                                最近检查：{time(binding.observed_at)}
                              </p>
                              {Object.keys(binding.observation ?? {}).length >
                                0 && (
                                <details>
                                  <summary>能力与认证检查结果</summary>
                                  <JsonView value={binding.observation} />
                                </details>
                              )}
                              <button
                                disabled={busy || agent?.revoked}
                                onClick={() =>
                                  void operate("inspect_project_repository", {
                                    project_id: project.id,
                                    agent_id: binding.agent_id,
                                  })
                                }
                              >
                                检查仓库能力
                              </button>
                            </article>
                          );
                        })
                      ) : (
                        <p className="empty-line">
                          尚未绑定节点。绑定后可检查仓库访问、Git 身份和工作区。
                        </p>
                      )}
                      <CommandList
                        commands={current.commands.filter(
                          (command) => command.action === "inspect_repository",
                        )}
                        state={state}
                        busy={busy}
                        onReconcile={(command) =>
                          void operate("reconcile_project_command", {
                            command_id: command.id,
                          })
                        }
                      />
                    </>
                  )}
                  {tab === "facts" && (
                    <>
                      <div className="project-section-heading">
                        <h3>共同事实</h3>
                        <button
                          onClick={() => setAction({ kind: "fact", project })}
                        >
                          登记事实
                        </button>
                      </div>
                      <p className="muted small">
                        版本化保存需求、接口约定和决定。依赖旧版本的工作需要重新确认。
                      </p>
                      {facts.length ? (
                        facts.map((fact) => (
                          <article className="project-record" key={fact.id}>
                            <div className="row-between">
                              <h4>{fact.key}</h4>
                              <span className="muted small">
                                版本 {fact.version}
                              </span>
                            </div>
                            <p className="project-prose">{fact.content}</p>
                            <p className="muted small">来源：{fact.source}</p>
                            <small className="muted">
                              {time(fact.created_at)}
                            </small>
                            <div className="actions">
                              <button
                                onClick={() =>
                                  setAction({ kind: "fact", project, fact })
                                }
                              >
                                更新事实
                              </button>
                            </div>
                          </article>
                        ))
                      ) : (
                        <p className="empty-line">尚未登记共同事实。</p>
                      )}
                    </>
                  )}
                  {tab === "policy" && (
                    <>
                      <div className="project-section-heading">
                        <div>
                          <h3>流程策略</h3>
                          <p className="muted small">
                            仓库规则和原生权限始终生效。
                          </p>
                        </div>
                        <button
                          onClick={() => setAction({ kind: "edit", project })}
                        >
                          编辑策略
                        </button>
                      </div>
                      <dl className="project-facts">
                        <div>
                          <dt>提交签名</dt>
                          <dd>
                            {project.policy.require_signed_commits
                              ? "必须签名，不可用时暂停"
                              : "继承本机 Git 设置"}
                          </dd>
                        </div>
                        <div>
                          <dt>独立评审</dt>
                          <dd>
                            {project.policy.require_review
                              ? "必须通过"
                              : "按工作需要安排"}
                          </dd>
                        </div>
                        <div>
                          <dt>必要 CI</dt>
                          <dd>
                            {project.policy.required_checks.length
                              ? project.policy.required_checks.join("、")
                              : "未登记必要检查"}
                          </dd>
                        </div>
                        <div>
                          <dt>auto 合并</dt>
                          <dd>
                            {project.policy.allow_auto_merge
                              ? "满足项目要求后允许"
                              : "需用户许可"}
                          </dd>
                        </div>
                        <div>
                          <dt>合并方式</dt>
                          <dd>{project.policy.merge_method}</dd>
                        </div>
                        <div>
                          <dt>发布</dt>
                          <dd>
                            {project.policy.allow_release
                              ? "允许独立发起发布流程"
                              : "未开放"}
                          </dd>
                        </div>
                        <div>
                          <dt>Tag 格式</dt>
                          <dd>
                            <code>{project.release.tag_template}</code>
                          </dd>
                        </div>
                        <div>
                          <dt>发布工作流</dt>
                          <dd>{project.release.workflow || "未配置"}</dd>
                        </div>
                        <div>
                          <dt>Release 创建者</dt>
                          <dd>
                            {project.release.release_owner === "workflow"
                              ? "GitHub 工作流"
                              : "守护进程"}
                          </dd>
                        </div>
                      </dl>
                      {Object.keys(project.release.commands).length > 0 && (
                        <details>
                          <summary>已登记的版本与验证命令</summary>
                          <JsonView value={project.release.commands} />
                        </details>
                      )}
                      <div className="project-section-heading">
                        <div>
                          <h3>事件规则</h3>
                          <p className="muted small">
                            只有允许来源及匹配条件的事件才创建目标。
                          </p>
                        </div>
                        <div className="actions">
                          <button
                            onClick={() => setAction({ kind: "poll", project })}
                          >
                            检查 GitHub 事件
                          </button>
                          <button
                            onClick={() => setAction({ kind: "rule", project })}
                          >
                            添加规则
                          </button>
                        </div>
                      </div>
                      {current.rules.length ? (
                        current.rules.map((rule) => (
                          <article className="project-record" key={rule.id}>
                            <div className="row-between">
                              <h4>{rule.event_type}</h4>
                              <span className="muted small">
                                {rule.enabled ? "已启用" : "已停用"}
                              </span>
                            </div>
                            <p className="small">
                              允许来源：{rule.allowed_actors.join("、")}
                            </p>
                            {rule.label && (
                              <p className="small">标签：{rule.label}</p>
                            )}
                            {rule.command && (
                              <p className="small">
                                指令：<code>{rule.command}</code>
                              </p>
                            )}
                            <p className="project-prose">
                              {rule.objective_template}
                            </p>
                            <button
                              onClick={() =>
                                setAction({ kind: "rule", project, rule })
                              }
                            >
                              编辑规则
                            </button>
                          </article>
                        ))
                      ) : (
                        <p className="empty-line">未配置自动触发规则。</p>
                      )}
                    </>
                  )}
                </>
              )}
            </section>
          </div>
        )}
      </div>
      {action && (
        <ProjectDialog
          key={action.kind}
          action={action}
          state={state}
          detail={current}
          onClose={() => setAction(null)}
          onSaved={(result) => {
            setAction(null);
            if (
              result.project &&
              typeof result.project === "object" &&
              !Array.isArray(result.project) &&
              typeof result.project.id === "string"
            )
              choose(result.project.id);
            setNotice(
              result.command
                ? "操作已登记，等待节点执行。"
                : result.status === "awaiting_approval"
                  ? "操作正在等待许可。"
                  : "已保存。",
            );
            reload();
            refresh();
          }}
        />
      )}
    </div>
  );
}

const commandNames: Record<string, string> = {
  inspect_issue: "读取 Issue",
  comment_issue: "发布 Issue 评论",
  update_issue: "更新 Issue",
  cleanup_workspace: "清理受管工作区",
  inspect_repository: "检查仓库",
  prepare_workspace: "准备工作区",
  deliver: "提交与 PR",
  create_pull_request: "创建总 PR",
  inspect_pull_request: "核对 PR / CI",
  merge_pull_request: "合并",
  publish_tag: "推送发布 Tag",
  inspect_release: "核对发布",
  publish_review: "同步评审",
  list_events: "读取 GitHub 事件",
  run_project_command: "运行项目命令",
  reconcile: "核对执行结果",
};
function commandLabel(status: string | undefined): string {
  return (
    (
      {
        pending: "等待节点",
        queued: "等待节点",
        running: "执行中",
        succeeded: "已完成",
        completed: "已完成",
        failed: "执行失败",
        unknown: "结果未知，需核对",
        blocked: "受阻",
        unverified: "尚未检查",
      } as Record<string, string>
    )[status ?? ""] ??
    status ??
    "尚未检查"
  );
}
function CommandList({
  commands,
  state,
  busy,
  onReconcile,
}: {
  commands: ProjectCommand[];
  state: PlatformState;
  busy: boolean;
  onReconcile: (command: ProjectCommand) => void;
}): ReactNode {
  return (
    <section className="project-commands" aria-label="执行记录">
      <h3>执行记录</h3>
      {commands.length ? (
        [...commands]
          .sort(
            (a, b) =>
              new Date(
                typeof b.created_at === "number"
                  ? b.created_at * 1000
                  : b.created_at,
              ).getTime() -
              new Date(
                typeof a.created_at === "number"
                  ? a.created_at * 1000
                  : a.created_at,
              ).getTime(),
          )
          .map((command) => (
            <details className="project-command" key={command.id}>
              <summary>
                <span>
                  {commandNames[command.action] ?? command.action}
                  <small>
                    {state.agents.find((agent) => agent.id === command.agent_id)
                      ?.name ?? command.agent_id}{" "}
                    · {time(command.updated_at ?? command.created_at)}
                  </small>
                </span>
                <span>{commandLabel(command.status)}</span>
              </summary>
              {command.status === "unknown" && (
                <p className="notice">
                  结果未知。先核对远端分支、PR
                  或发布结果，再决定下一步；不会自动重新执行。
                </p>
              )}
              {command.status === "pending" && (
                <p className="muted small">
                  等待该节点主动连接并领取操作。登记成功不代表执行完成。
                </p>
              )}
              {command.error && <p className="notice error">{command.error}</p>}
              <p className="muted small">操作 ID：{command.id}</p>
              {["publish_tag", "inspect_release"].includes(command.action) && (
                <ReleaseObservation result={command.result} />
              )}
              {Object.keys(command.result).length > 0 && (
                <JsonView value={command.result} />
              )}
              {["unknown", "pending"].includes(command.status) && (
                <button disabled={busy} onClick={() => onReconcile(command)}>
                  核对原操作结果
                </button>
              )}
            </details>
          ))
      ) : (
        <p className="empty-line">尚无执行记录</p>
      )}
    </section>
  );
}

function ReleaseObservation({ result }: { result: JsonObject }): ReactNode {
  const runs = Array.isArray(result.workflow_runs) ? result.workflow_runs : [];
  return (
    <div className="project-release-observation">
      <p>
        GitHub Release：
        {result.published === true
          ? "已观察到正式 Release"
          : "尚未确认正式发布"}
      </p>
      {typeof result.tag === "string" && (
        <p className="muted small">
          Tag：<code>{result.tag}</code> · Commit：
          <code>{String(result.commit_sha ?? "尚未确认")}</code>
        </p>
      )}
      {runs.length ? (
        <ul>
          {runs.map((value, index) => {
            if (!value || typeof value !== "object" || Array.isArray(value))
              return null;
            return (
              <li key={index}>
                {String(
                  value.name ??
                    value.workflowName ??
                    value.databaseId ??
                    "发布工作流",
                )}
                ：
                {value.conclusion === "success"
                  ? "通过"
                  : value.conclusion === "failure"
                    ? "失败"
                    : String(value.status ?? "等待结果")}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="muted small">尚未观察到对应发布工作流结果。</p>
      )}
      <p className="muted small">
        仓库操作完成不代表发布完成，请核对工作流及项目发布目标。
      </p>
    </div>
  );
}
function safeLink(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
function WorkItem({
  work,
  detail,
  state,
  busy,
  onStart,
  onDeliver,
  onCommand,
  onFacts,
  onCleanup,
  onReview,
  onDispatchReview,
  onMerge,
  onInspect,
}: {
  work: ProjectWorkItem;
  detail: ProjectDetail;
  state: PlatformState;
  busy: boolean;
  onStart: () => void;
  onDeliver: () => void;
  onCommand: () => void;
  onFacts: () => void;
  onCleanup: () => void;
  onReview: (delivery: ProjectDelivery) => void;
  onDispatchReview: (delivery: ProjectDelivery) => void;
  onMerge: (delivery: ProjectDelivery) => void;
  onInspect: (delivery: ProjectDelivery) => void;
}): ReactNode {
  const attempts = detail.attempts.filter(
    (attempt) => attempt.work_item_id === work.id,
  );
  const deliveries = detail.deliveries.filter(
    (delivery) => delivery.work_item_id === work.id,
  );
  const currentAttempt = attempts.find(
    (attempt) => attempt.id === work.current_attempt_id,
  );
  return (
    <article className="project-record" aria-label={`工作项 ${work.title}`}>
      <div className="row-between">
        <h4>{work.title}</h4>
        <Status value={work.state} />
      </div>
      <p className="project-prose">{work.instruction}</p>
      <div className="project-work-meta">
        <span>
          {work.delivery_mode === "integrated"
            ? "汇入目标集成分支"
            : "单独 PR 交付"}
        </span>
        {work.goal_id && (
          <a href={`#goals/${encodeURIComponent(work.goal_id)}`}>关联目标 ↗</a>
        )}
      </div>
      {work.stale_facts.length > 0 && (
        <p className="notice">
          依据已经变化：{work.stale_facts.join("、")}
          。需要确认新版本并重新验证交付。
        </p>
      )}
      {work.depends_on.length > 0 && (
        <p className="muted small">
          前置工作：
          {work.depends_on
            .map(
              (id) =>
                detail.work_items.find((item) => item.id === id)?.title ?? id,
            )
            .join("、")}
        </p>
      )}
      {work.acceptance_criteria.length > 0 && (
        <details>
          <summary>验收条件</summary>
          <ul>
            {work.acceptance_criteria.map((criterion, index) => (
              <li key={index}>{criterion}</li>
            ))}
          </ul>
        </details>
      )}
      {attempts.map((attempt) => (
        <div
          className={`project-attempt${attempt.id !== work.current_attempt_id ? " is-historical" : ""}`}
          key={attempt.id}
        >
          <div className="row-between">
            <strong>
              尝试 {attempt.number} ·{" "}
              {state.agents.find((agent) => agent.id === attempt.agent_id)
                ?.name ?? attempt.agent_id}
            </strong>
            <Status value={attempt.state} />
          </div>
          <dl>
            <dt>分支</dt>
            <dd>
              <code>{attempt.branch}</code>
            </dd>
            <dt>代码基线</dt>
            <dd>
              <code>{attempt.base_sha}</code>
            </dd>
            <dt>合入分支</dt>
            <dd>
              <code>{attempt.target_branch}</code>
            </dd>
            {attempt.workdir && (
              <>
                <dt>工作目录</dt>
                <dd>
                  <code>{attempt.workdir}</code>
                </dd>
              </>
            )}
          </dl>
          {attempt.task_id && (
            <span className="small muted">任务 {attempt.task_id}</span>
          )}
        </div>
      ))}
      {deliveries.map((delivery) => (
        <div
          className="project-delivery"
          key={delivery.id}
          aria-label={`PR ${delivery.pr_number}`}
        >
          <div className="row-between">
            <h4>
              {safeLink(delivery.url) ? (
                <a
                  href={safeLink(delivery.url)}
                  target="_blank"
                  rel="noreferrer"
                >
                  PR #{delivery.pr_number} ↗
                </a>
              ) : (
                `PR #${delivery.pr_number}`
              )}
            </h4>
            <Status value={delivery.state.toLowerCase()} />
          </div>
          <dl className="project-shas">
            <dt>Head</dt>
            <dd>
              <code>{delivery.head_sha}</code>
            </dd>
            <dt>Base</dt>
            <dd>
              <code>{delivery.base_sha}</code>
            </dd>
            {delivery.merge_commit_sha && (
              <>
                <dt>合并提交</dt>
                <dd>
                  <code>{delivery.merge_commit_sha}</code>
                </dd>
              </>
            )}
          </dl>
          <p className="muted small">最近核对：{time(delivery.observed_at)}</p>
          <Checks observation={delivery.observation} />
          {delivery.reviews.map((review) => (
            <div className="project-review" key={review.id}>
              <strong>
                {review.decision === "accepted" ? "评审通过" : "要求修改"}
                {review.current ? "" : " · 已过期"}
              </strong>
              <span className="muted small">
                {state.agents.find((agent) => agent.id === review.reviewer)
                  ?.name ?? review.reviewer}
              </span>
              <p>{review.note}</p>
              {review.evidence.length > 0 && (
                <details>
                  <summary>评审证据</summary>
                  <JsonView value={review.evidence} />
                </details>
              )}
            </div>
          ))}
          {!delivery.reviews.length && (
            <p className="muted small">尚无平台独立评审</p>
          )}
          <div className="actions">
            <button disabled={busy} onClick={() => onInspect(delivery)}>
              核对 PR / CI
            </button>
            {delivery.state.toLowerCase() !== "merged" && (
              <>
                <button onClick={() => onDispatchReview(delivery)}>
                  安排评审任务
                </button>
                <button onClick={() => onReview(delivery)}>记录评审</button>
                <button onClick={() => onMerge(delivery)}>合并交付</button>
              </>
            )}
          </div>
        </div>
      ))}
      <div className="actions">
        <button onClick={onStart}>分配分支与工作区</button>
        {detail.facts.length > 0 && (
          <button onClick={onFacts}>
            {work.stale_facts.length > 0 ? "确认事实新版本" : "更新事实依赖"}
          </button>
        )}
        {currentAttempt && <button onClick={onDeliver}>提交并创建 PR</button>}
        {currentAttempt && <button onClick={onCleanup}>清理本机工作区</button>}
        {currentAttempt &&
          Object.keys(detail.project.release.commands).length > 0 && (
            <button onClick={onCommand}>运行项目命令</button>
          )}
      </div>
    </article>
  );
}
function Checks({ observation }: { observation: JsonObject }): ReactNode {
  const checks = Array.isArray(observation.checks) ? observation.checks : [];
  return (
    <div className="project-checks">
      <strong>CI 检查</strong>
      {checks.length ? (
        <ul>
          {checks.map((item, index) => {
            if (!item || typeof item !== "object" || Array.isArray(item))
              return null;
            const conclusion =
              typeof item.conclusion === "string" && item.conclusion
                ? item.conclusion.toLowerCase()
                : typeof item.status === "string"
                  ? item.status.toLowerCase()
                  : "pending";
            return (
              <li key={index}>
                <span>{String(item.name ?? "未命名检查")}</span>
                <span
                  className={
                    conclusion === "success" ? "check-success" : "muted"
                  }
                >
                  {conclusion === "success"
                    ? "通过"
                    : conclusion === "failure"
                      ? "失败"
                      : conclusion === "pending" || conclusion === "queued"
                        ? "等待结果"
                        : conclusion}
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="muted small">
          尚未观察到检查结果，不能作为 CI 通过依据。
        </p>
      )}
    </div>
  );
}
