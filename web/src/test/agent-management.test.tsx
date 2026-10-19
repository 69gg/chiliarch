import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import AgentsPage from "../AgentsPage";
import { api, ApiError } from "../api";
import type { Agent, AgentRequest, Approval, Task } from "../types";
import { platformState } from "./fixtures";

function agent(overrides: Partial<Agent> = {}): Agent {
  return {
    id: "agent-one",
    name: "开发机",
    machine: "workstation-one",
    capabilities: ["task.dispatch", "task.cancel"],
    revoked: false,
    online: true,
    created_at: 1759800000,
    last_seen: 1759800045,
    transport: "sse",
    metadata: {},
    ...overrides,
  };
}

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-one",
    agent_id: "agent-one",
    instruction: "校验发布产物",
    parameters: {},
    status: "running",
    progress: 0.4,
    result: null,
    error: null,
    message: "正在校验 wheel",
    created_at: 1759800000,
    updated_at: 1759800045,
    requested_by: "user",
    ...overrides,
  };
}

function request(overrides: Partial<AgentRequest> = {}): AgentRequest {
  return {
    id: "request-one",
    agent_id: "agent-one",
    external_id: "external-one",
    operation: "dispatch_task",
    arguments: { agent_id: "agent-two", instruction: "协助校验" },
    message: "需要另一台机器协作",
    status: "pending",
    result: null,
    created_at: 1759800000,
    resolved_at: null,
    ...overrides,
  };
}

function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "approval-one",
    operation: "cancel_task",
    arguments: { task_id: "task-one" },
    requested_by: "supervisor",
    status: "pending",
    reason: "总管请求取消任务",
    created_at: 1759800000,
    resolved_at: null,
    result: null,
    ...overrides,
  };
}

describe("Agents 管理的机器列表与详情", () => {
  it("把自行声明的介绍显示为纯文本，不解释 HTML", async () => {
    const state = platformState();
    const introduction = '<img src=x onerror="alert(1)"> 我是本机助手。';
    state.agents = [agent({ introduction, metadata: { introduction } })];
    const user = userEvent.setup();
    const { container } = render(
      <AgentsPage state={state} refresh={vi.fn()} />,
    );
    await user.click(
      screen.getByRole("button", { name: "查看 开发机，机器 workstation-one" }),
    );
    expect(screen.getByText("Agent 自述（自行声明）")).toBeVisible();
    expect(screen.getByText(introduction)).toBeVisible();
    expect(container.querySelector(".agent-introduction img")).toBeNull();
    expect(screen.queryByText("agent 上报信息")).toBeNull();
  });

  it("按真实任务、请求与审批计数，进入独立详情并在返回后恢复卡片焦点", async () => {
    const state = platformState();
    state.agents = [
      agent(),
      agent({
        id: "agent-two",
        name: "备用机",
        machine: "workstation-two",
        online: false,
        transport: "poll",
      }),
    ];
    state.tasks = [
      task(),
      task({ id: "queued-task", instruction: "等待处理", status: "queued" }),
      task({
        id: "other-task",
        agent_id: "agent-two",
        instruction: "备用机器的任务",
      }),
    ];
    state.requests = [
      request(),
      request({ id: "resolved-request", status: "approved" }),
    ];
    state.approvals = [
      approval(),
      approval({
        id: "other-approval",
        arguments: { agent_id: "agent-two" },
        reason: "备用机的许可",
      }),
      approval({ id: "resolved-approval", status: "approved" }),
    ];
    const user = userEvent.setup();
    render(<AgentsPage state={state} refresh={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Agents 管理" })).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "任务说明" })).toBeNull();
    const first = screen.getByRole("button", {
      name: "查看 开发机，机器 workstation-one",
    });
    expect(within(first).getByText("SSE")).toBeVisible();
    expect(within(first).getByText("1 待执行")).toBeVisible();
    expect(
      within(first).getByLabelText("2 项待处理：1 个请求，1 个审批"),
    ).toHaveTextContent("2");
    const second = screen.getByRole("button", {
      name: "查看 备用机，机器 workstation-two",
    });
    expect(within(second).getByText("上次传输")).toBeVisible();
    expect(within(second).getByText("长轮询")).toBeVisible();
    expect(
      within(second).getByLabelText("1 项待处理：0 个请求，1 个审批"),
    ).toHaveTextContent("1");

    await user.click(first);
    expect(screen.getByRole("heading", { name: "开发机" })).toHaveFocus();
    const cockpit = screen.getByRole("region", { name: "开发机 驾驶舱" });
    expect(cockpit).toHaveTextContent("task.dispatch");
    expect(cockpit).toHaveTextContent("workstation-one");
    expect(cockpit).toHaveTextContent("正在校验 wheel");
    expect(
      within(cockpit).getByRole("progressbar", {
        name: "校验发布产物执行进度",
      }),
    ).toHaveAttribute("value", "0.4");
    expect(screen.queryByText("备用机器的任务")).toBeNull();
    expect(screen.queryByText("备用机的许可")).toBeNull();
    expect(screen.queryByText("agent 上报信息")).toBeNull();

    await user.click(
      screen.getByRole("button", { name: "← 返回 Agents 管理" }),
    );
    expect(
      screen.getByRole("button", {
        name: "查看 开发机，机器 workstation-one",
      }),
    ).toHaveFocus();
    expect(screen.queryByRole("region", { name: "开发机 驾驶舱" })).toBeNull();
  });

  it("待处理数随服务端快照变化，额外机器信息只显示实际声明的 metadata", async () => {
    const state = platformState();
    state.agents = [
      agent({ metadata: { cpu_percent: 23, host_label: "本机" } }),
    ];
    state.requests = [request()];
    const refresh = vi.fn();
    const user = userEvent.setup();
    const view = render(<AgentsPage state={state} refresh={refresh} />);
    expect(
      screen.getByLabelText("1 项待处理：1 个请求，0 个审批"),
    ).toBeVisible();
    view.rerender(
      <AgentsPage
        state={{
          ...state,
          requests: [{ ...state.requests[0], status: "rejected" }],
        }}
        refresh={refresh}
      />,
    );
    expect(
      screen.queryByLabelText("1 项待处理：1 个请求，0 个审批"),
    ).toBeNull();
    expect(screen.getByText("没有待处理事项")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /查看 开发机/ }));
    await user.click(screen.getByText("agent 上报信息"));
    expect(screen.getByText(/"cpu_percent": 23/)).toBeVisible();
    expect(screen.getByText(/"host_label": "本机"/)).toBeVisible();
    expect(screen.queryByText(/内存使用率/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "刷新 agents" }));
    expect(refresh).toHaveBeenCalledOnce();
  });
});

describe("添加 agent 的配对对话框", () => {
  it("打开不生成注册码，默认有效期交给服务端，复制真实命令并用 ESC 恢复焦点", async () => {
    const state = platformState();
    state.config.server = { public_url: "http://platform.example:8765" };
    const operate = vi.spyOn(api, "operate").mockResolvedValue({
      code: "one-time-code",
      expires_at: 1759800900,
    });
    const refresh = vi.fn();
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(<AgentsPage state={state} refresh={refresh} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    const add = screen.getByRole("button", { name: "＋ 添加 agent" });
    await user.click(add);
    const dialog = screen.getByRole("dialog", { name: "添加 agent" });
    expect(dialog).toBeVisible();
    const ttl = within(dialog).getByLabelText("注册码有效期");
    expect(ttl).toHaveFocus();
    expect(ttl).toHaveValue(null);
    expect(ttl).toHaveAttribute("placeholder", "使用服务端默认");
    expect(operate).not.toHaveBeenCalled();
    await user.click(
      within(dialog).getByRole("button", { name: "创建一次性注册码" }),
    );
    expect(operate).toHaveBeenCalledWith("create_pairing_code", {});
    expect(await within(dialog).findByText("one-time-code")).toBeVisible();
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: /查看 / })).toBeNull();
    await user.click(
      within(dialog).getByRole("button", { name: "复制配对命令" }),
    );
    expect(copy).toHaveBeenCalledWith(
      "chiliarch pair --server 'http://platform.example:8765' --code 'one-time-code' --name '<agent 名称>' --machine '<机器标识>'",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "复制注册码" }),
    );
    expect(copy).toHaveBeenLastCalledWith("one-time-code");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(add).toHaveFocus();
    await user.click(add);
    expect(screen.queryByText("one-time-code")).toBeNull();
  });

  it("指定整数有效期才传给服务端，平台地址回退到当前 origin 并正确引用单引号", async () => {
    const operate = vi.spyOn(api, "operate").mockResolvedValue({
      code: "code'quote",
      expires_at: 1759800900,
    });
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(<AgentsPage state={platformState()} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "＋ 添加 agent" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("注册码有效期"), "120");
    await user.click(
      within(dialog).getByRole("button", { name: "创建一次性注册码" }),
    );
    expect(operate).toHaveBeenCalledWith("create_pairing_code", {
      ttl_seconds: 120,
    });
    await user.click(
      await within(dialog).findByRole("button", { name: "复制配对命令" }),
    );
    expect(copy).toHaveBeenCalledWith(
      `chiliarch pair --server '${window.location.origin}' --code 'code'\\''quote' --name '<agent 名称>' --machine '<机器标识>'`,
    );
  });

  it("无效有效期不会提交，生成失败保留对话框与错误，关闭按钮恢复焦点", async () => {
    const operate = vi
      .spyOn(api, "operate")
      .mockRejectedValue(new ApiError("没有创建权限", 403));
    const user = userEvent.setup();
    render(<AgentsPage state={platformState()} refresh={vi.fn()} />);
    const add = screen.getByRole("button", { name: "＋ 添加 agent" });
    await user.click(add);
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("注册码有效期"), {
      target: { value: "120.5" },
    });
    fireEvent.submit(dialog.querySelector("form")!);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "整数秒",
    );
    expect(operate).not.toHaveBeenCalled();
    await user.clear(within(dialog).getByLabelText("注册码有效期"));
    await user.click(
      within(dialog).getByRole("button", { name: "创建一次性注册码" }),
    );
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "没有创建权限",
    );
    expect(
      within(dialog).queryByRole("button", { name: "复制注册码" }),
    ).toBeNull();
    await user.click(
      within(dialog).getByRole("button", { name: "关闭添加 agent" }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(add).toHaveFocus();
  });
});

describe("详情操作与授权", () => {
  it("离线派发发送正确的 agent 与 JSON 参数，非法对象不会调用接口", async () => {
    const state = platformState();
    state.agents = [agent({ online: false })];
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ status: "queued", id: "new-task" });
    const refresh = vi.fn();
    const user = userEvent.setup();
    render(<AgentsPage state={state} refresh={refresh} />);
    await user.click(screen.getByRole("button", { name: /查看 开发机/ }));
    expect(screen.getByText(/等待它主动连接/)).toBeVisible();
    const send = screen.getByRole("button", { name: "加入离线队列" });
    expect(send).toBeDisabled();
    await user.type(screen.getByLabelText("任务说明"), "  分析测试结果  ");
    await user.click(screen.getByText("附加参数"));
    const params = screen.getByLabelText("JSON 对象");
    fireEvent.change(params, { target: { value: "[]" } });
    await user.click(send);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "必须是 JSON 对象",
    );
    expect(operate).not.toHaveBeenCalled();
    fireEvent.change(params, { target: { value: '{"priority":2}' } });
    await user.type(
      screen.getByLabelText("工作目录（可选）"),
      "C:\\Projects\\demo",
    );
    await user.click(send);
    expect(operate).toHaveBeenCalledWith("dispatch_task", {
      agent_id: "agent-one",
      instruction: "分析测试结果",
      parameters: { priority: 2 },
      workdir: "C:\\Projects\\demo",
    });
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("任务说明")).toHaveValue("");
    expect(
      screen.getByText("指令已加入离线队列，agent 上线后接收。"),
    ).toBeVisible();
  });

  it("请求与审批使用各自的持久 ID，取消任务沿用任务 ID", async () => {
    const state = platformState();
    state.agents = [agent()];
    state.tasks = [task()];
    state.requests = [request()];
    state.approvals = [approval()];
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ status: "approved" });
    const refresh = vi.fn();
    const user = userEvent.setup();
    render(<AgentsPage state={state} refresh={refresh} />);
    await user.click(screen.getByRole("button", { name: /查看 开发机/ }));
    await user.click(screen.getByRole("button", { name: "允许请求" }));
    expect(operate).toHaveBeenLastCalledWith("resolve_agent_request", {
      request_id: "request-one",
      decision: "approve",
    });
    await user.click(screen.getByRole("button", { name: "拒绝请求" }));
    expect(operate).toHaveBeenLastCalledWith("resolve_agent_request", {
      request_id: "request-one",
      decision: "reject",
    });
    await user.click(screen.getByRole("button", { name: "允许这次操作" }));
    expect(operate).toHaveBeenLastCalledWith("approve_operation", {
      approval_id: "approval-one",
    });
    await user.click(screen.getByRole("button", { name: "拒绝" }));
    expect(operate).toHaveBeenLastCalledWith("reject_operation", {
      approval_id: "approval-one",
    });
    await user.click(screen.getByText("校验发布产物", { selector: "strong" }));
    await user.click(screen.getByRole("button", { name: "请求取消任务" }));
    expect(operate).toHaveBeenLastCalledWith("cancel_task", {
      task_id: "task-one",
    });
    expect(refresh).toHaveBeenCalledTimes(5);
  });

  it("重命名与吊销针对选中身份，吊销后的服务端快照禁用派发与名称编辑", async () => {
    const state = platformState();
    state.agents = [agent()];
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ status: "ok" });
    const refresh = vi.fn();
    const user = userEvent.setup();
    const view = render(<AgentsPage state={state} refresh={refresh} />);
    await user.click(screen.getByRole("button", { name: /查看 开发机/ }));
    expect(screen.getByRole("button", { name: "保存名称" })).toBeDisabled();
    await user.clear(screen.getByLabelText("名称", { exact: true }));
    await user.type(
      screen.getByLabelText("名称", { exact: true }),
      "  工作机  ",
    );
    await user.click(screen.getByRole("button", { name: "保存名称" }));
    expect(operate).toHaveBeenLastCalledWith("rename_agent", {
      agent_id: "agent-one",
      name: "工作机",
    });
    await user.click(screen.getByText("吊销长期凭证"));
    await user.click(
      screen.getByRole("button", { name: "吊销 开发机 的凭证" }),
    );
    expect(operate).toHaveBeenLastCalledWith("revoke_agent", {
      agent_id: "agent-one",
    });
    expect(refresh).toHaveBeenCalledTimes(2);
    view.rerender(
      <AgentsPage
        state={{
          ...state,
          agents: [agent({ name: "工作机", revoked: true, online: false })],
        }}
        refresh={refresh}
      />,
    );
    expect(screen.getByLabelText("任务说明")).toBeDisabled();
    expect(screen.getByLabelText("名称", { exact: true })).toBeDisabled();
    expect(screen.getByRole("button", { name: "加入离线队列" })).toBeDisabled();
    expect(screen.getByText(/此身份的凭证已吊销/)).toBeVisible();
    expect(screen.queryByText("吊销长期凭证")).toBeNull();
  });
});

describe("agent 执行感知", () => {
  it("在线连接与执行能力分开显示，旧客户端不推测为可执行", async () => {
    const state = platformState();
    state.agents = [
      agent({
        id: "manual",
        name: "App 会话",
        metadata: {
          execution_mode: "manual",
          state: "waiting_for_claim",
          queued_tasks: 2,
        },
      }),
      agent({
        id: "managed",
        name: "CLI 执行器",
        metadata: {
          execution_mode: "managed",
          state: "idle",
          capacity: 2,
          adapter: "codex-cli",
          workspace: "/workspace/project",
          active_task_ids: [],
          queued_tasks: 0,
        },
      }),
      agent({
        id: "blocked",
        name: "受阻执行器",
        metadata: {
          execution_mode: "managed",
          state: "blocked",
          blocked_reason: "工作目录不可用",
        },
      }),
      agent({ id: "legacy", name: "旧客户端" }),
    ];
    const user = userEvent.setup();
    render(<AgentsPage state={state} refresh={vi.fn()} />);
    expect(
      within(screen.getByRole("button", { name: /查看 App 会话/ })).getByText(
        "等待会话领取",
      ),
    ).toBeVisible();
    expect(
      within(screen.getByRole("button", { name: /查看 CLI 执行器/ })).getByText(
        "可自动执行",
      ),
    ).toBeVisible();
    expect(
      within(screen.getByRole("button", { name: /查看 受阻执行器/ })).getByText(
        "执行受阻",
      ),
    ).toBeVisible();
    expect(
      within(screen.getByRole("button", { name: /查看 旧客户端/ })).getByText(
        "执行能力未上报",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: /查看 CLI 执行器/ }));
    expect(screen.getByLabelText("执行状态")).toHaveTextContent("codex-cli");
    expect(screen.getByLabelText("执行状态")).toHaveTextContent(
      "/workspace/project",
    );
    expect(screen.getByLabelText("执行状态")).toHaveTextContent("执行容量2");
  });
});
