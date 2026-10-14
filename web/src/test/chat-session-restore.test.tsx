import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import App from "../App";
import ChatPage from "../ChatPage";
import { api, ApiError } from "../api";
import type { ConversationDetail } from "../types";
import { platformState } from "./fixtures";

const lastConversationKey = "chiliarch-last-conversation";

function conversation(id: string, title: string): ConversationDetail {
  return {
    id,
    title,
    source: "user",
    created_at: 0,
    updated_at: 0,
    messages: [
      {
        id: `message:${id}`,
        role: "assistant",
        content: `历史正文 ${title}`,
        reasoning: "",
        created_at: 0,
      },
    ],
    runs: [],
    last_event_seq: 7,
  };
}

function mockChat(details: ConversationDetail[]) {
  let records = [...details];
  const stop = vi.fn();
  vi.spyOn(api, "conversations").mockImplementation(async () => records);
  const load = vi.spyOn(api, "conversation").mockImplementation(async (id) => {
    const found = records.find((entry) => entry.id === id);
    if (!found) throw new ApiError("会话不存在", 404);
    return found;
  });
  const stream = vi.spyOn(api, "streamConversation").mockReturnValue(stop);
  const created = conversation("created", "新建的会话");
  const create = vi
    .spyOn(api, "createConversation")
    .mockImplementation(async () => {
      records = [created, ...records];
      return created;
    });
  const send = vi.spyOn(api, "sendMessage").mockImplementation(async (id) => ({
    conversation_id: id,
    run_id: `run:${id}`,
  }));
  const operate = vi
    .spyOn(api, "operate")
    .mockImplementation(async (name, args) => {
      if (name === "list_native_interactions")
        return { interactions: [] } as never;
      if (name === "delete_conversation")
        records = records.filter((entry) => entry.id !== args.conversation_id);
      return { status: "completed" };
    });
  return { load, stream, stop, create, send, operate, created };
}

function renderChat(): ReturnType<typeof render> {
  return render(
    <ChatPage
      state={platformState()}
      refresh={vi.fn()}
      onConfigure={vi.fn()}
    />,
  );
}

async function expectOpened(title: string): Promise<void> {
  await waitFor(() =>
    expect(
      screen.getByText(title, { selector: ".chat-conversation-title" }),
    ).toBeVisible(),
  );
}

it("没有记录时保留新对话页面，即使服务端已有历史也不自动选择或创建", async () => {
  const { load, stream, create, send } = mockChat([
    conversation("latest", "最新会话"),
  ]);
  renderChat();
  await waitFor(() => expect(api.conversations).toHaveBeenCalledOnce());
  expect(screen.getByText("开始一段对话")).toBeVisible();
  expect(load).not.toHaveBeenCalled();
  expect(stream).not.toHaveBeenCalled();
  expect(create).not.toHaveBeenCalled();
  expect(send).not.toHaveBeenCalled();
});

it("记住实际打开的会话，离开后返回恢复正文并从历史游标续接，不选列表最新一项", async () => {
  const older = conversation("older", "之前打开的会话");
  const { load, stream, stop, create, send } = mockChat([
    conversation("latest", "最新会话"),
    older,
  ]);
  const { unmount } = renderChat();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "展开会话列表" }));
  await user.click(
    await screen.findByRole("button", { name: /^之前打开的会话/ }),
  );
  await expectOpened(older.title);
  expect(localStorage.getItem(lastConversationKey)).toBe(older.id);
  unmount();
  expect(stop).toHaveBeenCalledOnce();
  history.replaceState(null, "", "#ai");
  renderChat();
  await expectOpened(older.title);
  expect(screen.getByText(`历史正文 ${older.title}`)).toBeVisible();
  expect(load).toHaveBeenNthCalledWith(2, older.id);
  expect(stream).toHaveBeenLastCalledWith(
    older.id,
    7,
    expect.any(Function),
    expect.any(Function),
  );
  expect(location.hash).toBe("#ai/older");
  expect(create).not.toHaveBeenCalled();
  expect(send).not.toHaveBeenCalled();
});

it("指定会话链接优先于浏览器记忆，加载成功后更新记忆", async () => {
  const linked = conversation("linked", "链接中的会话");
  const { load } = mockChat([conversation("remembered", "上次会话"), linked]);
  localStorage.setItem(lastConversationKey, "remembered");
  history.replaceState(null, "", "#ai/linked");
  renderChat();
  await expectOpened(linked.title);
  expect(load).toHaveBeenCalledExactlyOnceWith(linked.id);
  expect(localStorage.getItem(lastConversationKey)).toBe(linked.id);
});

it("返回总管导航的 hashchange 不清空已恢复的会话", async () => {
  const remembered = conversation("remembered", "上次会话");
  const { load } = mockChat([remembered]);
  localStorage.setItem(lastConversationKey, remembered.id);
  renderChat();
  await expectOpened(remembered.title);
  act(() => {
    history.replaceState(null, "", "#ai");
    fireEvent(window, new HashChangeEvent("hashchange"));
  });
  await expectOpened(remembered.title);
  expect(localStorage.getItem(lastConversationKey)).toBe(remembered.id);
  expect(load).toHaveBeenCalledOnce();
});

it("通过应用导航从配置页进入和返回总管时恢复同一会话", async () => {
  const remembered = conversation("remembered", "上次会话");
  const { load, stop, send } = mockChat([remembered]);
  vi.spyOn(api, "session").mockResolvedValue({
    authenticated: true,
    csrf_token: "test",
  });
  vi.spyOn(api, "state").mockResolvedValue(platformState());
  localStorage.setItem(lastConversationKey, remembered.id);
  history.replaceState(null, "", "#config");
  render(<App />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "总管对话" }));
  await expectOpened(remembered.title);
  await user.click(screen.getByRole("button", { name: "配置" }));
  await screen.findByRole("heading", { name: "配置" });
  expect(stop).toHaveBeenCalledOnce();
  await user.click(screen.getByRole("button", { name: "总管对话" }));
  await expectOpened(remembered.title);
  expect(load).toHaveBeenCalledTimes(2);
  expect(localStorage.getItem(lastConversationKey)).toBe(remembered.id);
  expect(send).not.toHaveBeenCalled();
});

it.each(["#ai", "#ai/deleted"])(
  "上次会话已删除时在 %s 清理记忆并回到新对话，不自动创建会话",
  async (hash) => {
    const { load, stream, create } = mockChat([]);
    localStorage.setItem(lastConversationKey, "deleted");
    history.replaceState(null, "", hash);
    renderChat();
    await waitFor(() =>
      expect(localStorage.getItem(lastConversationKey)).toBeNull(),
    );
    expect(screen.getByText("开始一段对话")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(location.hash).toBe("#ai");
    expect(load).toHaveBeenCalledExactlyOnceWith("deleted");
    expect(stream).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
  },
);

it.each([401, 503])("恢复遇到 HTTP %i 时保留记忆并显示错误", async (status) => {
  const { load, create } = mockChat([]);
  load.mockRejectedValueOnce(new ApiError("临时访问失败", status));
  localStorage.setItem(lastConversationKey, "remembered");
  history.replaceState(null, "", "#ai");
  renderChat();
  expect(await screen.findByRole("alert")).toHaveTextContent("临时访问失败");
  expect(localStorage.getItem(lastConversationKey)).toBe("remembered");
  expect(create).not.toHaveBeenCalled();
});

it("无效的指定会话链接显示错误，保留之前成功打开的会话记录", async () => {
  mockChat([]);
  localStorage.setItem(lastConversationKey, "remembered");
  history.replaceState(null, "", "#ai/missing-link");
  renderChat();
  expect(await screen.findByRole("alert")).toHaveTextContent("会话不存在");
  expect(localStorage.getItem(lastConversationKey)).toBe("remembered");
  expect(location.hash).toBe("#ai/missing-link");
});

it("恢复请求迟到时不能覆盖用户新打开的会话、记忆或订阅", async () => {
  const previous = conversation("previous", "原来的会话");
  const next = conversation("next", "新选择的会话");
  const { load, stream } = mockChat([previous, next]);
  let finish!: (detail: ConversationDetail) => void;
  load.mockImplementationOnce(
    () =>
      new Promise<ConversationDetail>((resolve) => {
        finish = resolve;
      }),
  );
  localStorage.setItem(lastConversationKey, previous.id);
  renderChat();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "展开会话列表" }));
  await user.click(
    await screen.findByRole("button", { name: /^新选择的会话/ }),
  );
  await expectOpened(next.title);
  await act(async () => finish(previous));
  await expectOpened(next.title);
  expect(
    screen.queryByText(`历史正文 ${previous.title}`),
  ).not.toBeInTheDocument();
  expect(localStorage.getItem(lastConversationKey)).toBe(next.id);
  expect(stream).toHaveBeenCalledExactlyOnceWith(
    next.id,
    7,
    expect.any(Function),
    expect.any(Function),
  );
});

it("删除当前会话清除记忆，下一次进入仍显示新对话", async () => {
  const previous = conversation("previous", "即将删除的会话");
  const { operate, stop } = mockChat([
    previous,
    conversation("other", "其他会话"),
  ]);
  localStorage.setItem(lastConversationKey, previous.id);
  const { unmount } = renderChat();
  await expectOpened(previous.title);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "展开会话列表" }));
  const drawer = screen.getByRole("dialog", { name: "会话列表" });
  await user.click(
    within(drawer).getByRole("button", { name: "删除 即将删除的会话" }),
  );
  await user.click(within(drawer).getByRole("button", { name: "确认删除" }));
  await waitFor(() =>
    expect(localStorage.getItem(lastConversationKey)).toBeNull(),
  );
  expect(operate).toHaveBeenCalledExactlyOnceWith("delete_conversation", {
    conversation_id: previous.id,
  });
  expect(stop).toHaveBeenCalledOnce();
  unmount();
  renderChat();
  await waitFor(() => expect(screen.getByText("开始一段对话")).toBeVisible());
  expect(localStorage.getItem(lastConversationKey)).toBeNull();
});

it.each(["button", "first-message"])(
  "通过 %s 创建的会话也会记住，恢复不重复发送",
  async (entry) => {
    const { create, send, created } = mockChat([]);
    const { unmount } = renderChat();
    if (entry === "button") {
      await userEvent
        .setup()
        .click(screen.getByRole("button", { name: "新建会话" }));
    } else {
      const input = screen.getByLabelText("发送消息");
      fireEvent.change(input, { target: { value: "安排今天的任务" } });
      fireEvent.submit(input.closest("form")!);
    }
    await expectOpened(created.title);
    expect(create).toHaveBeenCalledOnce();
    expect(localStorage.getItem(lastConversationKey)).toBe(created.id);
    expect(send).toHaveBeenCalledTimes(entry === "button" ? 0 : 1);
    unmount();
    history.replaceState(null, "", "#ai");
    renderChat();
    await expectOpened(created.title);
    expect(create).toHaveBeenCalledOnce();
    expect(send).toHaveBeenCalledTimes(entry === "button" ? 0 : 1);
  },
);
