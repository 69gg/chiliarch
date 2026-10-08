import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import GoalsPage from "../GoalsPage";
import { api, ApiError } from "../api";
import type { Agent, AgentRequest, Goal, Task, TaskLink } from "../types";
import { platformState } from "./fixtures";

const goal: Goal = {
  id: "goal-one",
  title: "交付登录功能",
  objective: "实现并评审登录功能",
  constraints: ["不修改生产配置"],
  acceptance_criteria: ["通过行为测试"],
  status: "active",
  conversation_id: "chat-one",
  summary: "等待评审",
  created_at: 1759800000,
  updated_at: 1759800045,
};
const agent: Agent = {
  id: "agent-one",
  name: "Codex",
  machine: "dev-machine",
  capabilities: ["task.dispatch"],
  revoked: false,
  online: true,
  created_at: 1759800000,
  last_seen: 1759800045,
  metadata: { execution_mode: "managed", state: "idle" },
};
const task: Task = {
  id: "task-one",
  agent_id: agent.id,
  instruction: "实现登录",
  parameters: {},
  status: "completed",
  progress: 1,
  result: { commit: "abc123" },
  error: null,
  created_at: 1759800000,
  updated_at: 1759800045,
  requested_by: "user",
};
const link: TaskLink = {
  task_id: task.id,
  goal_id: goal.id,
  conversation_id: goal.conversation_id,
  depends_on: [],
  acceptance_criteria: ["密码错误时拒绝登录"],
  review_status: "pending",
  review_note: "",
  artifacts: [],
  retry_of: null,
};

function stateWithGoal() {
  return {
    ...platformState(),
    goals: [{ ...goal }],
    agents: [{ ...agent }],
    tasks: [{ ...task }],
    task_links: [{ ...link }],
  };
}

beforeEach(() => history.replaceState(null, "", "#goals"));

describe("目标管理", () => {
  it("创建目标时提交逐行约束与验收标准，并导航到新目标", async () => {
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ goal: { ...goal } });
    const refresh = vi.fn();
    const user = userEvent.setup();
    render(<GoalsPage state={platformState()} refresh={refresh} />);
    await user.click(screen.getByRole("button", { name: "＋ 新建目标" }));
    const dialog = screen.getByRole("dialog", { name: "新建目标" });
    expect(within(dialog).getByLabelText("目标名称")).toHaveFocus();
    await user.type(
      within(dialog).getByLabelText("目标名称"),
      "  交付登录功能  ",
    );
    await user.type(
      within(dialog).getByLabelText("目标说明"),
      "实现并评审登录功能",
    );
    await user.type(
      within(dialog).getByLabelText("约束（每行一条）"),
      "不修改生产配置\n\n  独立工作区 ",
    );
    await user.type(
      within(dialog).getByLabelText("验收条件（每行一条）"),
      "通过行为测试",
    );
    await user.click(within(dialog).getByRole("button", { name: "保存" }));
    expect(operate).toHaveBeenCalledWith("create_goal", {
      title: goal.title,
      objective: goal.objective,
      constraints: ["不修改生产配置", "独立工作区"],
      acceptance_criteria: ["通过行为测试"],
    });
    expect(refresh).toHaveBeenCalledOnce();
    expect(location.hash).toBe("#goals/goal-one");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("展示依赖、执行人与安全产物引用，并派发带依赖的下一任务", async () => {
    const state = stateWithGoal();
    state.tasks.push({
      ...task,
      id: "task-two",
      instruction: "评审登录",
      status: "blocked",
      result: null,
    });
    state.task_links.push({
      ...link,
      task_id: "task-two",
      depends_on: [task.id],
      artifacts: [
        {
          name: "评审 PR",
          uri: "https://github.com/example/repo/pull/1",
          kind: "pull_request",
        },
        { name: "本机报告", uri: "file:///tmp/report.txt", kind: "report" },
        { name: "危险地址", uri: "javascript:alert(1)", kind: "reference" },
      ],
    });
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ task: { id: "task-three" } });
    const user = userEvent.setup();
    render(<GoalsPage state={state} refresh={vi.fn()} />);
    const review = screen.getByRole("article", { name: "任务 评审登录" });
    expect(review).toHaveTextContent("受阻");
    expect(review).toHaveTextContent("Codex · dev-machine");
    expect(
      within(review).getByRole("link", { name: "实现登录 · 待验收" }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "评审 PR ↗" })).toHaveAttribute(
      "href",
      "https://github.com/example/repo/pull/1",
    );
    expect(
      screen.queryByRole("link", { name: /本机报告|危险地址/ }),
    ).toBeNull();
    expect(screen.getByText("file:///tmp/report.txt")).toBeVisible();
    expect(
      screen.getByRole("link", { name: "查看协调会话 ↗" }),
    ).toHaveAttribute("href", "#ai/chat-one");
    await user.click(screen.getByRole("button", { name: "派发任务" }));
    const dialog = screen.getByRole("dialog", { name: "派发目标任务" });
    await user.selectOptions(
      within(dialog).getByLabelText("执行 agent"),
      agent.id,
    );
    await user.type(within(dialog).getByLabelText("任务说明"), "发布前测试");
    await user.click(
      within(dialog).getByRole("checkbox", { name: "评审登录" }),
    );
    await user.type(
      within(dialog).getByLabelText("任务验收条件（每行一条）"),
      "测试无失败",
    );
    await user.click(within(dialog).getByRole("button", { name: "确认派发" }));
    expect(operate).toHaveBeenCalledWith("dispatch_task", {
      goal_id: goal.id,
      agent_id: agent.id,
      instruction: "发布前测试",
      parameters: {},
      depends_on: ["task-two"],
      acceptance_criteria: ["测试无失败"],
    });
  });

  it("验收保留既有产物，新增引用与依据交给统一操作服务", async () => {
    const state = stateWithGoal();
    state.task_links[0] = {
      ...link,
      artifacts: [
        { name: "提交", uri: "git:abc123", kind: "commit", digest: "abc123" },
      ],
    };
    const operate = vi.spyOn(api, "operate").mockResolvedValue({});
    const user = userEvent.setup();
    render(<GoalsPage state={state} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "验收 / 返工" }));
    const dialog = screen.getByRole("dialog", { name: "验收任务" });
    await user.type(
      within(dialog).getByLabelText("验收依据 / 返工原因"),
      "已验证密码错误与成功路径",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "添加产物引用" }),
    );
    await user.type(within(dialog).getByLabelText("产物 2 名称"), "测试报告");
    await user.type(
      within(dialog).getByLabelText("产物 2 地址"),
      "https://example.com/report",
    );
    await user.click(within(dialog).getByRole("button", { name: "保存" }));
    expect(operate).toHaveBeenCalledWith("review_task", {
      task_id: task.id,
      decision: "accepted",
      note: "已验证密码错误与成功路径",
      artifacts: [
        { name: "提交", uri: "git:abc123", kind: "commit", digest: "abc123" },
        {
          name: "测试报告",
          uri: "https://example.com/report",
          kind: "reference",
        },
      ],
    });
  });

  it("返工和核查重试分开执行，重试必须附核查说明且保留服务端错误", async () => {
    const state = stateWithGoal();
    const operate = vi.spyOn(api, "operate").mockResolvedValue({});
    const user = userEvent.setup();
    const view = render(<GoalsPage state={state} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "验收 / 返工" }));
    await user.selectOptions(
      screen.getByLabelText("验收决定"),
      "changes_requested",
    );
    await user.type(
      screen.getByLabelText("验收依据 / 返工原因"),
      "缺少错误密码测试",
    );
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(operate).toHaveBeenLastCalledWith("review_task", {
      task_id: task.id,
      decision: "changes_requested",
      note: "缺少错误密码测试",
      artifacts: [],
    });
    state.task_links = [
      {
        ...link,
        review_status: "changes_requested",
        review_note: "缺少错误密码测试",
      },
    ];
    view.rerender(<GoalsPage state={state} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "核查后重试" }));
    const dialog = screen.getByRole("dialog", { name: "核查后重试" });
    await user.click(within(dialog).getByRole("button", { name: "确认重试" }));
    expect(operate).toHaveBeenCalledTimes(1);
    await user.type(
      within(dialog).getByLabelText("核查说明"),
      "已核对提交，登录功能已实现，仅补测试",
    );
    await user.type(
      within(dialog).getByLabelText("新的任务说明（可选）"),
      "补上错误密码测试",
    );
    operate.mockRejectedValueOnce(new ApiError("任务已被新尝试替代", 409));
    await user.click(within(dialog).getByRole("button", { name: "确认重试" }));
    expect(operate).toHaveBeenLastCalledWith("retry_task", {
      task_id: task.id,
      recovery_note: "已核对提交，登录功能已实现，仅补测试",
      instruction: "补上错误密码测试",
    });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "任务已被新尝试替代",
    );
    expect(dialog).toBeVisible();
    expect(within(dialog).getByLabelText("核查说明")).toHaveValue(
      "已核对提交，登录功能已实现，仅补测试",
    );
  });

  it("目标完成受服务端验收守卫约束，失败时不关闭表单或假报成功", async () => {
    const operate = vi
      .spyOn(api, "operate")
      .mockRejectedValue(new ApiError("所有任务需要完成验收", 409));
    const refresh = vi.fn();
    const user = userEvent.setup();
    render(<GoalsPage state={stateWithGoal()} refresh={refresh} />);
    await user.click(screen.getByRole("button", { name: "更新目标状态" }));
    await user.selectOptions(screen.getByLabelText("目标状态"), "completed");
    await user.clear(screen.getByLabelText("进展与结论"));
    await user.type(screen.getByLabelText("进展与结论"), "功能已交付");
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(operate).toHaveBeenCalledWith("update_goal", {
      goal_id: goal.id,
      status: "completed",
      summary: "功能已交付",
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "所有任务需要完成验收",
    );
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("目标深链接切换详情，重试链中的旧尝试不再提供重试", async () => {
    const state = stateWithGoal();
    state.goals.push({ ...goal, id: "goal-two", title: "第二个目标" });
    state.tasks.push({
      ...task,
      id: "task-retry",
      status: "queued",
      instruction: "新的执行尝试",
    });
    state.task_links.push({
      ...link,
      task_id: "task-retry",
      retry_of: task.id,
    });
    state.tasks[0].status = "interrupted";
    const user = userEvent.setup();
    render(<GoalsPage state={state} refresh={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "核查后重试" })).toBeNull();
    expect(screen.getByText("已由新尝试接续：task-retry")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /第二个目标/ }));
    expect(
      screen.getByRole("region", { name: "目标 第二个目标" }),
    ).toBeVisible();
    act(() => {
      history.replaceState(null, "", "#goals/goal-one");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(
      screen.getByRole("region", { name: "目标 交付登录功能" }),
    ).toBeVisible();
  });
});

describe("继续协调", () => {
  it("重新激活受阻目标以唤醒总管，失败显示错误且允许重试", async () => {
    const state = stateWithGoal();
    state.goals = [{ ...goal, status: "blocked" }];
    const operate = vi
      .spyOn(api, "operate")
      .mockRejectedValueOnce(new ApiError("总管暂不可用", 503))
      .mockResolvedValue({});
    const refresh = vi.fn();
    const user = userEvent.setup();
    render(<GoalsPage state={state} refresh={refresh} />);
    await user.click(screen.getByRole("button", { name: "继续协调" }));
    expect(operate).toHaveBeenCalledWith("update_goal", {
      goal_id: goal.id,
      status: "active",
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("总管暂不可用");
    expect(refresh).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "继续协调" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "已请求 Chiliarch AI 继续协调。",
    );
    expect(refresh).toHaveBeenCalledOnce();
  });
});

describe("目标查找与处理提示", () => {
  it("下级派工请求及包裹审批未处理完时，不误报目标可以结束", () => {
    const state = stateWithGoal();
    state.task_links[0].review_status = "accepted";
    state.requests = [
      {
        id: "request-one",
        agent_id: agent.id,
        external_id: "external-one",
        operation: "dispatch_task",
        arguments: {
          goal_id: goal.id,
          agent_id: agent.id,
          instruction: "补充验证",
        },
        message: "申请补充验证",
        status: "pending",
        result: null,
        created_at: 1759800046,
        resolved_at: null,
      },
    ];
    const view = render(<GoalsPage state={state} refresh={vi.fn()} />);
    expect(screen.getByRole("region", { name: "需要处理" })).toHaveTextContent(
      "待处理请求 1",
    );
    expect(
      screen.getByRole("region", { name: "需要处理" }),
    ).not.toHaveTextContent("待结束目标");
    state.approvals = [
      {
        id: "approval-one",
        operation: "resolve_agent_request",
        arguments: { request_id: "request-one", decision: "approve" },
        requested_by: "supervisor",
        status: "pending",
        reason: "ask 模式",
        created_at: 1759800047,
        resolved_at: null,
        result: null,
      },
    ];
    view.rerender(<GoalsPage state={state} refresh={vi.fn()} />);
    expect(screen.getByRole("region", { name: "需要处理" })).toHaveTextContent(
      "等待许可 1",
    );
    expect(
      screen.getByRole("region", { name: "需要处理" }),
    ).not.toHaveTextContent(/待结束目标|待处理请求/);
    state.requests[0] = {
      ...state.requests[0],
      status: "rejected",
      resolved_at: 1759800048,
    };
    view.rerender(<GoalsPage state={state} refresh={vi.fn()} />);
    expect(screen.getByRole("region", { name: "需要处理" })).toHaveTextContent(
      "等待许可 1",
    );
    expect(
      screen.getByRole("region", { name: "需要处理" }),
    ).not.toHaveTextContent("待结束目标");
    state.approvals[0] = {
      ...state.approvals[0],
      status: "rejected",
      resolved_at: 1759800049,
    };
    view.rerender(<GoalsPage state={state} refresh={vi.fn()} />);
    expect(screen.getByRole("region", { name: "需要处理" })).toHaveTextContent(
      "待结束目标 1",
    );
  });

  it("旧尝试上的下级请求仍关联原目标，其他目标请求不污染处理状态", () => {
    const state = stateWithGoal();
    state.tasks[0].status = "failed";
    state.tasks.push({ ...task, id: "accepted-retry" });
    state.task_links.push({
      ...link,
      task_id: "accepted-retry",
      retry_of: task.id,
      review_status: "accepted",
    });
    const request: AgentRequest = {
      id: "retry-request",
      agent_id: agent.id,
      external_id: "external-retry",
      operation: "retry_task",
      arguments: { task_id: task.id, recovery_note: "已核查" },
      message: "申请接续",
      status: "pending",
      result: null,
      created_at: 1759800046,
      resolved_at: null,
    };
    state.requests = [request];
    const view = render(<GoalsPage state={state} refresh={vi.fn()} />);
    expect(screen.getByRole("region", { name: "需要处理" })).toHaveTextContent(
      "待处理请求 1",
    );
    expect(
      screen.getByRole("region", { name: "需要处理" }),
    ).not.toHaveTextContent("待结束目标");
    state.requests = [
      {
        ...request,
        operation: "dispatch_task",
        arguments: { goal_id: "other-goal" },
      },
    ];
    view.rerender(<GoalsPage state={state} refresh={vi.fn()} />);
    expect(screen.getByRole("region", { name: "需要处理" })).toHaveTextContent(
      "待结束目标 1",
    );
    expect(
      screen.getByRole("region", { name: "需要处理" }),
    ).not.toHaveTextContent("待处理请求");
  });

  it("筛选切换目标后不会将前一个目标的协调错误显示给新目标", async () => {
    const state = stateWithGoal();
    state.goals.push({ ...goal, id: "second", title: "部署服务" });
    vi.spyOn(api, "operate").mockRejectedValue(
      new ApiError("首个目标协调失败", 409),
    );
    const user = userEvent.setup();
    render(<GoalsPage state={state} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "继续协调" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "首个目标协调失败",
    );
    await user.type(
      screen.getByRole("searchbox", { name: "搜索目标" }),
      "部署服务",
    );
    expect(screen.getByRole("region", { name: "目标 部署服务" })).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("按任务内容搜索并组合状态筛选，无匹配时能清除筛选恢复目标", async () => {
    const state = stateWithGoal();
    state.goals.push(
      { ...goal, id: "goal-done", title: "已交付支付", status: "completed" },
      { ...goal, id: "goal-paused", title: "部署服务", status: "blocked" },
    );
    state.tasks[0].instruction = "验证过期会话";
    const user = userEvent.setup();
    render(<GoalsPage state={state} refresh={vi.fn()} />);
    const list = screen.getByRole("navigation", { name: "目标列表" });
    expect(within(list).getAllByRole("button")).toHaveLength(3);
    await user.type(
      screen.getByRole("searchbox", { name: "搜索目标" }),
      "过期会话",
    );
    expect(within(list).getAllByRole("button")).toHaveLength(1);
    expect(
      screen.getByRole("region", { name: "目标 交付登录功能" }),
    ).toBeVisible();
    await user.selectOptions(screen.getByLabelText("筛选状态"), "completed");
    expect(within(list).queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByText("没有匹配目标")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(within(list).getAllByRole("button")).toHaveLength(3);
    await user.selectOptions(screen.getByLabelText("筛选状态"), "blocked");
    expect(within(list).getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("region", { name: "目标 部署服务" })).toBeVisible();
  });

  it("待处理只统计当前尝试，历史失败和已验收任务的离线 agent 不制造告警", async () => {
    const state = stateWithGoal();
    state.goals.push({ ...goal, id: "goal-running", title: "运行中的目标" });
    state.agents.push({ ...agent, id: "offline", online: false });
    state.tasks[0].status = "failed";
    state.tasks.push(
      { ...task, id: "replacement", agent_id: "offline" },
      { ...task, id: "dependent", instruction: "依赖评审", status: "blocked" },
      { ...task, id: "running", status: "running" },
    );
    state.task_links.push(
      {
        ...link,
        task_id: "replacement",
        retry_of: task.id,
        review_status: "pending",
      },
      { ...link, task_id: "dependent", depends_on: [task.id] },
      { ...link, task_id: "running", goal_id: "goal-running" },
    );
    const user = userEvent.setup();
    const view = render(<GoalsPage state={state} refresh={vi.fn()} />);
    const attention = screen.getByRole("region", { name: "需要处理" });
    expect(attention).toHaveTextContent("待验收 1 · 等待依赖 1");
    expect(attention).not.toHaveTextContent(/待核查|agent 离线/);
    await user.click(screen.getByRole("checkbox", { name: "仅显示需要处理" }));
    const list = screen.getByRole("navigation", { name: "目标列表" });
    expect(
      within(list).queryByRole("button", { name: /运行中的目标/ }),
    ).toBeNull();
    expect(within(list).getAllByRole("button")).toHaveLength(1);
    state.task_links[1].review_status = "accepted";
    state.tasks[2].status = "queued";
    view.rerender(<GoalsPage state={state} refresh={vi.fn()} />);
    expect(within(list).queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByText("没有匹配目标")).toBeVisible();
  });

  it("取消尚未确认时显示真实阻碍，不开放重试入口", () => {
    const state = stateWithGoal();
    state.tasks[0].status = "cancellation_requested";
    render(<GoalsPage state={state} refresh={vi.fn()} />);
    expect(screen.getByRole("region", { name: "需要处理" })).toHaveTextContent(
      "取消待确认 1",
    );
    expect(screen.getByLabelText("任务处理提示")).toHaveTextContent(
      "尚未确认执行已停止",
    );
    expect(screen.queryByRole("button", { name: "核查后重试" })).toBeNull();
  });

  it("目标已取消也保留尚未确认停止的任务提示，不提示历史失败需要重试", () => {
    const state = stateWithGoal();
    state.goals[0].status = "cancelled";
    state.tasks[0].status = "cancellation_requested";
    state.tasks.push({ ...task, id: "old-failure", status: "failed" });
    state.task_links.push({ ...link, task_id: "old-failure" });
    render(<GoalsPage state={state} refresh={vi.fn()} />);
    expect(screen.getByRole("region", { name: "需要处理" })).toHaveTextContent(
      "取消待确认 1",
    );
    expect(
      screen.getByRole("region", { name: "需要处理" }),
    ).not.toHaveTextContent("待核查");
    expect(screen.queryByRole("button", { name: "核查后重试" })).toBeNull();
  });

  it("前置任务未验收且后续 agent 离线时，两种阻碍都可见", () => {
    const state = stateWithGoal();
    state.agents[0].online = false;
    state.tasks.push({
      ...task,
      id: "waiting",
      status: "blocked",
      instruction: "后续任务",
    });
    state.task_links.push({
      ...link,
      task_id: "waiting",
      depends_on: [task.id],
    });
    render(<GoalsPage state={state} refresh={vi.fn()} />);
    const article = screen.getByRole("article", { name: "任务 后续任务" });
    expect(within(article).getByLabelText("任务处理提示")).toHaveTextContent(
      "等待 1 项前置任务完成并通过验收",
    );
    expect(article).toHaveTextContent("离线不表示执行已经停止");
    expect(screen.getByRole("region", { name: "需要处理" })).toHaveTextContent(
      "agent 离线 1",
    );
  });
});

describe("任务交接", () => {
  it("原身份吊销后可选择其他 agent 接续，交接失败仍保留选择和说明", async () => {
    const state = stateWithGoal();
    state.tasks[0].status = "failed";
    state.agents[0].revoked = true;
    state.agents.push(
      { ...agent, id: "reviewer", name: "Claude", machine: "review-machine" },
      { ...agent, id: "revoked", name: "已吊销执行器", revoked: true },
    );
    const operate = vi
      .spyOn(api, "operate")
      .mockRejectedValue(new ApiError("原任务已有新尝试", 409));
    const user = userEvent.setup();
    render(<GoalsPage state={state} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "核查后重试" }));
    const dialog = screen.getByRole("dialog", { name: "核查后重试" });
    const selector = within(dialog).getByRole("combobox", {
      name: "接续执行的 agent",
    });
    expect(
      within(selector).getByRole("option", { name: /原 agent 已不可用/ }),
    ).toBeDisabled();
    expect(
      within(selector).queryByRole("option", { name: /已吊销执行器/ }),
    ).toBeNull();
    await user.selectOptions(selector, "reviewer");
    expect(dialog).toHaveTextContent("本机文件不会自动同步");
    await user.type(
      within(dialog).getByLabelText("核查说明"),
      "原执行已停止，提交已推送，接手后先核对提交",
    );
    await user.click(within(dialog).getByRole("button", { name: "确认重试" }));
    expect(operate).toHaveBeenCalledWith("retry_task", {
      task_id: task.id,
      agent_id: "reviewer",
      recovery_note: "原执行已停止，提交已推送，接手后先核对提交",
    });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "原任务已有新尝试",
    );
    expect(selector).toHaveValue("reviewer");
    expect(within(dialog).getByLabelText("核查说明")).toHaveValue(
      "原执行已停止，提交已推送，接手后先核对提交",
    );
  });
});
