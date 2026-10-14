import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import { ProjectCandidates } from "./ProjectInsights";
import { ErrorNotice, JsonView } from "./components";
import { errorText, latestFacts, lineValues } from "./projectUtils";
import type { JsonObject, PlatformState } from "./types";
import type { ProjectAction, ProjectDetail } from "./projectTypes";

const boundAgentActions = new Set<ProjectAction["kind"]>([
  "start",
  "release",
  "inspect_release",
  "finalize",
  "poll",
  "issue_read",
  "issue_comment",
  "issue_update",
]);

export default function ProjectDialog({
  action,
  state,
  detail,
  onClose,
  onSaved,
}: {
  action: ProjectAction;
  state: PlatformState;
  detail: ProjectDetail | null;
  onClose: () => void;
  onSaved: (result: JsonObject) => void;
}): ReactNode {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bindMode, setBindMode] = useState("managed");
  const [deliveryMode, setDeliveryMode] = useState("separate");
  const [reviewerId, setReviewerId] = useState("");
  const project = action.kind === "create" ? null : action.project;
  const title = {
    create: "登记项目",
    issue_read: "查看或导入 Issue",
    issue_comment: "添加 Issue 进展评论",
    issue_update: "更新 Issue",
    cleanup: "清理受管工作区",
    edit: "编辑项目策略",
    bind: "绑定执行节点",
    work: "新建工作项",
    start: "分配分支与工作区",
    deliver: "提交并创建 PR",
    review: "评审交付",
    dispatch_review: "安排独立评审任务",
    merge: "合并交付",
    release: "发起发布",
    inspect_release: "核对发布结果",
    finalize: "创建目标总 PR",
    poll: "检查 GitHub 事件",
    fact: "登记共同事实",
    rule: "事件规则",
    command: "运行项目命令",
    work_facts: "确认事实新版本",
  }[action.kind];
  const choosesAgent =
    action.kind === "bind" || boundAgentActions.has(action.kind);
  const agents = state.agents.filter(
    (agent) =>
      !agent.revoked &&
      (!boundAgentActions.has(action.kind) ||
        detail?.bindings.some((binding) => binding.agent_id === agent.id)),
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
      let name: string;
      let args: JsonObject;
      if (action.kind === "create" || action.kind === "edit") {
        const commands: unknown = JSON.parse(field("commands") || "{}");
        if (
          !commands ||
          typeof commands !== "object" ||
          Array.isArray(commands) ||
          !Object.values(commands).every(
            (command) =>
              Array.isArray(command) &&
              command.length > 0 &&
              command.every((part: unknown) => typeof part === "string"),
          )
        )
          throw new Error("命令需要填写 JSON 对象，每个值为非空的参数数组。");
        name = action.kind === "create" ? "create_project" : "update_project";
        args = {
          name: field("name"),
          target_branch: field("target_branch"),
          policy: {
            ...(project?.policy ?? {}),
            observation_interval_seconds: Number(field("observation_interval")),
            require_signed_commits: data.has("signed"),
            require_signed_tags: data.has("signed_tags"),
            branch_prefix: field("branch_prefix"),
            required_checks: lineValues(data.get("checks")),
            require_review: data.has("review"),
            allow_auto_merge: data.has("auto_merge"),
            allow_release: data.has("release"),
            merge_method: field("merge_method"),
          },
          release: {
            ...(project?.release ?? {}),
            verify_timeout_seconds: Number(field("verify_timeout")),
            commands: commands as JsonObject,
            tag_template: field("tag_template"),
            workflow: field("workflow") || null,
            verify_command: field("verify_command") || null,
            release_owner: field("release_owner"),
          },
          ...(action.kind === "create"
            ? {
                repository: field("repository"),
                github_host: field("github_host"),
              }
            : { project_id: action.project.id }),
        };
      } else if (action.kind === "issue_read") {
        name =
          field("issue_intent") === "import"
            ? "import_project_issue"
            : "inspect_project_issue";
        args = {
          project_id: action.project.id,
          agent_id: field("agent"),
          issue_number: Number(field("issue_number")),
        };
      } else if (action.kind === "issue_comment") {
        name = "comment_project_issue";
        args = {
          project_id: action.project.id,
          agent_id: field("agent"),
          issue_number: action.issue.number,
          body: field("body"),
        };
      } else if (action.kind === "issue_update") {
        name = "update_project_issue";
        args = {
          project_id: action.project.id,
          agent_id: field("agent"),
          issue_number: action.issue.number,
          state: field("issue_state") || null,
          ...(data.has("replace_labels")
            ? { labels: lineValues(data.get("issue_labels")) }
            : {}),
        };
      } else if (action.kind === "cleanup") {
        name = "cleanup_project_workspace";
        args = { work_item_id: action.work.id };
      } else if (action.kind === "bind") {
        name = "bind_project_agent";
        args = {
          project_id: action.project.id,
          agent_id: field("agent"),
          ...(bindMode === "existing" ? { repo_path: field("repo_path") } : {}),
        };
      } else if (action.kind === "work") {
        name = "create_project_work_item";
        args = {
          project_id: action.project.id,
          title: field("title"),
          instruction: field("instruction"),
          goal_id: field("goal") || null,
          delivery_mode: field("delivery_mode"),
          depends_on: data.getAll("dependency").map(String),
          acceptance_criteria: lineValues(data.get("criteria")),
          fact_versions: Object.fromEntries(
            data.getAll("fact").map((key) => {
              const fact = latestFacts(detail?.facts ?? []).find(
                (item) => item.key === key,
              )!;
              return [fact.key, fact.version];
            }),
          ),
        };
      } else if (action.kind === "start") {
        name = "start_project_work_item";
        args = {
          work_item_id: action.work.id,
          agent_id: field("agent"),
          base_sha: field("base_sha").toLowerCase(),
          recovery_note: field("recovery_note"),
          previous_stopped: data.has("previous_stopped"),
        };
      } else if (action.kind === "deliver") {
        name = "deliver_project_work_item";
        args = {
          work_item_id: action.work.id,
          message: field("message"),
          title: field("title"),
          body: field("body"),
          draft: data.has("draft"),
          files: lineValues(data.get("files")).length
            ? lineValues(data.get("files"))
            : null,
        };
      } else if (action.kind === "review") {
        name = "review_project_delivery";
        const evidence: unknown = JSON.parse(field("evidence") || "[]");
        if (
          !Array.isArray(evidence) ||
          !evidence.every(
            (item: unknown) =>
              item !== null && typeof item === "object" && !Array.isArray(item),
          )
        )
          throw new Error("评审证据应为 JSON 对象数组。");
        args = {
          delivery_id: action.delivery.id,
          reviewer_agent_id: field("reviewer") || null,
          review_task_id: field("review_task_id") || null,
          head_sha: action.delivery.head_sha,
          base_sha: action.delivery.base_sha,
          decision: field("decision"),
          note: field("note"),
          evidence: evidence as JsonObject[],
        };
      } else if (action.kind === "dispatch_review") {
        name = "dispatch_task";
        args = {
          agent_id: field("reviewer"),
          instruction: field("instruction"),
          parameters: {
            project_review: {
              delivery_id: action.delivery.id,
              head_sha: action.delivery.head_sha,
              base_sha: action.delivery.base_sha,
            },
          },
          ...(field("workdir") ? { workdir: field("workdir") } : {}),
        };
      } else if (action.kind === "merge") {
        name = "merge_project_delivery";
        args = { delivery_id: action.delivery.id };
      } else if (action.kind === "release") {
        name = "publish_project_release";
        args = {
          project_id: action.project.id,
          agent_id: field("agent"),
          version: field("version"),
          commit_sha: field("commit_sha").toLowerCase(),
          delivery_id: field("delivery"),
          release_title: field("title"),
          release_notes: field("body"),
        };
      } else if (action.kind === "inspect_release") {
        name = "inspect_project_release";
        args = {
          project_id: action.project.id,
          agent_id: field("agent"),
          tag: field("tag"),
          commit_sha: field("commit_sha").toLowerCase(),
        };
      } else if (action.kind === "finalize") {
        name = "finalize_project_goal";
        args = {
          project_id: action.project.id,
          goal_id: field("goal"),
          agent_id: field("agent"),
          head_sha: field("head_sha").toLowerCase(),
          title: field("title"),
          body: field("body"),
          draft: data.has("draft"),
        };
      } else if (action.kind === "fact") {
        name = "set_project_fact";
        args = {
          project_id: action.project.id,
          key: action.fact?.key ?? field("key"),
          content: field("content"),
          source: field("source"),
          expected_version: action.fact?.version ?? 0,
        };
      } else if (action.kind === "rule") {
        name = "configure_project_rule";
        args = {
          project_id: action.project.id,
          rule_id: action.rule?.id ?? null,
          event_type: field("event_type"),
          allowed_actors: lineValues(data.get("actors")),
          label: field("label") || null,
          command: field("rule_command") || null,
          objective_template: field("objective"),
          enabled: data.has("enabled"),
        };
      } else if (action.kind === "poll") {
        name = "poll_project_events";
        args = { project_id: action.project.id, agent_id: field("agent") };
      } else if (action.kind === "work_facts") {
        name = "update_project_work_item_facts";
        args = {
          work_item_id: action.work.id,
          note: field("note"),
          fact_versions: Object.fromEntries(
            [
              ...new Set([
                ...Object.keys(action.work.fact_versions),
                ...data.getAll("fact_dependency").map(String),
              ]),
            ].map((key) => [
              key,
              latestFacts(detail?.facts ?? []).find((fact) => fact.key === key)
                ?.version ?? action.work.fact_versions[key],
            ]),
          ),
        };
      } else if (action.kind === "command") {
        name = "run_project_command";
        args = {
          work_item_id: action.work.id,
          command_name: field("command_name"),
        };
      } else {
        throw new Error("不支持的操作。");
      }

      onSaved(await api.operate(name, args));
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="agent-pairing-dialog project-dialog"
      aria-labelledby="project-dialog-title"
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onClose();
      }}
    >
      <div className="agent-dialog-header">
        <h2 id="project-dialog-title">{title}</h2>
        <button
          type="button"
          className="plain"
          aria-label="关闭"
          disabled={busy}
          onClick={onClose}
        >
          ×
        </button>
      </div>
      <form className="project-form" onSubmit={(event) => void submit(event)}>
        {(action.kind === "create" || action.kind === "edit") && (
          <>
            <label>
              项目名称
              <input name="name" required defaultValue={project?.name} />
            </label>
            {action.kind === "create" && (
              <div className="project-form-grid">
                <label>
                  GitHub 仓库
                  <input
                    name="repository"
                    placeholder="owner/repository"
                    required
                    pattern="[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
                  />
                </label>
                <label>
                  GitHub 主机
                  <input
                    name="github_host"
                    defaultValue="github.com"
                    required
                  />
                </label>
              </div>
            )}
            <div className="project-form-grid">
              <label>
                目标分支
                <input
                  name="target_branch"
                  required
                  defaultValue={project?.target_branch}
                  placeholder="仓库实际的交付分支"
                />
              </label>
              <label>
                合并方式
                <select
                  name="merge_method"
                  defaultValue={project?.policy.merge_method ?? "squash"}
                >
                  <option value="squash">Squash</option>
                  <option value="merge">Merge</option>
                  <option value="rebase">Rebase</option>
                </select>
              </label>
            </div>
            <label>
              分支前缀
              <input
                name="branch_prefix"
                required
                defaultValue={project?.policy.branch_prefix ?? "chiliarch"}
              />
            </label>
            <label>
              自动观察间隔（秒）
              <input
                name="observation_interval"
                type="number"
                min={0}
                step="any"
                required
                defaultValue={
                  project?.policy.observation_interval_seconds ?? 60
                }
              />
              <small className="muted">
                0 表示关闭自动观察；节点在线时按此间隔核对仓库变化。
              </small>
            </label>
            <fieldset>
              <legend>交付要求</legend>
              <label className="project-check">
                <input
                  type="checkbox"
                  name="signed"
                  defaultChecked={project?.policy.require_signed_commits}
                />
                必须使用已配置的 Git 签名
              </label>
              <label className="project-check">
                <input
                  type="checkbox"
                  name="review"
                  defaultChecked={project?.policy.require_review ?? true}
                />
                需要独立 agent 评审
              </label>
              <label className="project-check">
                <input
                  type="checkbox"
                  name="auto_merge"
                  defaultChecked={project?.policy.allow_auto_merge}
                />
                auto 模式满足规则后允许合并
              </label>
            </fieldset>
            <label>
              必要 CI 检查（每行一项）
              <textarea
                name="checks"
                rows={3}
                defaultValue={project?.policy.required_checks.join("\n")}
                placeholder="填写 GitHub 上的检查名称"
              />
            </label>
            <details className="project-release-settings">
              <summary>版本与发布设置</summary>
              <label className="project-check">
                <input
                  type="checkbox"
                  name="signed_tags"
                  defaultChecked={project?.policy.require_signed_tags}
                />
                发布 tag 必须签名
              </label>
              <label className="project-check">
                <input
                  type="checkbox"
                  name="release"
                  defaultChecked={project?.policy.allow_release}
                />
                允许独立发起发布流程
              </label>
              <div className="project-form-grid">
                <label>
                  Tag 格式
                  <input
                    name="tag_template"
                    defaultValue={project?.release.tag_template ?? "v{version}"}
                    required
                  />
                </label>
                <label>
                  发布工作流
                  <input
                    name="workflow"
                    defaultValue={project?.release.workflow ?? ""}
                    placeholder="例如 release.yml"
                  />
                </label>
              </div>
              <label>
                GitHub Release 创建者
                <select
                  name="release_owner"
                  defaultValue={project?.release.release_owner ?? "workflow"}
                >
                  <option value="workflow">GitHub 工作流</option>
                  <option value="daemon">守护进程</option>
                </select>
              </label>
              <label>
                发布校验超时（秒）
                <input
                  name="verify_timeout"
                  type="number"
                  min={1}
                  max={86400}
                  required
                  defaultValue={project?.release.verify_timeout_seconds ?? 120}
                />
              </label>
              <label>
                发布前校验命令名
                <input
                  name="verify_command"
                  defaultValue={project?.release.verify_command ?? ""}
                  placeholder="下方已登记的命令名"
                />
              </label>
              <label>
                版本与验证命令（JSON）
                <textarea
                  name="commands"
                  rows={5}
                  defaultValue={JSON.stringify(
                    project?.release.commands ?? {},
                    null,
                    2,
                  )}
                  spellCheck={false}
                />
              </label>
              <p className="muted small">
                按名称登记参数数组，例如 {`{"check": ["uv", "run", "pytest"]}`}
                。命令在受管工作区执行，不经过 shell。发布前校验命令可使用{" "}
                {"{version}"}，需验证仓库版本、变更日志及发布条件。
              </p>
            </details>
          </>
        )}
        {choosesAgent && (
          <>
            <label>
              执行 agent
              <select name="agent" defaultValue="" required>
                <option value="" disabled>
                  选择节点上的 agent
                </option>
                {agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name} · {agent.machine}
                    {agent.online ? "" : "（离线）"}
                  </option>
                ))}
              </select>
            </label>
            {!agents.length && (
              <p className="notice">
                {boundAgentActions.has(action.kind)
                  ? "请先在执行节点页面绑定 agent。"
                  : "请先在 Agents 管理中配对 agent。"}
              </p>
            )}
          </>
        )}
        {action.kind === "start" && (
          <ProjectCandidates projectId={action.project.id} />
        )}
        {action.kind === "bind" && (
          <>
            <label>
              仓库工作区
              <select
                value={bindMode}
                onChange={(event) => setBindMode(event.target.value)}
              >
                <option value="managed">自动准备专用副本和 worktree</option>
                <option value="existing">显式绑定已有仓库</option>
              </select>
            </label>
            {bindMode === "existing" && (
              <label>
                节点上的仓库绝对路径
                <input
                  name="repo_path"
                  required
                  placeholder="填写该机器上的实际仓库路径"
                />
              </label>
            )}
            <p className="muted small">
              沿用节点已有的 Git / gh
              登录及签名设置。绑定后请运行一次仓库能力检查。
            </p>
          </>
        )}
        {action.kind === "work" && (
          <>
            <label>
              工作项名称
              <input name="title" required />
            </label>
            <label>
              工作说明
              <textarea name="instruction" rows={4} required />
            </label>
            <label>
              关联目标
              <select
                name="goal"
                defaultValue=""
                required={deliveryMode === "integrated"}
              >
                <option value="">
                  {deliveryMode === "integrated" ? "请选择目标" : "不关联目标"}
                </option>
                {state.goals?.map((goal) => (
                  <option value={goal.id} key={goal.id}>
                    {goal.title}
                  </option>
                ))}
              </select>
            </label>
            <label>
              交付方式
              <select
                name="delivery_mode"
                value={deliveryMode}
                onChange={(event) => setDeliveryMode(event.target.value)}
              >
                <option value="separate">每个任务单独 PR 到目标分支</option>
                <option value="integrated">
                  先汇入目标集成分支，再整体交付
                </option>
              </select>
            </label>
            <label>
              验收条件（每行一条）
              <textarea name="criteria" rows={3} />
            </label>
            {Boolean(detail?.work_items.length) && (
              <fieldset>
                <legend>代码依赖</legend>
                {detail?.work_items.map((work) => (
                  <label className="project-check" key={work.id}>
                    <input type="checkbox" name="dependency" value={work.id} />
                    {work.title}
                  </label>
                ))}
              </fieldset>
            )}
          </>
        )}
        {action.kind === "start" && (
          <>
            <p className="muted small">
              为「{action.work.title}」创建执行尝试；节点准备成功后才开始 agent
              任务。
            </p>
            <label>
              精确代码基线 SHA
              <input
                name="base_sha"
                required
                pattern="[a-fA-F0-9]{40,64}"
                minLength={40}
                maxLength={64}
                placeholder="完整的 commit SHA"
              />
            </label>
            <p className="muted small">
              使用仓库检查得到的提交。代码依赖必须已经包含在这个版本中；结果未知的旧尝试需要先核对。
            </p>
            {action.work.current_attempt_id && (
              <>
                <label className="project-check">
                  <input name="previous_stopped" type="checkbox" required />
                  已确认上一次原生进程停止
                </label>
                <label>
                  恢复核对记录
                  <textarea
                    name="recovery_note"
                    required
                    rows={3}
                    placeholder="记录已确认的分支、提交和外部操作结果"
                  />
                </label>
              </>
            )}
          </>
        )}
        {action.kind === "work" &&
          latestFacts(detail?.facts ?? []).length > 0 && (
            <fieldset>
              <legend>依赖的共同事实</legend>
              {latestFacts(detail?.facts ?? []).map((fact) => (
                <label className="project-check" key={fact.key}>
                  <input type="checkbox" name="fact" value={fact.key} />
                  {fact.key} · 版本 {fact.version}
                </label>
              ))}
            </fieldset>
          )}
        {action.kind === "deliver" && (
          <>
            <p className="muted small">
              代码任务停止后，在该尝试的受管工作区提交和创建 PR。
            </p>
            <label>
              提交说明
              <input name="message" required />
            </label>
            <label>
              要暂存的文件（每行一个相对路径）
              <textarea
                name="files"
                rows={3}
                placeholder="留空只提交已经暂存的修改"
              />
            </label>
            <label>
              PR 标题
              <input name="title" defaultValue={action.work.title} required />
            </label>
            <label>
              PR 说明
              <textarea name="body" rows={4} />
            </label>
            <label className="project-check">
              <input type="checkbox" name="draft" defaultChecked />
              创建为草稿 PR
            </label>
          </>
        )}
        {action.kind === "review" && (
          <>
            <p className="muted small">
              评审绑定当前观察到的 Head /
              Base；提交变化后原评审会失效。这是平台评审，会同步成 GitHub 评论。
            </p>
            <dl className="project-shas">
              <dt>Head</dt>
              <dd>
                <code>{action.delivery.head_sha}</code>
              </dd>
              <dt>Base</dt>
              <dd>
                <code>{action.delivery.base_sha}</code>
              </dd>
            </dl>
            <label>
              评审者
              <select
                name="reviewer"
                value={reviewerId}
                onChange={(event) => setReviewerId(event.target.value)}
              >
                <option value="">我（用户）</option>
                {state.agents
                  .filter(
                    (agent) =>
                      !agent.revoked &&
                      agent.id !==
                        detail?.attempts.find(
                          (attempt) =>
                            attempt.id === action.delivery.attempt_id,
                        )?.agent_id,
                  )
                  .map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name} · {agent.machine}
                    </option>
                  ))}
              </select>
            </label>
            {reviewerId && (
              <>
                <label>
                  已完成的评审任务 ID
                  <input
                    key={reviewerId}
                    name="review_task_id"
                    required
                    list="completed-review-tasks"
                    placeholder="选择或填写该 agent 的真实评审任务"
                  />
                  <datalist id="completed-review-tasks">
                    {state.tasks
                      .filter(
                        (task) =>
                          task.agent_id === reviewerId &&
                          task.status === "completed",
                      )
                      .map((task) => (
                        <option key={task.id} value={task.id}>
                          {task.instruction}
                        </option>
                      ))}
                  </datalist>
                </label>
                <p className="muted small">
                  任务必须已经完成，并记录相同 Head / Base 的 project_review
                  证据。不能代录其他 agent 未实际完成的评审。
                </p>
              </>
            )}
            <label>
              结论
              <select name="decision" defaultValue="accepted">
                <option value="accepted">验收通过</option>
                <option value="changes_requested">需要修改</option>
              </select>
            </label>
            <label>
              评审依据
              <textarea name="note" required rows={4} />
            </label>
            <label>
              证据引用（JSON 数组）
              <textarea name="evidence" rows={3} defaultValue="[]" />
            </label>
            <p className="muted small">
              例如{" "}
              {`[{"name":"测试报告","uri":"https://example.org/report","head_sha":"…"}]`}
              。填写实际检查结果及其版本。
            </p>
          </>
        )}
        {action.kind === "dispatch_review" && (
          <>
            <label>
              独立评审 agent
              <select name="reviewer" required defaultValue="">
                <option value="" disabled>
                  选择已绑定的其他 agent
                </option>
                {state.agents
                  .filter(
                    (agent) =>
                      !agent.revoked &&
                      detail?.bindings.some(
                        (binding) => binding.agent_id === agent.id,
                      ) &&
                      agent.id !==
                        detail?.attempts.find(
                          (attempt) =>
                            attempt.id === action.delivery.attempt_id,
                        )?.agent_id,
                  )
                  .map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name} · {agent.machine}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              评审任务说明
              <textarea
                name="instruction"
                required
                rows={6}
                defaultValue={`独立评审 ${action.project.repository} 的 PR #${action.delivery.pr_number}。在独立工作区检查 head ${action.delivery.head_sha}，基线为 ${action.delivery.base_sha}。检查实际修改、运行必要验证，记录发现与证据；不要直接修改或合并被评审分支。最终报告明确记录发现、验证结果及相同的 Head / Base。任务结束后由总管使用本次任务 ID 作为 review_task_id 记录评审。`}
              />
            </label>
            <label>
              评审工作目录（可选）
              <input name="workdir" placeholder="节点上的独立评审目录" />
            </label>
            <p className="muted small">
              评审任务参数绑定交付和精确代码版本。任务完成后才能记录为该 agent
              的独立评审。
            </p>
          </>
        )}
        {action.kind === "merge" && (
          <>
            <p>将 PR #{action.delivery.pr_number} 合入其目标分支。</p>
            <p className="muted small">
              执行前重新核对 Head、Base、评审、必要 CI 和共同事实，遵守 GitHub
              仓库规则。
            </p>
            <dl className="project-shas">
              <dt>Head</dt>
              <dd>
                <code>{action.delivery.head_sha}</code>
              </dd>
              <dt>Base</dt>
              <dd>
                <code>{action.delivery.base_sha}</code>
              </dd>
            </dl>
            <label className="project-check">
              <input type="checkbox" required />
              确认按项目规则合并这个交付
            </label>
          </>
        )}
        {action.kind === "release" && (
          <>
            <p className="muted small">
              发起发布会推送项目配置的 tag。随后仍需核对工作流和实际发布结果。
            </p>
            <label>
              版本
              <input name="version" required placeholder="填写项目版本" />
            </label>
            <label>
              来源交付
              <select name="delivery" required defaultValue="">
                <option value="" disabled>
                  选择已经合并的交付
                </option>
                {detail?.deliveries
                  .filter(
                    (delivery) => delivery.state.toLowerCase() === "merged",
                  )
                  .map((delivery) => (
                    <option key={delivery.id} value={delivery.id}>
                      PR #{delivery.pr_number} · {delivery.merge_commit_sha}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              发布 commit SHA
              <input name="commit_sha" required pattern="[a-fA-F0-9]{40,64}" />
            </label>
            <label>
              Release 标题
              <input name="title" />
            </label>
            <label>
              Release 说明
              <textarea name="body" rows={4} />
            </label>
          </>
        )}
        {action.kind === "inspect_release" && (
          <>
            <label>
              发布 tag
              <input name="tag" required />
            </label>
            <label>
              预期 commit SHA
              <input name="commit_sha" required pattern="[a-fA-F0-9]{40,64}" />
            </label>
            <p className="muted small">
              仅读取 tag、工作流与 Release 的实际状态，不创建或重推 tag。
            </p>
          </>
        )}
        {action.kind === "finalize" && (
          <>
            <p className="muted small">
              全部子工作项合并到目标集成分支后，创建一个面向项目目标分支的总
              PR；最终组合仍需独立评审与 CI。
            </p>
            <label>
              集成目标
              <select name="goal" required defaultValue="">
                <option value="" disabled>
                  选择目标
                </option>
                {state.goals
                  ?.filter((goal) =>
                    detail?.work_items.some(
                      (work) =>
                        work.goal_id === goal.id &&
                        work.delivery_mode === "integrated",
                    ),
                  )
                  .map((goal) => (
                    <option key={goal.id} value={goal.id}>
                      {goal.title}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              集成分支 Head SHA
              <input name="head_sha" required pattern="[a-fA-F0-9]{40,64}" />
            </label>
            <label>
              总 PR 标题
              <input name="title" required />
            </label>
            <label>
              总 PR 说明
              <textarea name="body" rows={4} />
            </label>
            <label className="project-check">
              <input type="checkbox" name="draft" defaultChecked />
              创建为草稿 PR
            </label>
          </>
        )}
        {action.kind === "fact" && (
          <>
            <label>
              事实名称
              <input
                name="key"
                required
                defaultValue={action.fact?.key}
                readOnly={Boolean(action.fact)}
              />
            </label>
            <label>
              事实内容
              <textarea
                name="content"
                required
                rows={5}
                defaultValue={action.fact?.content}
              />
            </label>
            <label>
              来源 / 决策依据
              <input
                name="source"
                required
                defaultValue={action.fact?.source}
                placeholder="文档地址、issue 或用户确认记录"
              />
            </label>
            {action.fact && (
              <p className="muted small">
                基于版本 {action.fact.version}{" "}
                更新。其他人已修改时会拒绝覆盖，依赖旧版本的工作会收到变化提示。
              </p>
            )}
          </>
        )}
        {action.kind === "rule" && (
          <>
            <label>
              GitHub 事件类型
              <input
                name="event_type"
                required
                defaultValue={action.rule?.event_type}
                list="github-event-types"
                placeholder="例如 IssuesEvent"
              />
              <datalist id="github-event-types">
                <option value="IssuesEvent" />
                <option value="IssueCommentEvent" />
                <option value="PullRequestEvent" />
                <option value="PushEvent" />
              </datalist>
            </label>
            <label>
              允许的 GitHub 用户（每行一个）
              <textarea
                name="actors"
                required
                rows={3}
                defaultValue={action.rule?.allowed_actors.join("\n")}
              />
            </label>
            <label>
              必须包含的标签
              <input name="label" defaultValue={action.rule?.label ?? ""} />
            </label>
            <label>
              评论指令前缀
              <input
                name="rule_command"
                defaultValue={action.rule?.command ?? ""}
              />
            </label>
            <label>
              目标说明模板
              <textarea
                name="objective"
                required
                rows={4}
                defaultValue={action.rule?.objective_template}
              />
            </label>
            <p className="muted small">
              可使用 {"{title}"}、{"{body}"} 和 {"{url}"}{" "}
              引用事件资料。只会创建目标，事件内容不是管理员指令。
            </p>
            <label className="project-check">
              <input
                type="checkbox"
                name="enabled"
                defaultChecked={action.rule?.enabled ?? true}
              />
              启用规则
            </label>
          </>
        )}
        {action.kind === "poll" && (
          <p className="muted small">
            让选定节点读取仓库事件。只处理符合已启用规则的来源与条件，重复事件自动去重。
          </p>
        )}
        {action.kind === "issue_read" && (
          <>
            <label>
              Issue 编号
              <input
                name="issue_number"
                type="number"
                min={1}
                step={1}
                required
              />
            </label>
            <label>
              操作
              <select name="issue_intent" defaultValue="inspect">
                <option value="inspect">只读取当前状态</option>
                <option value="import">导入为 Chiliarch 目标</option>
              </select>
            </label>
            <p className="muted small">
              导入复用已经关联的目标，不会重复创建。Issue
              正文作为工作资料，不改变平台权限。
            </p>
          </>
        )}
        {action.kind === "issue_comment" && (
          <>
            <p className="muted small">
              在 GitHub Issue #{action.issue.number} 发布一条进展评论。
            </p>
            <label>
              进展评论
              <textarea name="body" required rows={5} />
            </label>
          </>
        )}
        {action.kind === "issue_update" && (
          <>
            <p className="muted small">
              更新 GitHub Issue #{action.issue.number}。
            </p>
            <label>
              Issue 状态
              <select name="issue_state" defaultValue="">
                <option value="">保持当前状态</option>
                <option value="open">打开</option>
                <option value="closed">关闭</option>
              </select>
            </label>
            <label className="project-check">
              <input name="replace_labels" type="checkbox" />
              替换全部标签
            </label>
            <label>
              完整标签列表（每行一个）
              <textarea
                name="issue_labels"
                rows={3}
                defaultValue={
                  Array.isArray(action.issue.observation.labels)
                    ? action.issue.observation.labels
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
                        .join("\n")
                    : ""
                }
              />
            </label>
            <p className="muted small">
              未勾选时保留原标签；勾选并留空将移除全部标签。
            </p>
          </>
        )}
        {action.kind === "cleanup" && (
          <>
            <p>清理「{action.work.title}」当前尝试的本机受管 worktree。</p>
            <p className="muted small">
              守护进程先核对任务已经停止、目录无未提交内容且远端已保存交付；不会删除用户绑定的仓库或远端分支。
            </p>
            <label className="project-check">
              <input type="checkbox" required />
              确认清理已完成交付的本机工作区
            </label>
          </>
        )}
        {action.kind === "work_facts" && (
          <>
            <p className="muted small">
              确认任务已经停止，并根据以下最新约定核对工作。旧评审会失效，需要重新验收。
            </p>
            <fieldset>
              <legend>依赖的事实版本</legend>
              {latestFacts(detail?.facts ?? []).map((fact) => (
                <label className="project-check" key={fact.key}>
                  <input
                    type="checkbox"
                    name="fact_dependency"
                    value={fact.key}
                    defaultChecked={Object.hasOwn(
                      action.work.fact_versions,
                      fact.key,
                    )}
                    disabled={Object.hasOwn(
                      action.work.fact_versions,
                      fact.key,
                    )}
                  />
                  {fact.key} · 版本 {fact.version}
                  {Object.hasOwn(action.work.fact_versions, fact.key)
                    ? `（原版本 ${action.work.fact_versions[fact.key]}，保留依赖）`
                    : "（新增依赖）"}
                </label>
              ))}
            </fieldset>
            <label>
              变更核对依据
              <textarea name="note" required rows={4} />
            </label>
          </>
        )}
        {action.kind === "command" && (
          <>
            <label>
              已登记命令
              <select name="command_name" required>
                {Object.keys(action.project.release.commands).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <p className="muted small">
              在工作项已经停止的受管工作区运行。执行结果会留在记录中，不自动代表交付验收通过。
            </p>
            <JsonView value={action.project.release.commands} />
          </>
        )}
        <ErrorNotice error={error} />
        <div className="actions goal-dialog-actions">
          <button type="button" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            className="primary"
            disabled={busy || (choosesAgent && !agents.length)}
          >
            {busy ? "正在提交…" : "保存"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
