import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Login } from "../App";
import ChatPage from "../ChatPage";
import ConfigPage from "../ConfigPage";
import AgentsPage from "../AgentsPage";
import { ApprovalItem } from "../components";
import { api, ApiError } from "../api";
import type { ChatEvent, ConversationDetail } from "../types";
import { platformState } from "./fixtures";

describe("登录", () => {
  it("输入密码后展示服务器错误，成功认证后才进入工作区", async () => {
    const login = vi
      .spyOn(api, "login")
      .mockRejectedValueOnce(new ApiError("密码无效", 401))
      .mockResolvedValueOnce({ authenticated: true, csrf_token: "csrf" });
    const entered = vi.fn();
    const user = userEvent.setup();
    render(<Login onLogin={entered} />);
    await user.type(screen.getByLabelText("登录密码"), "input-password");
    await user.click(screen.getByRole("button", { name: "登录 →" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("密码无效");
    expect(entered).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "登录 →" }));
    await waitFor(() => expect(entered).toHaveBeenCalledOnce());
    expect(login).toHaveBeenLastCalledWith("input-password");
  });
});

describe("操作审批", () => {
  const approval = {
    id: "approval-one",
    operation: "dispatch_task",
    arguments: { agent_id: "agent-one", instruction: "实现功能" },
    requested_by: "supervisor",
    status: "pending",
    reason: null,
    created_at: 0,
    resolved_at: null,
    result: null,
  };
  it.each([
    ["允许这次操作", "approve_operation"],
    ["拒绝", "reject_operation"],
  ])("点击%s只提交对应审批 ID", async (button, operation) => {
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ status: "approved" });
    const resolved = vi.fn();
    render(<ApprovalItem approval={approval} onResolved={resolved} />);
    await userEvent.setup().click(screen.getByRole("button", { name: button }));
    expect(operate).toHaveBeenCalledWith(operation, {
      approval_id: "approval-one",
    });
    expect(resolved).toHaveBeenCalledOnce();
  });
});

describe("持久会话和事件流", () => {
  it("模型使用 SDK 默认地址时仍可发送消息", async () => {
    const state = platformState();
    state.config.model = { model: "test-model", base_url: "" };
    const conversation: ConversationDetail = {
      id: "default-url",
      title: "默认地址会话",
      source: "user",
      created_at: 0,
      updated_at: 0,
      messages: [],
      runs: [],
    };
    vi.spyOn(api, "conversations").mockResolvedValue([]);
    vi.spyOn(api, "createConversation").mockResolvedValue(conversation);
    vi.spyOn(api, "conversation").mockResolvedValue(conversation);
    vi.spyOn(api, "streamConversation").mockReturnValue(vi.fn());
    const send = vi.spyOn(api, "sendMessage").mockResolvedValue({
      conversation_id: conversation.id,
      run_id: "default-url-run",
    });
    const user = userEvent.setup();
    render(<ChatPage state={state} refresh={vi.fn()} onConfigure={vi.fn()} />);
    const composer = screen.getByLabelText("发送消息");
    expect(composer).toBeEnabled();
    await user.type(composer, "查看所有机器的工作进度");
    await user.click(screen.getByRole("button", { name: "发送 ↑" }));
    await waitFor(() =>
      expect(send).toHaveBeenCalledWith(
        "default-url",
        "查看所有机器的工作进度",
      ),
    );
  });

  it.each([false, true])(
    "模型未配置时恢复真实 agent 来信，并避免重复持久消息（已入消息表=%s）",
    async (persisted) => {
      const state = platformState();
      state.config.model = { model: "", base_url: "" };
      const detail: ConversationDetail = {
        id: "agent-conversation",
        title: "机器 A 来信",
        source: "agent",
        agent_id: "agent-one",
        created_at: 0,
        updated_at: 0,
        last_event_seq: 7,
        messages: persisted
          ? [
              {
                id: "stored-agent-message",
                role: "agent",
                content: "离线工作进度已保存",
                reasoning: "",
                created_at: 0,
                source: "agent",
              },
            ]
          : [],
        runs: [],
        events: [
          {
            seq: 7,
            type: "agent_contact",
            run_id: null,
            data: {
              message_id: "envelope-one",
              agent_id: "agent-one",
              content: { message: "离线工作进度已保存", progress: 0.5 },
            },
          },
        ],
      };
      vi.spyOn(api, "conversations").mockResolvedValue([detail]);
      vi.spyOn(api, "conversation").mockResolvedValue(detail);
      const stream = vi
        .spyOn(api, "streamConversation")
        .mockReturnValue(vi.fn());
      render(
        <ChatPage state={state} refresh={vi.fn()} onConfigure={vi.fn()} />,
      );
      await userEvent
        .setup()
        .click(screen.getByRole("button", { name: "展开会话列表" }));
      await userEvent
        .setup()
        .click(await screen.findByRole("button", { name: /^机器 A 来信/ }));
      const workspace = screen.getByRole("region", {
        name: "与 Chiliarch AI 对话",
      });
      await waitFor(() =>
        expect(workspace).toHaveTextContent("离线工作进度已保存"),
      );
      expect(within(workspace).getAllByText("agent 来信")).toHaveLength(1);
      expect(screen.getByLabelText("发送消息")).toBeDisabled();
      expect(
        within(workspace).queryByText("Chiliarch AI"),
      ).not.toBeInTheDocument();
      expect(stream.mock.calls[0][1]).toBe(7);
    },
  );

  it("恢复历史审批时保留任务终态，并隔离已切换会话的旧事件", async () => {
    const first: ConversationDetail = {
      id: "first",
      title: "第一个会话",
      source: "user",
      created_at: 0,
      updated_at: 0,
      last_event_seq: 9,
      messages: [
        {
          id: "answer-first",
          role: "assistant",
          content: "第一段正文",
          reasoning: "",
          created_at: 0,
          run_id: "run-first",
        },
      ],
      runs: [
        {
          id: "run-first",
          conversation_id: "first",
          status: "completed",
          created_at: 0,
          updated_at: 0,
        },
      ],
      events: [
        {
          seq: 7,
          type: "approval_required",
          run_id: "run-first",
          data: { id: "tool-old", name: "dispatch_task", arguments: {} },
        },
        {
          seq: 8,
          type: "tool_end",
          run_id: "run-first",
          data: {
            id: "tool-old",
            name: "dispatch_task",
            result: { status: "queued" },
          },
        },
      ],
    };
    const second: ConversationDetail = {
      ...first,
      id: "second",
      title: "第二个会话",
      messages: [
        {
          id: "answer-second",
          role: "assistant",
          content: "第二段正文",
          reasoning: "",
          created_at: 0,
        },
      ],
      runs: [],
      events: [],
    };
    vi.spyOn(api, "conversations").mockResolvedValue([first, second]);
    vi.spyOn(api, "conversation").mockImplementation(async (id) =>
      id === "first" ? first : second,
    );
    const receivers: Record<string, (event: ChatEvent) => void> = {};
    const stop = vi.fn();
    vi.spyOn(api, "streamConversation").mockImplementation(
      (id, _cursor, receive) => {
        receivers[id] = receive;
        return stop;
      },
    );
    const user = userEvent.setup();
    render(
      <ChatPage
        state={platformState()}
        refresh={vi.fn()}
        onConfigure={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "展开会话列表" }));
    await user.click(
      await screen.findByRole("button", { name: /^第一个会话/ }),
    );
    expect(await screen.findByText("第一段正文")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "停止运行" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "展开会话列表" }));
    await user.click(screen.getByRole("button", { name: /^第二个会话/ }));
    expect(await screen.findByText("第二段正文")).toBeVisible();
    act(() =>
      receivers.first({
        seq: 10,
        type: "text_delta",
        run_id: "run-first",
        data: { message_id: "answer-first", delta: "旧连接迟到内容" },
      }),
    );
    expect(screen.queryByText(/旧连接迟到内容/)).not.toBeInTheDocument();
    expect(stop).toHaveBeenCalled();
  });

  it("从快照游标继续，增量渲染 Markdown、思考和工具，不重复历史正文", async () => {
    const detail: ConversationDetail = {
      id: "conversation-one",
      title: "工作进度",
      source: "user",
      created_at: 0,
      updated_at: 0,
      last_event_seq: 12,
      events: [],
      messages: [
        {
          id: "answer",
          role: "assistant",
          content: "已保存的回答。",
          reasoning: "",
          created_at: 0,
          run_id: "run-one",
        },
      ],
      runs: [
        {
          id: "run-one",
          conversation_id: "conversation-one",
          status: "running",
          created_at: 0,
          updated_at: 0,
        },
      ],
    };
    vi.spyOn(api, "conversations").mockResolvedValue([detail]);
    vi.spyOn(api, "conversation").mockResolvedValue(detail);
    let receive: ((event: ChatEvent) => void) | undefined;
    const stream = vi
      .spyOn(api, "streamConversation")
      .mockImplementation((_id, _after, callback) => {
        receive = callback;
        return vi.fn();
      });
    render(
      <ChatPage
        state={platformState()}
        refresh={vi.fn()}
        onConfigure={vi.fn()}
      />,
    );
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "展开会话列表" }));
    await userEvent
      .setup()
      .click(await screen.findByRole("button", { name: /^工作进度/ }));
    await waitFor(() => expect(stream).toHaveBeenCalled());
    expect(stream.mock.calls[0][1]).toBe(12);
    act(() => {
      receive?.({
        seq: 13,
        type: "text_delta",
        run_id: "run-one",
        data: {
          message_id: "answer",
          delta: "\n\n## 当前进度\n\n**真实输出**\n\n```python\nprint(1)\n```",
        },
      });
      receive?.({
        seq: 14,
        type: "reasoning_delta",
        run_id: "run-one",
        data: { message_id: "answer", delta: "先读取任务状态。" },
      });
      receive?.({
        seq: 15,
        type: "tool_start",
        run_id: "run-one",
        data: {
          id: "tool-one",
          name: "get_state",
          arguments: { agent_id: "agent-one" },
        },
      });
      receive?.({
        seq: 16,
        type: "tool_end",
        run_id: "run-one",
        data: {
          id: "tool-one",
          name: "get_state",
          result: { status: "running" },
        },
      });
    });
    expect(screen.getByRole("heading", { name: "当前进度" })).toBeVisible();
    expect(screen.getByText("真实输出").tagName).toBe("STRONG");
    expect(screen.getAllByText("已保存的回答。")).toHaveLength(1);
    const reasoning = screen.getByText("思考过程").closest("details")!;
    expect(reasoning).toHaveAttribute("open");
    expect(within(reasoning).getByText("先读取任务状态。")).toBeVisible();
    const tool = screen.getByText("get_state").closest("details")!;
    await userEvent.setup().click(within(tool).getByText("get_state"));
    expect(tool).toHaveAttribute("open");
    expect(within(tool).getByText("参数")).toBeVisible();
    expect(within(tool).getByText("结果")).toBeVisible();
    expect(within(tool).getByText("已完成")).toBeVisible();
    const clipboard = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue();
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "复制代码" }));
    expect(clipboard).toHaveBeenCalledWith("print(1)");
    act(() => {
      receive?.({
        seq: 17,
        type: "completed",
        run_id: "run-one",
        data: { status: "completed" },
      });
      receive?.({
        seq: 18,
        type: "run_started",
        run_id: "run-two",
        data: { message_id: "answer-two" },
      });
      receive?.({
        seq: 19,
        type: "tool_start",
        run_id: "run-two",
        data: {
          id: "tool-one",
          name: "get_state",
          arguments: { agent_id: "agent-two" },
        },
      });
      receive?.({
        seq: 20,
        type: "tool_end",
        run_id: "run-two",
        data: {
          id: "tool-one",
          name: "get_state",
          result: { status: "offline" },
        },
      });
    });
    expect(screen.getAllByText("get_state")).toHaveLength(2);
    act(() =>
      receive?.({
        seq: 21,
        type: "error",
        run_id: "run-two",
        data: {
          status: "interrupted",
          message: "模型配置已变更，此次运行已中断。",
        },
      }),
    );
    expect(
      screen.queryByRole("button", { name: "停止运行" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("此次运行已中断");
  });
});

describe("模型配置", () => {
  it("保存可设置的模型上下文窗口，不提交已移除的运行限制", async () => {
    const state = platformState();
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ config: state.config, revision: 2 });
    render(<ConfigPage state={state} refresh={vi.fn()} />);
    expect(screen.getByLabelText("模型上下文窗口（tokens）")).toHaveValue(
      200000,
    );
    for (const label of [
      "最大输出 tokens",
      "最大工具轮次",
      "上下文消息上限",
      "并行运行上限",
    ])
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("模型上下文窗口（tokens）"), {
      target: { value: "128000" },
    });
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "保存配置" }));
    expect(operate).toHaveBeenCalledExactlyOnceWith("update_config", {
      patch: { model: { context_window: 128000 } },
    });
  });

  it.each([
    { field: "port", label: "监听端口", current: 8000, next: 9000 },
    {
      field: "graceful_shutdown_seconds",
      label: "关闭时等待请求结束（秒）",
      current: 15,
      next: 8,
    },
  ])("保存 $label 后提示重启，表单保持当前生效值", async ({ field, label, current, next }) => {
    const state = platformState();
    state.config.server = { [field]: current };
    const operate = vi.spyOn(api, "operate").mockResolvedValue({
      config: state.config,
      revision: 2,
      pending_restart: [`server.${field}`],
    });
    render(<ConfigPage state={state} refresh={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(label), {
      target: { value: String(next) },
    });
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "保存配置" }));
    expect(operate).toHaveBeenCalledWith("update_config", {
      patch: { server: { [field]: next } },
    });
    expect(await screen.findByRole("status")).toHaveTextContent(
      `${label}需重启后生效`,
    );
    expect(screen.getByLabelText(label)).toHaveValue(current);
  });

  it("删去掩码请求头会提交完整空对象，以真正移除旧请求头", async () => {
    const state = platformState();
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ config: state.config, revision: 2 });
    render(<ConfigPage state={state} refresh={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("额外请求头（JSON）"), {
      target: { value: "{}" },
    });
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "保存配置" }));
    expect(operate).toHaveBeenCalledWith("update_config", {
      patch: { model: { headers: {} } },
    });
  });

  it("更换 API 并保存时不覆盖留空的密钥或掩码请求头", async () => {
    const state = platformState();
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ config: state.config, revision: 2 });
    const user = userEvent.setup();
    render(<ConfigPage state={state} refresh={vi.fn()} />);
    const key = screen.getByLabelText("API Key");
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("placeholder", "已设置 · 留空保持原值");
    await user.selectOptions(screen.getByLabelText("模型 API"), "responses");
    await user.click(screen.getByRole("button", { name: "保存配置" }));
    expect(operate).toHaveBeenCalledWith("update_config", {
      patch: { model: { api_mode: "responses" } },
    });
  });

  it("新密钥只能通过输入显式写入，清除是独立动作", async () => {
    const state = platformState();
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ config: state.config, revision: 2 });
    const user = userEvent.setup();
    render(<ConfigPage state={state} refresh={vi.fn()} />);
    await user.type(screen.getByLabelText("API Key"), "replacement-key");
    await user.click(screen.getByRole("button", { name: "保存配置" }));
    expect(operate).toHaveBeenLastCalledWith("update_config", {
      patch: { model: { api_key: "replacement-key" } },
    });
    await user.click(screen.getByLabelText("清除已保存的 API Key"));
    await user.click(screen.getByRole("button", { name: "保存配置" }));
    expect(operate).toHaveBeenLastCalledWith("update_config", {
      patch: { model: { api_key: "" } },
    });
  });

  it("无效 JSON 参数阻止保存并展示错误", async () => {
    const operate = vi.spyOn(api, "operate").mockResolvedValue({});
    render(<ConfigPage state={platformState()} refresh={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("额外请求参数（JSON）"), {
      target: { value: "[]" },
    });
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "保存配置" }));
    expect(screen.getByRole("alert")).toHaveTextContent("必须是 JSON 对象");
    expect(operate).not.toHaveBeenCalled();
  });
});

describe("agent 调度", () => {
  it("展示已上报的执行进度，取消只针对当前任务", async () => {
    const state = platformState();
    state.agents = [
      {
        id: "agent-one",
        name: "验收机",
        machine: "workstation",
        online: true,
        revoked: false,
        capabilities: [],
        created_at: 0,
        last_seen: 0,
        metadata: {},
      },
    ];
    state.tasks = [
      {
        id: "task-one",
        agent_id: "agent-one",
        instruction: "测试进度",
        parameters: {},
        status: "running",
        progress: 0.4,
        result: null,
        error: null,
        created_at: 0,
        updated_at: 0,
        requested_by: "user",
      },
    ];
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ status: "cancel_requested" });
    const user = userEvent.setup();
    render(<AgentsPage state={state} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /查看 验收机/ }));
    const taskHistory = within(screen.getByRole("region", { name: "任务" }));
    await user.click(taskHistory.getByText("测试进度"));
    expect(
      screen.getByRole("progressbar", { name: "执行进度" }),
    ).toHaveAttribute("value", "0.4");
    expect(taskHistory.getByText("40%")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "请求取消任务" }));
    expect(operate).toHaveBeenCalledWith("cancel_task", {
      task_id: "task-one",
    });
  });

  it("注册码采用服务端默认 TTL，并展示实际返回的码", async () => {
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ code: "one-use-code", expires_at: 2000000000 });
    render(<AgentsPage state={platformState()} refresh={vi.fn()} />);
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: /添加 agent/ }));
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "创建一次性注册码" }));
    expect(operate).toHaveBeenCalledWith("create_pairing_code", {});
    expect(screen.getByText("one-use-code")).toBeVisible();
  });
  it("向选中的离线 agent 派发任务，不生成另一身份", async () => {
    const state = platformState();
    state.agents = [
      {
        id: "offline-agent",
        name: "开发机",
        machine: "workstation",
        capabilities: ["task.dispatch"],
        online: false,
        revoked: false,
        created_at: 0,
        last_seen: 0,
        transport: "sse",
        metadata: {},
      },
    ];
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ task_id: "new-task" });
    const user = userEvent.setup();
    render(<AgentsPage state={state} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /查看 开发机/ }));
    await user.type(screen.getByLabelText("任务说明"), "检查任务状态");
    await user.click(screen.getByRole("button", { name: "加入离线队列" }));
    expect(operate).toHaveBeenCalledWith("dispatch_task", {
      agent_id: "offline-agent",
      instruction: "检查任务状态",
      parameters: {},
    });
  });
});
