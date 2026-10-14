import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ProjectsPage from "../ProjectsPage";
import { api, ApiError } from "../api";
import type { Agent, JsonObject } from "../types";
import type {
  Project,
  ProjectDetail,
  ProjectWorkItem,
  ProjectDelivery,
} from "../projectTypes";
import { platformState } from "./fixtures";
const sha = "a".repeat(40);
const baseSha = "b".repeat(40);
const project: Project = {
  id: "project-one",
  name: "演示项目",
  repository: "example/demo",
  github_host: "github.com",
  target_branch: "develop",
  created_at: 1,
  policy: {
    require_signed_commits: false,
    require_signed_tags: true,
    required_checks: ["build"],
    require_review: true,
    observation_interval_seconds: 60,
    allow_auto_merge: false,
    allow_release: true,
    merge_method: "squash",
    branch_prefix: "team",
  },
  release: {
    commands: { check: ["uv", "run", "pytest"] },
    verify_command: "check",
    verify_timeout_seconds: 120,
    tag_template: "v{version}",
    workflow: "release.yml",
    release_owner: "workflow",
  },
};
const agents: Agent[] = [
  {
    id: "author",
    name: "Codex",
    machine: "Linux",
    capabilities: ["task.dispatch"],
    online: true,
    revoked: false,
    metadata: {},
    created_at: 1,
    last_seen: 2,
  },
  {
    id: "reviewer",
    name: "Claude",
    machine: "Windows",
    capabilities: ["task.dispatch"],
    online: false,
    revoked: false,
    metadata: {},
    created_at: 1,
    last_seen: 2,
  },
];
const work: ProjectWorkItem = {
  id: "work-one",
  project_id: project.id,
  goal_id: "goal-one",
  title: "实现登录",
  instruction: "登录功能与测试",
  delivery_mode: "integrated",
  depends_on: [],
  acceptance_criteria: ["错误密码拒绝登录"],
  fact_versions: {},
  stale_facts: [],
  state: "planned",
  current_attempt_id: "attempt-one",
  created_at: 1,
};
const delivery: ProjectDelivery = {
  id: "delivery-one",
  work_item_id: work.id,
  attempt_id: "attempt-one",
  pr_number: 9,
  url: "https://github.com/example/demo/pull/9",
  head_sha: sha,
  base_sha: baseSha,
  state: "open",
  observation: {},
  reviews: [],
  observed_at: 3,
  merge_commit_sha: null,
};
function state() {
  return {
    ...platformState(),
    agents,
    goals: [
      {
        id: "goal-one",
        title: "登录目标",
        objective: "交付登录",
        constraints: [],
        acceptance_criteria: [],
        summary: "",
        status: "active" as const,
        conversation_id: null,
        created_at: 1,
        updated_at: 1,
      },
    ],
  };
}
function detail(): ProjectDetail {
  return {
    project: structuredClone(project),
    bindings: [
      {
        project_id: project.id,
        agent_id: "author",
        repo_path: null,
        observation: {},
        observed_at: null,
      },
    ],
    work_items: [structuredClone(work)],
    attempts: [
      {
        id: "attempt-one",
        work_item_id: work.id,
        agent_id: "author",
        number: 1,
        base_sha: baseSha,
        branch: "team/goal/work/a1",
        target_branch: "team/goal/integration",
        state: "ready",
        workdir: "/managed/work",
        task_id: null,
      },
    ],
    deliveries: [],
    commands: [],
    facts: [],
    rules: [],
    issues: [],
  };
}
function mockOperations(
  value = detail(),
  overrides: Record<string, (args: JsonObject) => unknown> = {},
) {
  return vi.spyOn(api, "operate").mockImplementation(async (name, args) => {
    if (overrides[name]) return (await overrides[name](args)) as never;
    if (name === "list_projects") return { projects: [value.project] } as never;
    if (name === "get_project") return value as never;
    if (name === "recommend_project_agents") return { candidates: [] } as never;
    throw new Error(`Unexpected operation ${name}`);
  });
}
async function opened(
  value = detail(),
  overrides: Record<string, (args: JsonObject) => unknown> = {},
) {
  const operate = mockOperations(value, overrides);
  const refresh = vi.fn();
  const user = userEvent.setup();
  render(<ProjectsPage state={state()} refresh={refresh} />);
  await screen.findByRole("heading", { name: project.name });
  return { operate, refresh, user };
}
function fill(label: string, value: string) {
  fireEvent.change(within(screen.getByRole("dialog")).getByLabelText(label), {
    target: { value },
  });
}
async function save(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", { name: "保存" }),
  );
}
beforeEach(() => history.replaceState(null, "", "#projects"));

describe("GitHub 项目管理", () => {
  it("登记仓库策略和发布校验命令，并导航到新项目", async () => {
    const { user, operate } = await opened(detail(), {
      create_project: () => ({ project }),
    });
    await user.click(screen.getByRole("button", { name: "＋ 登记项目" }));
    expect(screen.getByLabelText("项目名称")).toHaveFocus();
    fill("项目名称", "新项目");
    fill("GitHub 仓库", "owner/repo");
    fill("目标分支", "stable");
    fill("必要 CI 检查（每行一项）", "python\n\n web ");
    await user.click(screen.getByText("版本与发布设置"));
    fill("发布前校验命令名", "check");
    fill("版本与验证命令（JSON）", '{"check":["uv","run","pytest"]}');
    await user.click(
      screen.getByRole("checkbox", { name: "发布 tag 必须签名" }),
    );
    await save(user);
    expect(operate).toHaveBeenCalledWith("create_project", {
      name: "新项目",
      repository: "owner/repo",
      github_host: "github.com",
      target_branch: "stable",
      policy: {
        require_signed_commits: false,
        require_signed_tags: true,
        branch_prefix: "chiliarch",
        required_checks: ["python", "web"],
        require_review: true,
        observation_interval_seconds: 60,
        allow_auto_merge: false,
        allow_release: false,
        merge_method: "squash",
      },
      release: {
        commands: { check: ["uv", "run", "pytest"] },
        verify_command: "check",
        verify_timeout_seconds: 120,
        tag_template: "v{version}",
        workflow: null,
        release_owner: "workflow",
      },
    });
    expect(location.hash).toBe("#projects/project-one");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  it("编辑时保留签名、分支和完整发布策略", async () => {
    const { user, operate } = await opened(detail(), {
      update_project: () => ({ project }),
    });
    await user.click(screen.getByRole("button", { name: "流程策略" }));
    await user.click(screen.getByRole("button", { name: "编辑策略" }));
    fill("项目名称", "重新命名");
    await save(user);
    expect(operate).toHaveBeenCalledWith("update_project", {
      project_id: project.id,
      name: "重新命名",
      target_branch: project.target_branch,
      policy: project.policy,
      release: project.release,
    });
  });
  it("绑定受管副本时不会带入上一次已有仓库的路径", async () => {
    const { user, operate } = await opened(detail(), {
      bind_project_agent: () => ({ binding: {} }),
    });
    await user.click(screen.getByRole("button", { name: "执行节点" }));
    await user.click(screen.getByRole("button", { name: "绑定节点" }));
    await user.selectOptions(screen.getByLabelText("执行 agent"), "author");
    await user.selectOptions(screen.getByLabelText("仓库工作区"), "existing");
    fill("节点上的仓库绝对路径", "/projects/owned");
    await save(user);
    expect(operate).toHaveBeenCalledWith("bind_project_agent", {
      project_id: project.id,
      agent_id: "author",
      repo_path: "/projects/owned",
    });
    await user.click(screen.getByRole("button", { name: "绑定节点" }));
    await user.selectOptions(screen.getByLabelText("执行 agent"), "author");
    await save(user);
    expect(operate).toHaveBeenCalledWith("bind_project_agent", {
      project_id: project.id,
      agent_id: "author",
    });
  });
  it("创建有目标、代码依赖和事实版本的集成工作项", async () => {
    const value = detail();
    value.facts = [
      {
        id: "fact-one",
        key: "API",
        content: "接口 v2",
        source: "用户确认",
        version: 2,
        created_at: 1,
      },
    ];
    const { user, operate } = await opened(value, {
      create_project_work_item: () => ({ work_item: work }),
    });
    await user.click(screen.getByRole("button", { name: "新建工作项" }));
    fill("工作项名称", "编写前端");
    fill("工作说明", "使用新接口");
    await user.selectOptions(screen.getByLabelText("交付方式"), "integrated");
    expect(screen.getByLabelText("关联目标")).toBeRequired();
    await user.selectOptions(screen.getByLabelText("关联目标"), "goal-one");
    fill("验收条件（每行一条）", "测试通过\n 独立评审 ");
    await user.click(screen.getByRole("checkbox", { name: "实现登录" }));
    await user.click(screen.getByRole("checkbox", { name: "API · 版本 2" }));
    await save(user);
    expect(operate).toHaveBeenCalledWith("create_project_work_item", {
      project_id: project.id,
      title: "编写前端",
      instruction: "使用新接口",
      goal_id: "goal-one",
      delivery_mode: "integrated",
      depends_on: [work.id],
      acceptance_criteria: ["测试通过", "独立评审"],
      fact_versions: { API: 2 },
    });
  });
  it("分支下发使用完整 SHA，恢复必须明确确认进程停止", async () => {
    const { user, operate } = await opened(detail(), {
      start_project_work_item: () => ({
        command: { id: "prepare", status: "pending" },
      }),
    });
    await user.click(screen.getByRole("button", { name: "分配分支与工作区" }));
    await user.selectOptions(screen.getByLabelText("执行 agent"), "author");
    fill("精确代码基线 SHA", sha);
    fill("恢复核对记录", "旧进程停止，远端分支已核对");
    expect(
      screen.getByRole("checkbox", { name: "已确认上一次原生进程停止" }),
    ).toBeRequired();
    await user.click(
      screen.getByRole("checkbox", { name: "已确认上一次原生进程停止" }),
    );
    await save(user);
    expect(operate).toHaveBeenCalledWith("start_project_work_item", {
      work_item_id: work.id,
      agent_id: "author",
      base_sha: sha,
      recovery_note: "旧进程停止，远端分支已核对",
      previous_stopped: true,
    });
    expect(screen.getByRole("status")).toHaveTextContent("等待节点执行");
    expect(operate).toHaveBeenCalledWith("recommend_project_agents", {
      project_id: project.id,
      operating_system: null,
      capabilities: [],
      exclude_agent_ids: [],
    });
  });
  it("交付只暂存明确文件，按表单创建草稿 PR", async () => {
    const { user, operate } = await opened(detail(), {
      deliver_project_work_item: () => ({
        command: { id: "deliver", status: "pending" },
      }),
    });
    await user.click(screen.getByRole("button", { name: "提交并创建 PR" }));
    fill("提交说明", "feat: login");
    fill(
      "要暂存的文件（每行一个相对路径）",
      "src/login.py\n tests/test_login.py ",
    );
    fill("PR 说明", "登录实现与验证");
    await save(user);
    expect(operate).toHaveBeenCalledWith("deliver_project_work_item", {
      work_item_id: work.id,
      message: "feat: login",
      files: ["src/login.py", "tests/test_login.py"],
      title: work.title,
      body: "登录实现与验证",
      draft: true,
    });
  });
  it("CI 缺失不能显示通过，独立评审固定 SHA 并排除作者", async () => {
    const value = detail();
    value.deliveries = [structuredClone(delivery)];
    const { user, operate } = await opened(value, {
      review_project_delivery: () => ({ review: {} }),
    });
    expect(
      screen.getByText("尚未观察到检查结果，不能作为 CI 通过依据。"),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "记录评审" }));
    expect(
      within(screen.getByRole("dialog")).queryByRole("option", {
        name: "Codex · Linux",
      }),
    ).toBeNull();
    await user.selectOptions(screen.getByLabelText("评审者"), "reviewer");
    fill("已完成的评审任务 ID", "review-task-one");
    fill("评审依据", "独立检查测试输出与业务行为");
    fill("证据引用（JSON 数组）", '[{"uri":"https://example.org/report"}]');
    await save(user);
    expect(operate).toHaveBeenCalledWith("review_project_delivery", {
      delivery_id: delivery.id,
      reviewer_agent_id: "reviewer",
      review_task_id: "review-task-one",
      head_sha: sha,
      base_sha: baseSha,
      decision: "accepted",
      note: "独立检查测试输出与业务行为",
      evidence: [{ uri: "https://example.org/report" }],
    });
  });
  it("合并被服务端拒绝时保留表单与错误", async () => {
    const value = detail();
    value.deliveries = [structuredClone(delivery)];
    const { user, operate } = await opened(value, {
      merge_project_delivery: () => {
        throw new ApiError("Required checks are missing", 409);
      },
    });
    await user.click(screen.getByRole("button", { name: "合并交付" }));
    await user.click(
      screen.getByRole("checkbox", { name: "确认按项目规则合并这个交付" }),
    );
    await save(user);
    expect(operate).toHaveBeenCalledWith("merge_project_delivery", {
      delivery_id: delivery.id,
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Required checks are missing",
    );
    expect(screen.getByRole("dialog")).toBeVisible();
  });
  it("未知操作提供核对入口而非重新执行", async () => {
    const value = detail();
    value.commands = [
      {
        id: "unknown-one",
        project_id: project.id,
        agent_id: "author",
        attempt_id: "attempt-one",
        action: "deliver",
        arguments: {},
        status: "unknown",
        result: { stage: "push" },
        error: "connection lost",
        created_at: 3,
        updated_at: 4,
      },
    ];
    const { user, operate } = await opened(value, {
      reconcile_project_command: () => ({
        command: { id: "reconcile", status: "pending" },
      }),
    });
    await user.click(screen.getByText("提交与 PR"));
    expect(screen.getByText(/不会自动重新执行/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "核对原操作结果" }));
    expect(operate).toHaveBeenCalledWith("reconcile_project_command", {
      command_id: "unknown-one",
    });
    expect(screen.getByRole("status")).toHaveTextContent("等待节点");
  });
  it("发布绑定已合并交付与精确 commit，登记完成不当作发布成功", async () => {
    const value = detail();
    value.deliveries = [
      { ...structuredClone(delivery), state: "merged", merge_commit_sha: sha },
    ];
    const { user, operate } = await opened(value, {
      publish_project_release: () => ({
        command: { id: "publish", status: "pending" },
      }),
    });
    await user.click(screen.getByRole("button", { name: "发起发布" }));
    await user.selectOptions(screen.getByLabelText("执行 agent"), "author");
    fill("版本", "1.2.3");
    fill("发布 commit SHA", sha);
    fill("Release 标题", "Release 1.2.3");
    await user.selectOptions(screen.getByLabelText("来源交付"), delivery.id);
    await save(user);
    expect(operate).toHaveBeenCalledWith("publish_project_release", {
      project_id: project.id,
      agent_id: "author",
      version: "1.2.3",
      commit_sha: sha,
      delivery_id: delivery.id,
      release_title: "Release 1.2.3",
      release_notes: "",
    });
    expect(screen.getByRole("status")).toHaveTextContent("等待节点执行");
    expect(screen.queryByText("发布成功")).toBeNull();
  });
  it("更新最新事实使用乐观版本，冲突时保留输入", async () => {
    const value = detail();
    value.facts = [1, 2].map((version) => ({
      id: `fact-${version}`,
      key: "API",
      content: `版本 ${version} 的约定`,
      source: "用户决定",
      version,
      created_at: version,
    }));
    const { user, operate } = await opened(value, {
      set_project_fact: () => {
        throw new ApiError("Fact version changed", 409);
      },
    });
    await user.click(screen.getByRole("button", { name: "共同事实" }));
    expect(screen.queryByText("版本 1 的约定")).toBeNull();
    await user.click(screen.getByRole("button", { name: "更新事实" }));
    fill("事实内容", "新约定");
    await save(user);
    expect(operate).toHaveBeenCalledWith("set_project_fact", {
      project_id: project.id,
      key: "API",
      content: "新约定",
      source: "用户决定",
      expected_version: 2,
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Fact version changed",
    );
    expect(screen.getByLabelText("事实内容")).toHaveValue("新约定");
  });
  it("确认变更影响保留依赖并提交最新版本与依据", async () => {
    const value = detail();
    value.work_items[0].fact_versions = { API: 1 };
    value.work_items[0].stale_facts = ["API"];
    value.facts = [
      {
        id: "fact-2",
        key: "API",
        content: "新版",
        source: "用户",
        version: 2,
        created_at: 2,
      },
    ];
    const { user, operate } = await opened(value, {
      update_project_work_item_facts: () => ({ work_item: work }),
    });
    await user.click(screen.getByRole("button", { name: "确认事实新版本" }));
    fill("变更核对依据", "已根据新版改动并完成测试");
    await save(user);
    expect(operate).toHaveBeenCalledWith("update_project_work_item_facts", {
      work_item_id: work.id,
      fact_versions: { API: 2 },
      note: "已根据新版改动并完成测试",
    });
  });
  it("可为已有工作补充事实依赖，同时不能移除原有依赖", async () => {
    const value = detail();
    value.work_items[0].fact_versions = { API: 1 };
    value.facts = ["API", "设计"].map((key) => ({
      id: key,
      key,
      content: "已确认的项目约定",
      source: "用户",
      version: 1,
      created_at: 1,
    }));
    const { user, operate } = await opened(value, {
      update_project_work_item_facts: () => ({ work_item: work }),
    });
    await user.click(screen.getByRole("button", { name: "更新事实依赖" }));
    expect(
      screen.getByRole("checkbox", { name: /API · 版本 1/ }),
    ).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: /API · 版本 1/ }),
    ).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /设计 · 版本 1/ }));
    fill("变更核对依据", "已停止任务并确认设计约定适用");
    await save(user);
    expect(operate).toHaveBeenCalledWith("update_project_work_item_facts", {
      work_item_id: work.id,
      fact_versions: { API: 1, 设计: 1 },
      note: "已停止任务并确认设计约定适用",
    });
  });
  it("事件规则提交明确允许来源、过滤条件与模板", async () => {
    const { user, operate } = await opened(detail(), {
      configure_project_rule: () => ({ rule: {} }),
    });
    await user.click(screen.getByRole("button", { name: "流程策略" }));
    await user.click(screen.getByRole("button", { name: "添加规则" }));
    fill("GitHub 事件类型", "IssueCommentEvent");
    fill("允许的 GitHub 用户（每行一个）", "maintainer\n reviewer ");
    fill("评论指令前缀", "/chiliarch");
    fill("目标说明模板", "处理 {title}：{body}");
    await save(user);
    expect(operate).toHaveBeenCalledWith("configure_project_rule", {
      project_id: project.id,
      rule_id: null,
      event_type: "IssueCommentEvent",
      allowed_actors: ["maintainer", "reviewer"],
      label: null,
      command: "/chiliarch",
      objective_template: "处理 {title}：{body}",
      enabled: true,
    });
  });
  it("切换项目忽略迟到的旧详情", async () => {
    const first = detail();
    const second = detail();
    second.project = { ...project, id: "project-two", name: "第二项目" };
    second.work_items = [];
    let releaseFirst!: (value: ProjectDetail) => void;
    const firstPromise = new Promise<ProjectDetail>((resolve) => {
      releaseFirst = resolve;
    });
    mockOperations(first, {
      list_projects: () => ({ projects: [project, second.project] }),
      get_project: (args) =>
        args.project_id === project.id ? firstPromise : second,
    });
    render(<ProjectsPage state={state()} refresh={vi.fn()} />);
    await userEvent
      .setup()
      .click(await screen.findByRole("button", { name: /第二项目/ }));
    await screen.findByRole("heading", { name: "第二项目" });
    await act(async () => releaseFirst(first));
    expect(screen.queryByRole("heading", { name: project.name })).toBeNull();
  });
  it("按需读取时间线，并将缺失用量明确标为未知", async () => {
    const { user, operate } = await opened(detail(), {
      get_project_insights: () => ({
        timeline: [
          {
            kind: "attempt",
            at: 2,
            branch: "team/work/a1",
            task_id: "task-one",
          },
        ],
        blockers: [],
        usage: {
          totals: { input_tokens: 125 },
          attempts: [],
          missing_usage_count: 1,
          source: "native_reported_not_billing",
        },
      }),
    });
    expect(operate).not.toHaveBeenCalledWith(
      "get_project_insights",
      expect.anything(),
    );
    await user.click(screen.getByText("协作时间线与用量"));
    expect(await screen.findByText(/1 次执行尚无用量报告/)).toBeVisible();
    expect(screen.getByText("125")).toBeVisible();
  });
  it("刷新列表成功不清除详情错误", async () => {
    const operate = mockOperations(detail(), {
      get_project: () => {
        throw new ApiError("项目读取受阻", 500);
      },
    });
    render(<ProjectsPage state={state()} refresh={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("项目读取受阻");
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "刷新项目" }));
    await waitFor(() =>
      expect(
        operate.mock.calls.filter(([name]) => name === "get_project"),
      ).toHaveLength(2),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("项目读取受阻");
  });
  it("独立评审任务固定交付与代码版本，不直接伪造通过记录", async () => {
    const value = detail();
    value.deliveries = [structuredClone(delivery)];
    value.bindings.push({ ...value.bindings[0], agent_id: "reviewer" });
    const { user, operate } = await opened(value, {
      dispatch_task: () => ({ task: { id: "review-task-one" } }),
    });
    await user.click(screen.getByRole("button", { name: "安排评审任务" }));
    await user.selectOptions(
      screen.getByLabelText("独立评审 agent"),
      "reviewer",
    );
    fill("评审任务说明", "独立检查并保留证据");
    fill("评审工作目录（可选）", "C:\\review-workspace");
    await save(user);
    expect(operate).toHaveBeenCalledWith("dispatch_task", {
      agent_id: "reviewer",
      instruction: "独立检查并保留证据",
      parameters: {
        project_review: {
          delivery_id: delivery.id,
          head_sha: sha,
          base_sha: baseSha,
        },
      },
      workdir: "C:\\review-workspace",
    });
    expect(operate).not.toHaveBeenCalledWith(
      "review_project_delivery",
      expect.anything(),
    );
  });
  it("创建整体交付总 PR 保留目标、集成版本和草稿意图", async () => {
    const { user, operate } = await opened(detail(), {
      finalize_project_goal: () => ({
        command: { id: "final", status: "pending" },
      }),
    });
    await user.click(screen.getByRole("button", { name: "创建目标总 PR" }));
    await user.selectOptions(screen.getByLabelText("执行 agent"), "author");
    await user.selectOptions(screen.getByLabelText("集成目标"), "goal-one");
    fill("集成分支 Head SHA", sha);
    fill("总 PR 标题", "交付登录功能");
    fill("总 PR 说明", "前后端集成与验证");
    await save(user);
    expect(operate).toHaveBeenCalledWith("finalize_project_goal", {
      project_id: project.id,
      goal_id: "goal-one",
      agent_id: "author",
      head_sha: sha,
      title: "交付登录功能",
      body: "前后端集成与验证",
      draft: true,
    });
  });
  it("核对发布只读取指定 tag 和版本，不触发重推", async () => {
    const { user, operate } = await opened(detail(), {
      inspect_project_release: () => ({
        command: { id: "inspect", status: "pending" },
      }),
    });
    await user.click(screen.getByRole("button", { name: "核对发布结果" }));
    await user.selectOptions(screen.getByLabelText("执行 agent"), "author");
    fill("发布 tag", "v1.2.3");
    fill("预期 commit SHA", sha);
    await save(user);
    expect(operate).toHaveBeenCalledWith("inspect_project_release", {
      project_id: project.id,
      agent_id: "author",
      tag: "v1.2.3",
      commit_sha: sha,
    });
    expect(operate).not.toHaveBeenCalledWith(
      "publish_project_release",
      expect.anything(),
    );
  });
  it("项目命令只传登记名称和工作项，不下发任意 shell", async () => {
    const { user, operate } = await opened(detail(), {
      run_project_command: () => ({
        command: { id: "run", status: "pending" },
      }),
    });
    await user.click(screen.getByRole("button", { name: "运行项目命令" }));
    await save(user);
    expect(operate).toHaveBeenCalledWith("run_project_command", {
      work_item_id: work.id,
      command_name: "check",
    });
  });
  it("导入 Issue 只使用绑定节点并等待真实观察结果", async () => {
    const { user, operate } = await opened(detail(), {
      import_project_issue: () => ({
        command: { id: "import", status: "pending" },
      }),
    });
    await user.click(screen.getByRole("button", { name: "Issues" }));
    await user.click(screen.getByRole("button", { name: "查看 / 导入 Issue" }));
    expect(screen.queryByRole("option", { name: /Claude/ })).toBeNull();
    await user.selectOptions(screen.getByLabelText("执行 agent"), "author");
    fill("Issue 编号", "42");
    await user.selectOptions(screen.getByLabelText("操作"), "import");
    await save(user);
    expect(operate).toHaveBeenCalledWith("import_project_issue", {
      project_id: project.id,
      agent_id: "author",
      issue_number: 42,
    });
    expect(screen.getByRole("status")).toHaveTextContent("等待节点执行");
    expect(screen.queryByRole("link", { name: "查看关联目标 ↗" })).toBeNull();
  });
  it("已观察 Issue 显示关联目标并发布用户填写的评论", async () => {
    const value = detail();
    value.issues = [
      {
        id: "issue-one",
        number: 42,
        goal_id: "goal-one",
        observed_at: 3,
        observation: {
          title: "修复登录",
          url: "https://github.com/example/demo/issues/42",
          body: "问题详情",
          state: "OPEN",
          labels: [{ name: "bug" }],
        },
      },
    ];
    const { user, operate } = await opened(value, {
      comment_project_issue: () => ({
        command: { id: "comment", status: "pending" },
      }),
    });
    await user.click(screen.getByRole("button", { name: "Issues" }));
    expect(
      screen.getByRole("link", { name: "查看关联目标 ↗" }),
    ).toHaveAttribute("href", "#goals/goal-one");
    await user.click(screen.getByRole("button", { name: "添加进展评论" }));
    await user.selectOptions(screen.getByLabelText("执行 agent"), "author");
    fill("进展评论", "已复现。\n下一步补回归测试。");
    await save(user);
    expect(operate).toHaveBeenCalledWith("comment_project_issue", {
      project_id: project.id,
      agent_id: "author",
      issue_number: 42,
      body: "已复现。\n下一步补回归测试。",
    });
  });
  it("修改 Issue 状态保留标签，仅明确勾选时允许清空标签", async () => {
    const value = detail();
    value.issues = [
      {
        id: "issue-one",
        number: 42,
        goal_id: null,
        observed_at: 3,
        observation: { title: "修复登录", state: "OPEN", labels: ["bug"] },
      },
    ];
    const { user, operate } = await opened(value, {
      update_project_issue: () => ({
        command: { id: "update", status: "pending" },
      }),
    });
    await user.click(screen.getByRole("button", { name: "Issues" }));
    await user.click(
      screen.getByRole("button", { name: "修改 Issue 状态 / 标签" }),
    );
    await user.selectOptions(screen.getByLabelText("执行 agent"), "author");
    await user.selectOptions(screen.getByLabelText("Issue 状态"), "closed");
    await save(user);
    expect(operate).toHaveBeenCalledWith("update_project_issue", {
      project_id: project.id,
      agent_id: "author",
      issue_number: 42,
      state: "closed",
    });
    await user.click(
      screen.getByRole("button", { name: "修改 Issue 状态 / 标签" }),
    );
    await user.selectOptions(screen.getByLabelText("执行 agent"), "author");
    await user.click(screen.getByRole("checkbox", { name: "替换全部标签" }));
    fill("完整标签列表（每行一个）", "");
    await save(user);
    expect(operate).toHaveBeenCalledWith("update_project_issue", {
      project_id: project.id,
      agent_id: "author",
      issue_number: 42,
      state: null,
      labels: [],
    });
  });
  it("清理工作区需确认，服务端拒绝时保留错误和当前工作项", async () => {
    const { user, operate } = await opened(detail(), {
      cleanup_project_workspace: () => {
        throw new ApiError("工作区包含未提交更改", 409);
      },
    });
    await user.click(screen.getByRole("button", { name: "清理本机工作区" }));
    await save(user);
    expect(operate).not.toHaveBeenCalledWith(
      "cleanup_project_workspace",
      expect.anything(),
    );
    await user.click(
      screen.getByRole("checkbox", { name: "确认清理已完成交付的本机工作区" }),
    );
    await save(user);
    expect(operate).toHaveBeenCalledWith("cleanup_project_workspace", {
      work_item_id: work.id,
    });
    expect(await screen.findByText("工作区包含未提交更改")).toBeVisible();
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "实现登录" }),
    ).toBeInTheDocument();
  });
  it("危险 PR 地址只显示编号，过期评审不显示为当前结果", async () => {
    const value = detail();
    value.deliveries = [
      {
        ...structuredClone(delivery),
        url: "javascript:alert(1)",
        reviews: [
          {
            id: "review-one",
            reviewer: "reviewer",
            head_sha: baseSha,
            base_sha: baseSha,
            decision: "accepted",
            note: "旧提交通过",
            evidence: [],
            current: false,
          },
        ],
      },
    ];
    await opened(value);
    expect(screen.queryByRole("link", { name: "PR #9 ↗" })).toBeNull();
    expect(screen.getByText("评审通过 · 已过期")).toBeVisible();
  });
});
