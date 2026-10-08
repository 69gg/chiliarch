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
import type { MockInstance } from "vitest";
import ChatPage from "../ChatPage";
import { api, ApiError } from "../api";
import type { ChatEvent, ConversationDetail } from "../types";
import { platformState } from "./fixtures";

function conversation(id: string, title: string): ConversationDetail {
  return {
    id,
    title,
    source: "user",
    created_at: 0,
    updated_at: 0,
    messages: [],
    runs: [],
    last_event_seq: 0,
  };
}

function setup(
  details: ConversationDetail[] = [conversation("one", "部署检查")],
): {
  receive: Map<string, (event: ChatEvent) => void>;
  stop: Map<string, ReturnType<typeof vi.fn>>;
  operate: MockInstance<typeof api.operate>;
} {
  let records = [...details];
  const receive = new Map<string, (event: ChatEvent) => void>();
  const stop = new Map<string, ReturnType<typeof vi.fn>>();
  vi.spyOn(api, "conversations").mockImplementation(async () => records);
  vi.spyOn(api, "conversation").mockImplementation(async (id) =>
    records.find((entry) => entry.id === id)!,
  );
  vi.spyOn(api, "streamConversation").mockImplementation(
    (id, _after, handler) => {
      const close = vi.fn();
      receive.set(id, handler);
      stop.set(id, close);
      return close;
    },
  );
  const operate = vi
    .spyOn(api, "operate")
    .mockImplementation(async (name, args) => {
      if (name === "rename_conversation")
        records = records.map((entry) =>
          entry.id === args.conversation_id
            ? { ...entry, title: String(args.title) }
            : entry,
        );
      if (name === "delete_conversation")
        records = records.filter((entry) => entry.id !== args.conversation_id);
      return { status: "completed" } as never;
    });
  render(
    <ChatPage
      state={platformState()}
      refresh={vi.fn()}
      onConfigure={vi.fn()}
    />,
  );
  return { receive, stop, operate };
}

function hoverAt(element: Element, x: number, pointerType = "mouse"): void {
  const event = new MouseEvent("pointermove", {
    bubbles: true,
    clientX: x,
    clientY: 100,
  });
  Object.defineProperty(event, "pointerType", { value: pointerType });
  fireEvent(element, event);
}

async function open(): Promise<HTMLElement> {
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "展开会话列表" }));
  return screen.getByRole("dialog", { name: "会话列表" });
}

async function choose(title: string): Promise<void> {
  await open();
  await userEvent
    .setup()
    .click(
      await screen.findByRole("button", { name: new RegExp(`^${title}`) }),
    );
  await waitFor(() =>
    expect(
      screen.getByText(title, { selector: ".chat-conversation-title" }),
    ).toBeVisible(),
  );
}

describe("右侧会话抽屉交互", () => {
  it("移到右侧边缘显示，离开收起，且不抢输入焦点或提交草稿", async () => {
    setup();
    const nativeShow = vi.spyOn(HTMLDialogElement.prototype, "show");
    const user = userEvent.setup();
    const input = screen.getByLabelText("发送消息");
    await user.type(input, "正在编辑的草稿");
    const layout = input.closest(".chat-layout")!;
    vi.spyOn(layout, "getBoundingClientRect").mockReturnValue({
      right: 1200,
    } as DOMRect);
    hoverAt(layout, 1195);
    const drawer = screen.getByRole("dialog", { name: "会话列表" });
    vi.spyOn(drawer, "getBoundingClientRect").mockReturnValue({
      left: 884,
      right: 1186,
      top: 8,
      bottom: 790,
    } as DOMRect);
    expect(drawer).toHaveAttribute("open");
    expect(input).toHaveFocus();
    expect(nativeShow).not.toHaveBeenCalled();
    hoverAt(layout, 1187);
    expect(drawer).toHaveAttribute("open");
    hoverAt(drawer, 1100);
    expect(drawer).toHaveAttribute("open");
    hoverAt(layout, 800);
    expect(drawer).not.toHaveAttribute("open");
    expect(input).toHaveFocus();
    expect(input).toHaveValue("正在编辑的草稿");
    expect(api.operate).not.toHaveBeenCalled();
  });

  it("触摸和笔输入不触发 hover；点击固定后鼠标离开仍显示", async () => {
    setup();
    const layout = screen.getByLabelText("发送消息").closest(".chat-layout")!;
    vi.spyOn(layout, "getBoundingClientRect").mockReturnValue({
      right: 1200,
    } as DOMRect);
    hoverAt(layout, 1195, "touch");
    hoverAt(layout, 1195, "pen");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    hoverAt(layout, 1195);
    const drawer = screen.getByRole("dialog", { name: "会话列表" });
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "固定会话列表" }));
    hoverAt(layout, 100);
    fireEvent.pointerLeave(drawer);
    expect(drawer).toHaveAttribute("open");
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "收起会话列表" }));
    expect(drawer).not.toHaveAttribute("open");
  });

  it("键盘进入 hover 抽屉后保留可操作状态，Escape 收起并回到列表按钮", async () => {
    setup();
    const input = screen.getByLabelText("发送消息");
    input.focus();
    const layout = input.closest(".chat-layout")!;
    vi.spyOn(layout, "getBoundingClientRect").mockReturnValue({
      right: 1200,
    } as DOMRect);
    hoverAt(layout, 1195);
    const drawer = screen.getByRole("dialog", { name: "会话列表" });
    act(() =>
      within(drawer).getByRole("button", { name: "关闭会话列表" }).focus(),
    );
    hoverAt(layout, 100);
    expect(drawer).toHaveAttribute("open");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(drawer).not.toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "展开会话列表" })).toHaveFocus();
  });

  it("退出页面清理打开抽屉的全局监听，不留下焦点或 modal", async () => {
    vi.spyOn(api, "conversations").mockResolvedValue([]);
    const remove = vi.spyOn(document, "removeEventListener");
    const { unmount } = render(
      <ChatPage
        state={platformState()}
        refresh={vi.fn()}
        onConfigure={vi.fn()}
      />,
    );
    await open();
    unmount();
    expect(remove.mock.calls.some(([name]) => name === "keydown")).toBe(true);
    expect(remove.mock.calls.some(([name]) => name === "pointerdown")).toBe(
      true,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("逐条重命名和删除会话", () => {
  it("可重命名未打开会话，取消不发请求，保存去掉首尾空白", async () => {
    const { operate } = setup();
    const drawer = await open();
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "重命名 部署检查" }),
    );
    const input = within(drawer).getByLabelText("会话名称");
    expect(input).toHaveFocus();
    await user.clear(input);
    await user.type(input, "先不保存");
    await user.click(within(drawer).getByRole("button", { name: "取消" }));
    expect(operate).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "重命名 部署检查" }));
    await user.clear(within(drawer).getByLabelText("会话名称"));
    await user.type(within(drawer).getByLabelText("会话名称"), "  发布检查  ");
    await user.click(within(drawer).getByRole("button", { name: "保存名称" }));
    expect(operate).toHaveBeenCalledExactlyOnceWith("rename_conversation", {
      conversation_id: "one",
      title: "发布检查",
    });
    expect(
      await within(drawer).findByRole("button", { name: /^发布检查/ }),
    ).toBeVisible();
    expect(screen.queryByLabelText("会话名称")).not.toBeInTheDocument();
    expect(api.conversation).not.toHaveBeenCalled();
  });

  it("重命名失败保留编辑输入和原始标题，空名称不能提交", async () => {
    const { operate } = setup();
    operate.mockRejectedValueOnce(new ApiError("会话名称保存失败", 503));
    const drawer = await open();
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "重命名 部署检查" }),
    );
    const input = within(drawer).getByLabelText("会话名称");
    await user.clear(input);
    await user.type(input, "   ");
    expect(
      within(drawer).getByRole("button", { name: "保存名称" }),
    ).toBeDisabled();
    await user.clear(input);
    await user.type(input, "需要保留的名称");
    await user.click(within(drawer).getByRole("button", { name: "保存名称" }));
    expect(await within(drawer).findByRole("alert")).toHaveTextContent(
      "会话名称保存失败",
    );
    expect(input).toHaveValue("需要保留的名称");
    expect(
      within(drawer).getByRole("button", { name: /^部署检查/ }),
    ).toBeVisible();
    expect(operate).toHaveBeenCalledTimes(1);
  });

  it("删除未打开会话前要确认，取消没有副作用，确认后只移除目标", async () => {
    const { operate } = setup([
      conversation("one", "部署检查"),
      conversation("two", "日常协调"),
    ]);
    const drawer = await open();
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "删除 部署检查" }),
    );
    expect(operate).not.toHaveBeenCalled();
    await user.click(within(drawer).getByRole("button", { name: "取消" }));
    await user.click(screen.getByRole("button", { name: "删除 部署检查" }));
    await user.click(within(drawer).getByRole("button", { name: "确认删除" }));
    expect(operate).toHaveBeenCalledExactlyOnceWith("delete_conversation", {
      conversation_id: "one",
    });
    await waitFor(() =>
      expect(
        within(drawer).queryByRole("button", { name: /^部署检查/ }),
      ).not.toBeInTheDocument(),
    );
    expect(
      within(drawer).getByRole("button", { name: /^日常协调/ }),
    ).toBeVisible();
  });

  it("删除当前会话清空历史和草稿，停止订阅，迟到流事件不复活旧正文", async () => {
    const detail = conversation("one", "部署检查");
    detail.messages = [
      {
        id: "history",
        role: "assistant",
        content: "即将删除的历史",
        reasoning: "",
        created_at: 0,
      },
    ];
    const { receive, stop } = setup([detail]);
    await choose(detail.title);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("发送消息"), "旧会话草稿");
    const drawer = await open();
    await user.click(screen.getByRole("button", { name: "删除 部署检查" }));
    await user.click(within(drawer).getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(stop.get("one")).toHaveBeenCalledOnce());
    expect(screen.queryByText("即将删除的历史")).not.toBeInTheDocument();
    expect(screen.getByLabelText("发送消息")).toHaveValue("");
    expect(
      screen.queryByText(detail.title, {
        selector: ".chat-conversation-title",
      }),
    ).not.toBeInTheDocument();
    act(() =>
      receive.get("one")?.({
        seq: 1,
        type: "text_delta",
        run_id: "old-run",
        data: { message_id: "late", delta: "迟到消息" },
      }),
    );
    expect(screen.queryByText("迟到消息")).not.toBeInTheDocument();
  });

  it("运行中的当前会话不能删除；其他会话服务器拒绝后仍保留条目和错误", async () => {
    const detail = conversation("one", "部署检查");
    detail.runs = [
      {
        id: "running",
        conversation_id: detail.id,
        status: "awaiting_approval",
        created_at: 0,
        updated_at: 0,
      },
    ];
    const { operate } = setup([detail, conversation("two", "后台任务")]);
    operate.mockRejectedValueOnce(new ApiError("会话仍有活跃运行", 409));
    await choose(detail.title);
    const drawer = await open();
    expect(
      screen.getByRole("button", { name: "删除 部署检查" }),
    ).toBeDisabled();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "删除 后台任务" }));
    await user.click(within(drawer).getByRole("button", { name: "确认删除" }));
    expect(await within(drawer).findByRole("alert")).toHaveTextContent(
      "会话仍有活跃运行",
    );
    expect(
      within(drawer).getByRole("button", { name: /^后台任务/ }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "停止运行" })).toBeEnabled();
  });

  it("自动命名事件更新列表和当前标题，同时保留未保存的手动名称", async () => {
    const { receive } = setup();
    await choose("部署检查");
    const drawer = await open();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "重命名 部署检查" }));
    const input = within(drawer).getByLabelText("会话名称");
    await user.clear(input);
    await user.type(input, "手动编辑中的名称");
    vi.mocked(api.conversations).mockResolvedValue([
      conversation("one", "服务器部署进度"),
    ]);
    act(() =>
      receive.get("one")?.({
        seq: 1,
        type: "conversation_updated",
        run_id: null,
        data: {
          conversation_id: "one",
          title: "服务器部署进度",
          source: "automatic",
        },
      }),
    );
    expect(
      screen.getByText("服务器部署进度", {
        selector: ".chat-conversation-title",
      }),
    ).toBeVisible();
    expect(
      await within(drawer).findByRole("button", { name: /^服务器部署进度/ }),
    ).toBeVisible();
    expect(input).toHaveValue("手动编辑中的名称");
  });
});

describe("目标关联会话", () => {
  it("从目标深链接进入会话，显示协调事件并保留返回目标的入口", async () => {
    const detail = conversation("coordination-chat", "登录功能协作");
    detail.messages = [
      {
        id: "coordination-event",
        role: "agent",
        source: "coordination",
        content: "实现任务已完成，等待验收。",
        reasoning: "",
        created_at: 0,
      },
    ];
    history.replaceState(null, "", "#ai/coordination-chat");
    vi.spyOn(api, "conversations").mockResolvedValue([detail]);
    vi.spyOn(api, "conversation").mockResolvedValue(detail);
    vi.spyOn(api, "streamConversation").mockReturnValue(vi.fn());
    const state = platformState();
    state.goals = [
      {
        id: "goal-one",
        title: "交付登录功能",
        objective: "实现并评审",
        constraints: [],
        acceptance_criteria: [],
        summary: "",
        status: "active",
        conversation_id: detail.id,
        created_at: 0,
        updated_at: 0,
      },
    ];
    render(<ChatPage state={state} refresh={vi.fn()} onConfigure={vi.fn()} />);
    expect(await screen.findByText("实现任务已完成，等待验收。")).toBeVisible();
    expect(api.conversation).toHaveBeenCalledWith(detail.id);
    expect(screen.getByText("协调事件")).toBeVisible();
    expect(screen.queryByText("agent 来信")).toBeNull();
    expect(
      screen.getByRole("link", { name: "交付登录功能 ↗" }),
    ).toHaveAttribute("href", "#goals/goal-one");
  });
});
