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
import ChatPage, { applyChatEvent } from "../ChatPage";
import { api, ApiError } from "../api";
import type {
  Approval,
  ChatEvent,
  ConversationDetail,
  PlatformState,
} from "../types";
import { platformState } from "./fixtures";

for (const name of ["show", "showModal", "close"] as const) {
  if (typeof HTMLDialogElement.prototype[name] !== "function") {
    Object.defineProperty(HTMLDialogElement.prototype, name, {
      configurable: true,
      value() {},
    });
  }
}

beforeEach(() => {
  for (const name of ["show", "showModal"] as const) {
    vi.spyOn(HTMLDialogElement.prototype, name).mockImplementation(function (
      this: HTMLDialogElement,
    ) {
      this.setAttribute("open", "");
      this.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    });
  }
  vi.spyOn(HTMLDialogElement.prototype, "close").mockImplementation(function (
    this: HTMLDialogElement,
  ) {
    this.removeAttribute("open");
  });
});

function conversation(): ConversationDetail {
  return {
    id: "conversation-one",
    title: "部署讨论",
    source: "user",
    created_at: 0,
    updated_at: 0,
    messages: [],
    runs: [],
    events: [],
    last_event_seq: 20,
  };
}

function approval(id: string): Approval {
  return {
    id,
    operation: "dispatch_task",
    arguments: { agent_id: id },
    requested_by: "supervisor",
    status: "pending",
    reason: `许可理由 ${id}`,
    created_at: 0,
    resolved_at: null,
    result: null,
  };
}

function mockConversation(detail: ConversationDetail): void {
  vi.spyOn(api, "conversations").mockResolvedValue([detail]);
  vi.spyOn(api, "conversation").mockResolvedValue(detail);
  vi.spyOn(api, "streamConversation").mockReturnValue(vi.fn());
}

async function selectConversation(): Promise<void> {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "展开会话列表" }));
  await user.click(await screen.findByRole("button", { name: /部署讨论/ }));
}

function renderChat(state: PlatformState = platformState()): void {
  render(<ChatPage state={state} refresh={vi.fn()} onConfigure={vi.fn()} />);
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: Error) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

describe("发送与事件流的竞态", () => {
  it("先建空会话再发送时，提前到达的正文和完成事件不会改变用户消息顺序或运行终态", async () => {
    const detail = conversation();
    mockConversation(detail);
    vi.spyOn(api, "conversations").mockResolvedValue([]);
    vi.spyOn(api, "createConversation").mockResolvedValue(detail);
    const response = deferred<{ run_id: string; conversation_id: string }>();
    const send = vi.spyOn(api, "sendMessage").mockReturnValue(response.promise);
    let receive: ((event: ChatEvent) => void) | undefined;
    vi.spyOn(api, "streamConversation").mockImplementation(
      (_id, _after, callback) => {
        receive = callback;
        return vi.fn();
      },
    );
    renderChat();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "展开会话列表" }));
    await user.click(screen.getByRole("button", { name: "＋ 新会话" }));
    await waitFor(() => expect(receive).toBeDefined());
    const composer = screen.getByLabelText("发送消息");
    await user.type(composer, "请检查机器上的工作进度");
    await user.click(screen.getByRole("button", { name: "发送 ↑" }));
    expect(send).toHaveBeenCalledExactlyOnceWith(
      detail.id,
      "请检查机器上的工作进度",
    );
    expect(
      within(screen.getByRole("article")).getByText("请检查机器上的工作进度"),
    ).toBeVisible();
    act(() => {
      receive?.({
        seq: 21,
        run_id: "early-run",
        type: "text_delta",
        data: { message_id: "early-answer", delta: "工作已完成。" },
      });
      receive?.({
        seq: 22,
        run_id: "early-run",
        type: "completed",
        data: { status: "completed" },
      });
    });
    const articles = screen.getAllByRole("article");
    expect(articles).toHaveLength(2);
    expect(
      within(articles[0]).getByText("请检查机器上的工作进度"),
    ).toBeVisible();
    expect(within(articles[1]).getByText("工作已完成。")).toBeVisible();
    await act(async () =>
      response.resolve({ run_id: "early-run", conversation_id: detail.id }),
    );
    expect(screen.getAllByRole("article")).toHaveLength(2);
    expect(screen.getAllByText("请检查机器上的工作进度")).toHaveLength(1);
    expect(screen.getAllByText("工作已完成。")).toHaveLength(1);
    expect(
      screen.queryByRole("button", { name: "停止运行" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeInTheDocument();
    expect(composer).toHaveValue("");
  });

  it("发送失败时移除本地用户占位，保留输入并显示服务器错误", async () => {
    mockConversation(conversation());
    const response = deferred<{ run_id: string; conversation_id: string }>();
    vi.spyOn(api, "sendMessage").mockReturnValue(response.promise);
    renderChat();
    await selectConversation();
    const user = userEvent.setup();
    const composer = screen.getByLabelText("发送消息");
    await user.type(composer, "这段输入需要保留");
    await user.click(screen.getByRole("button", { name: "发送 ↑" }));
    expect(screen.getByRole("article")).toHaveTextContent("这段输入需要保留");
    await act(async () => response.reject(new ApiError("模型暂不可用", 503)));
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(composer).toHaveValue("这段输入需要保留");
    expect(screen.getByRole("alert")).toHaveTextContent("模型暂不可用");
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeEnabled();
  });
});

describe("收起的会话侧栏", () => {
  it("默认隐藏列表并去掉右侧面板，通过紧凑按钮展开与关闭", async () => {
    mockConversation(conversation());
    renderChat();
    expect(
      screen.getByRole("heading", { name: "Chiliarch AI", level: 1 }),
    ).toBeVisible();
    expect(screen.queryByText("总管")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /请求与审批/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "最近任务" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "会话列表" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /部署讨论/ }),
    ).not.toBeInTheDocument();
    const user = userEvent.setup();
    const toggle = screen.getByRole("button", { name: "展开会话列表" });
    await user.click(toggle);
    const drawer = screen.getByRole("dialog", { name: "会话列表" });
    expect(drawer).toHaveAttribute("open");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(
      within(drawer).getByRole("button", { name: "关闭会话列表" }),
    );
    expect(drawer).not.toHaveAttribute("open");
    expect(toggle).toHaveFocus();
    await user.click(toggle);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(drawer).not.toHaveAttribute("open");
    expect(toggle).toHaveFocus();
  });

  it("移动端使用原生模态抽屉，支持取消和选择后关闭并恢复焦点", async () => {
    vi.spyOn(window, "matchMedia").mockReturnValue({
      matches: true,
      media: "(max-width: 700px)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    });
    const detail = conversation();
    detail.messages = [
      {
        id: "answer",
        role: "assistant",
        content: "部署讨论的历史正文",
        reasoning: "",
        created_at: 0,
      },
    ];
    mockConversation(detail);
    renderChat();
    const user = userEvent.setup();
    const toggle = screen.getByRole("button", { name: "展开会话列表" });
    await user.click(toggle);
    const drawer = screen.getByRole("dialog", { name: "会话列表" });
    expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledOnce();
    expect(drawer).toHaveAttribute("aria-modal", "true");
    expect(
      within(drawer).getByRole("button", { name: "关闭会话列表" }),
    ).toHaveFocus();
    fireEvent(drawer, new Event("cancel", { cancelable: true }));
    expect(drawer).not.toHaveAttribute("open");
    expect(toggle).toHaveFocus();
    await user.click(toggle);
    await user.click(await screen.findByRole("button", { name: /部署讨论/ }));
    expect(await screen.findByText("部署讨论的历史正文")).toBeVisible();
    expect(drawer).not.toHaveAttribute("open");
    expect(toggle).toHaveFocus();
  });
});

describe("工具下方的审批", () => {
  it("用户与助手消息共享运行 ID 时，只在助手消息下显示一组工具和审批", async () => {
    const detail = conversation();
    detail.messages = [
      {
        id: "user-message",
        role: "user",
        content: "请给机器派发任务",
        reasoning: "",
        created_at: 0,
        run_id: "shared-run",
      },
      {
        id: "assistant-message",
        role: "assistant",
        content: "正在请求你的许可",
        reasoning: "",
        created_at: 0,
        run_id: "shared-run",
      },
    ];
    detail.runs = [
      {
        id: "shared-run",
        conversation_id: detail.id,
        status: "awaiting_approval",
        pending_approval_id: "shared-approval",
        created_at: 0,
        updated_at: 0,
      },
    ];
    detail.events = [
      {
        seq: 20,
        type: "approval_required",
        run_id: "shared-run",
        data: {
          id: "shared-call",
          name: "dispatch_task",
          arguments: { agent_id: "machine-one" },
          approval_id: "shared-approval",
        },
      },
    ];
    mockConversation(detail);
    const state = platformState();
    state.approvals = [approval("shared-approval")];
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ status: "approved" });
    renderChat(state);
    await selectConversation();
    const groups = await screen.findAllByRole("group", {
      name: "工具调用 dispatch_task",
    });
    expect(groups).toHaveLength(1);
    const userMessage = screen
      .getByText("请给机器派发任务")
      .closest("article")!;
    const assistantMessage = screen
      .getByText("正在请求你的许可")
      .closest("article")!;
    expect(within(userMessage).queryByRole("group")).not.toBeInTheDocument();
    expect(
      within(assistantMessage).getByRole("group", {
        name: "工具调用 dispatch_task",
      }),
    ).toBe(groups[0]);
    const allow = screen.getAllByRole("button", { name: "允许这次操作" });
    expect(allow).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "拒绝" })).toHaveLength(1);
    await userEvent.setup().click(allow[0]);
    expect(operate).toHaveBeenCalledExactlyOnceWith("approve_operation", {
      approval_id: "shared-approval",
    });
  });

  it("刷新后按运行与工具标识恢复审批，同名工具与重复调用 ID 不会串联", async () => {
    const detail = conversation();
    detail.messages = ["one", "two", "missing"].map((id) => ({
      id: `message-${id}`,
      role: "assistant",
      content: `处理 ${id}`,
      reasoning: "",
      created_at: 0,
      run_id: `run-${id}`,
    }));
    detail.events = ["one", "two", "missing"].map((id, index) => ({
      seq: index + 1,
      run_id: `run-${id}`,
      type: "approval_required",
      data: {
        id: "reused-call",
        name: "dispatch_task",
        arguments: { target: id },
        ...(id === "missing" ? {} : { approval_id: `approval-${id}` }),
      },
    }));
    mockConversation(detail);
    const state = platformState();
    state.approvals = [
      approval("approval-one"),
      approval("approval-two"),
      approval("unrelated"),
    ];
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ status: "approved" });
    renderChat(state);
    await selectConversation();
    const groups = await screen.findAllByRole("group", {
      name: "工具调用 dispatch_task",
    });
    expect(groups).toHaveLength(3);
    const first = groups.find((group) =>
      group.textContent?.includes("许可理由 approval-one"),
    )!;
    const second = groups.find((group) =>
      group.textContent?.includes("许可理由 approval-two"),
    )!;
    const missing = groups.find(
      (group) => within(group).queryAllByRole("button").length === 0,
    )!;
    expect(
      within(missing).queryByRole("button", { name: "允许这次操作" }),
    ).not.toBeInTheDocument();
    const firstApproval = within(first).getByRole("region", {
      name: "审批 dispatch_task",
    });
    expect(firstApproval.closest("details")).toBeNull();
    expect(
      within(firstApproval).queryByText("dispatch_task"),
    ).not.toBeInTheDocument();
    expect(firstApproval.querySelector("pre")).toBeNull();
    expect(screen.queryByText("许可理由 unrelated")).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(
      within(first).getByRole("button", { name: "允许这次操作" }),
    );
    await user.click(within(second).getByRole("button", { name: "拒绝" }));
    expect(operate.mock.calls).toEqual([
      ["approve_operation", { approval_id: "approval-one" }],
      ["reject_operation", { approval_id: "approval-two" }],
    ]);
  });

  it("流式审批绑定明确 ID，失败在工具下方可见，完成后按钮不会复活", async () => {
    const detail = conversation();
    detail.messages = [
      {
        id: "message-one",
        role: "assistant",
        content: "准备派发",
        reasoning: "",
        created_at: 0,
        run_id: "run-one",
      },
    ];
    mockConversation(detail);
    let receive: ((event: ChatEvent) => void) | undefined;
    vi.spyOn(api, "streamConversation").mockImplementation(
      (_id, _after, callback) => {
        receive = callback;
        return vi.fn();
      },
    );
    const state = platformState();
    state.approvals = [approval("exact-approval")];
    vi.spyOn(api, "operate").mockRejectedValue(new ApiError("审批已过期", 409));
    renderChat(state);
    await selectConversation();
    await waitFor(() => expect(receive).toBeDefined());
    const event: ChatEvent = {
      seq: 21,
      type: "approval_required",
      run_id: "run-one",
      data: {
        id: "call-one",
        name: "dispatch_task",
        arguments: {},
        approval_id: "exact-approval",
      },
    };
    act(() => receive?.(event));
    const tool = screen.getByRole("group", { name: "工具调用 dispatch_task" });
    await userEvent
      .setup()
      .click(within(tool).getByRole("button", { name: "允许这次操作" }));
    expect(await within(tool).findByRole("alert")).toHaveTextContent(
      "审批已过期",
    );
    act(() =>
      receive?.({
        seq: 22,
        type: "tool_end",
        run_id: "run-one",
        data: {
          id: "call-one",
          name: "dispatch_task",
          result: { status: "rejected" },
        },
      }),
    );
    expect(
      within(tool).queryByRole("button", { name: "允许这次操作" }),
    ).not.toBeInTheDocument();
    const restored = [
      event,
      {
        seq: 22,
        type: "tool_end",
        run_id: "run-one",
        data: {
          id: "call-one",
          name: "dispatch_task",
          result: { status: "rejected" },
        },
      },
    ].reduce(applyChatEvent, { messages: [], runs: [], tools: [] });
    expect(restored.tools[0]).toMatchObject({
      approval_id: "exact-approval",
      status: "completed",
    });
  });
});
