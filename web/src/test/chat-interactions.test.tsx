import { useRef, useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { MockInstance } from "vitest";
import ChatPage from "../ChatPage";
import { ChatComposer, MessageCopy } from "../ChatUI";
import { api } from "../api";
import type { ChatEvent, ConversationDetail } from "../types";
import { platformState } from "./fixtures";

type ComposerFlags = Partial<
  Pick<
    ComponentProps<typeof ChatComposer>,
    "configured" | "busy" | "waitingForConversation" | "active"
  >
>;

function ComposerHarness({
  submitted,
  cancelled = () => {},
  initialContent = "查看任务进度",
  ...flags
}: ComposerFlags & {
  submitted: (content: string) => void;
  cancelled?: () => void;
  initialContent?: string;
}): ReactNode {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [content, setContent] = useState(initialContent);
  return (
    <ChatComposer
      inputRef={inputRef}
      content={content}
      onChange={setContent}
      onSubmit={(event) => {
        event.preventDefault();
        submitted(content);
      }}
      onCancel={cancelled}
      configured
      busy={false}
      waitingForConversation={false}
      active={false}
      model="test-model"
      {...flags}
    />
  );
}

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

function mockChat(details: ConversationDetail[]): {
  receive: Map<string, (event: ChatEvent) => void>;
  send: MockInstance<typeof api.sendMessage>;
  create: MockInstance<typeof api.createConversation>;
} {
  const receive = new Map<string, (event: ChatEvent) => void>();
  vi.spyOn(api, "conversations").mockResolvedValue(details);
  vi.spyOn(api, "conversation").mockImplementation(async (id) => {
    const found = details.find((detail) => detail.id === id);
    if (!found) throw new Error("测试会话不存在");
    return found;
  });
  vi.spyOn(api, "streamConversation").mockImplementation(
    (id, _after, callback) => {
      receive.set(id, callback);
      return vi.fn();
    },
  );
  const send = vi.spyOn(api, "sendMessage").mockImplementation(async (id) => ({
    conversation_id: id,
    run_id: `run:${id}`,
  }));
  const create = vi
    .spyOn(api, "createConversation")
    .mockResolvedValue(conversation("new-created", "新会话"));
  return { receive, send, create };
}

function renderChat(): void {
  render(
    <ChatPage
      state={platformState()}
      refresh={vi.fn()}
      onConfigure={vi.fn()}
    />,
  );
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

async function selectConversation(
  user: ReturnType<typeof userEvent.setup>,
  detail: ConversationDetail,
): Promise<void> {
  await user.click(screen.getByRole("button", { name: "展开会话列表" }));
  await user.click(
    await screen.findByRole("button", { name: new RegExp(detail.title) }),
  );
  await waitFor(() =>
    expect(
      screen.getByText(detail.title, {
        selector: ".chat-conversation-title",
      }),
    ).toBeVisible(),
  );
}

describe("聊天输入快捷键", () => {
  it.each([
    ["Enter", "{Enter}"],
    ["Ctrl + Enter", "{Control>}{Enter}{/Control}"],
    ["⌘ + Enter", "{Meta>}{Enter}{/Meta}"],
  ])("%s 通过表单发送真实草稿一次", async (_name, keys) => {
    const submitted = vi.fn();
    render(<ComposerHarness submitted={submitted} />);
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("发送消息"));
    await user.keyboard(keys);
    expect(submitted).toHaveBeenCalledExactlyOnceWith("查看任务进度");
  });

  it("Shift + Enter 插入换行并继续编辑，不触发发送", async () => {
    const submitted = vi.fn();
    render(<ComposerHarness submitted={submitted} />);
    const user = userEvent.setup();
    const input = screen.getByLabelText("发送消息");
    await user.click(input);
    await user.keyboard("{Shift>}{Enter}{/Shift}补充下一行");
    expect(input).toHaveValue("查看任务进度\n补充下一行");
    expect(submitted).not.toHaveBeenCalled();
  });

  it.each([{ isComposing: true }, { isComposing: false, keyCode: 229 }])(
    "输入法组合或候选确认事件不提交，组合结束后可发送（%j）",
    async (native) => {
      const submitted = vi.fn();
      render(
        <ComposerHarness submitted={submitted} initialContent="待确认的中文" />,
      );
      const input = screen.getByLabelText("发送消息");
      input.focus();
      fireEvent.compositionStart(input);
      fireEvent.keyDown(input, { key: "Enter", code: "Enter", ...native });
      expect(submitted).not.toHaveBeenCalled();
      expect(input).toHaveValue("待确认的中文");
      fireEvent.compositionEnd(input);
      await userEvent.setup().keyboard("{Enter}");
      expect(submitted).toHaveBeenCalledExactlyOnceWith("待确认的中文");
    },
  );

  it.each([
    ["未配置模型", { configured: false }],
    ["正在提交", { busy: true }],
    ["等待会话读取", { waitingForConversation: true }],
    ["模型仍在运行", { active: true }],
  ] as [string, ComposerFlags][])(
    "%s 时 Enter 不发送",
    async (_name, flags) => {
      const submitted = vi.fn();
      render(<ComposerHarness submitted={submitted} {...flags} />);
      const input = screen.getByLabelText("发送消息");
      fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
      if (!flags.active) {
        const send = screen.getByRole("button", {
          name: flags.busy ? "发送中…" : "发送 ↑",
        });
        expect(send).toBeDisabled();
        await userEvent.setup().click(send);
      }
      expect(submitted).not.toHaveBeenCalled();
    },
  );

  it("空白草稿不发送，运行中仍可编辑草稿并明确停止", async () => {
    const submitted = vi.fn();
    const cancelled = vi.fn();
    const { rerender } = render(
      <ComposerHarness submitted={submitted} initialContent={" \n "} />,
    );
    const user = userEvent.setup();
    const input = screen.getByLabelText("发送消息");
    await user.click(input);
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeDisabled();
    expect(submitted).not.toHaveBeenCalled();
    rerender(
      <ComposerHarness submitted={submitted} cancelled={cancelled} active />,
    );
    await user.clear(input);
    await user.type(input, "下一条工作安排");
    await user.keyboard("{Enter}");
    expect(input).toHaveValue("下一条工作安排");
    expect(submitted).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "停止运行" }));
    expect(cancelled).toHaveBeenCalledOnce();
  });
});

describe("回复复制", () => {
  it("复制完整 Markdown 原文，保留换行、代码与链接", async () => {
    const user = userEvent.setup();
    const clipboard = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue();
    const content =
      "## 当前进度\n\n**已经完成**。\n\n```python\nprint('你好')\n```\n\n[任务记录](https://example.test/tasks/one)";
    render(<MessageCopy content={content} />);
    await user.click(screen.getByRole("button", { name: "复制回复" }));
    expect(clipboard).toHaveBeenCalledExactlyOnceWith(content);
    expect(screen.getByRole("button", { name: "回复已复制" })).toHaveAttribute(
      "title",
      "已复制",
    );
  });

  it("剪贴板失败有明确反馈，重试成功后清除失败提示", async () => {
    const user = userEvent.setup();
    const clipboard = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockRejectedValueOnce(new Error("clipboard unavailable"))
      .mockResolvedValueOnce();
    render(<MessageCopy content="真实的回复内容" />);
    await user.click(screen.getByRole("button", { name: "复制回复" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "无法访问剪贴板，请手动复制。",
    );
    await user.click(screen.getByRole("button", { name: "复制回复" }));
    expect(clipboard).toHaveBeenCalledTimes(2);
    expect(clipboard).toHaveBeenLastCalledWith("真实的回复内容");
    expect(screen.getByRole("button", { name: "回复已复制" })).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("再次复制失败时清除上一轮成功标记，不把失败显示成已复制", async () => {
    const user = userEvent.setup();
    const clipboard = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValueOnce()
      .mockRejectedValueOnce(new Error("clipboard unavailable"));
    render(<MessageCopy content="要复制的回复" />);
    await user.click(screen.getByRole("button", { name: "复制回复" }));
    await user.click(screen.getByRole("button", { name: "回复已复制" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "无法访问剪贴板，请手动复制。",
    );
    expect(screen.getByRole("button", { name: "复制回复" })).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "回复已复制" }),
    ).not.toBeInTheDocument();
    expect(clipboard).toHaveBeenCalledTimes(2);
  });
});

describe("会话草稿与阅读位置", () => {
  it("快捷建议只填草稿并聚焦，不创建会话或发送请求", async () => {
    const { send, create } = mockChat([]);
    renderChat();
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "有哪些任务需要关注？" }));
    const input = screen.getByLabelText("发送消息");
    expect(input).toHaveValue("有哪些任务需要关注？");
    await waitFor(() => expect(input).toHaveFocus());
    expect(send).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
  });

  it("切换会话后各自恢复未发送草稿，不把草稿提交给模型", async () => {
    const first = conversation("first", "第一段会话");
    const second = conversation("second", "第二段会话");
    const { send } = mockChat([first, second]);
    const user = userEvent.setup();
    renderChat();
    await selectConversation(user, first);
    await user.type(screen.getByLabelText("发送消息"), "第一段草稿");
    await selectConversation(user, second);
    expect(screen.getByLabelText("发送消息")).toHaveValue("");
    await user.type(screen.getByLabelText("发送消息"), "第二段草稿");
    await selectConversation(user, first);
    expect(screen.getByLabelText("发送消息")).toHaveValue("第一段草稿");
    await selectConversation(user, second);
    expect(screen.getByLabelText("发送消息")).toHaveValue("第二段草稿");
    expect(send).not.toHaveBeenCalled();
  });

  it("已有会话受理期间保留继续起草的内容，阻止切换，受理后运行中可切换", async () => {
    const first = conversation("accepting-first", "正在受理的会话");
    const second = conversation("accepting-second", "另一个会话");
    const { send } = mockChat([first, second]);
    const acceptance = deferred<Awaited<ReturnType<typeof api.sendMessage>>>();
    send.mockReturnValueOnce(acceptance.promise);
    const user = userEvent.setup();
    renderChat();
    await selectConversation(user, first);
    const input = screen.getByLabelText("发送消息");
    await user.type(input, "提交给第一个会话的指令");
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(send).toHaveBeenCalledExactlyOnceWith(
        first.id,
        "提交给第一个会话的指令",
      ),
    );
    expect(screen.getByRole("button", { name: "发送中…" })).toBeDisabled();

    await user.clear(input);
    await user.type(input, "受理期间写的下一条草稿");
    await user.click(screen.getByRole("button", { name: "展开会话列表" }));
    const nextConversation = screen.getByRole("button", { name: /另一个会话/ });
    expect(nextConversation).toBeDisabled();
    expect(screen.getByRole("button", { name: "新建会话" })).toBeDisabled();
    await user.click(nextConversation);
    expect(api.conversation).not.toHaveBeenCalledWith(second.id);

    await act(async () =>
      acceptance.resolve({
        conversation_id: first.id,
        run_id: "accepting-first-run",
      }),
    );
    expect(input).toHaveValue("受理期间写的下一条草稿");
    expect(screen.getByRole("button", { name: "停止运行" })).toBeEnabled();
    expect(nextConversation).toBeEnabled();
    await user.click(nextConversation);
    await waitFor(() =>
      expect(api.conversation).toHaveBeenCalledWith(second.id),
    );
    expect(
      screen.getByText(second.title, { selector: ".chat-conversation-title" }),
    ).toBeVisible();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("新会话首次发送受理期间修改草稿，响应返回后保留新内容并只发送原指令", async () => {
    const created = conversation("new-accepting", "首次发送的会话");
    created.messages = [
      {
        id: "accepted-user-message",
        role: "user",
        content: "第一条指令",
        reasoning: "",
        created_at: 0,
        run_id: "new-accepting-run",
      },
    ];
    created.runs = [
      {
        id: "new-accepting-run",
        conversation_id: created.id,
        status: "running",
        created_at: 0,
        updated_at: 0,
      },
    ];
    const { send, create } = mockChat([created]);
    vi.mocked(api.conversations).mockResolvedValue([]);
    create.mockResolvedValue(created);
    const acceptance = deferred<Awaited<ReturnType<typeof api.sendMessage>>>();
    send.mockReturnValueOnce(acceptance.promise);
    const user = userEvent.setup();
    renderChat();
    const input = screen.getByLabelText("发送消息");
    await user.type(input, "第一条指令");
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(send).toHaveBeenCalledExactlyOnceWith(created.id, "第一条指令"),
    );

    await user.click(screen.getByRole("button", { name: "检查平台配置" }));
    expect(input).toHaveValue("检查平台配置");
    await user.type(input, "，下一轮再执行");
    await act(async () =>
      acceptance.resolve({
        conversation_id: created.id,
        run_id: "new-accepting-run",
      }),
    );
    expect(await screen.findByText("第一条指令")).toBeVisible();
    expect(input).toHaveValue("检查平台配置，下一轮再执行");
    expect(screen.getByRole("button", { name: "停止运行" })).toBeEnabled();
    await user.click(input);
    await user.keyboard("{Enter}");
    expect(send).toHaveBeenCalledTimes(1);
    expect(create).toHaveBeenCalledOnce();
  });

  it("读历史时流式输出不抢滚动，点击回到最新后重新跟随", async () => {
    const detail = conversation("streaming", "正在进行的会话");
    detail.messages = [
      {
        id: "reply",
        role: "assistant",
        content: "已有正文",
        reasoning: "",
        created_at: 0,
        run_id: "run-streaming",
      },
    ];
    detail.runs = [
      {
        id: "run-streaming",
        conversation_id: detail.id,
        status: "running",
        created_at: 0,
        updated_at: 0,
      },
    ];
    const { receive } = mockChat([detail]);
    const user = userEvent.setup();
    renderChat();
    await selectConversation(user, detail);
    expect(await screen.findByText("已有正文")).toBeVisible();
    const scroll = screen
      .getByRole("region", { name: "与 Chiliarch AI 对话" })
      .querySelector<HTMLDivElement>(".message-scroll")!;
    Object.defineProperties(scroll, {
      scrollHeight: { configurable: true, value: 1400 },
      clientHeight: { configurable: true, value: 400 },
      scrollTop: { configurable: true, writable: true, value: 120 },
    });
    const scrolling = vi.spyOn(scroll, "scrollTo");
    scrolling.mockClear();
    fireEvent.scroll(scroll);
    expect(screen.getByRole("button", { name: "回到最新消息" })).toBeVisible();

    const sendDelta = (seq: number, delta: string): void => {
      act(() =>
        receive.get(detail.id)?.({
          seq,
          type: "text_delta",
          run_id: "run-streaming",
          data: { message_id: "reply", delta },
        }),
      );
    };
    sendDelta(1, " 新增片段");
    expect(screen.getByText("已有正文 新增片段")).toBeVisible();
    expect(scrolling).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "回到最新消息" }));
    expect(scrolling).toHaveBeenLastCalledWith({
      top: 1400,
      behavior: "instant",
    });
    expect(
      screen.queryByRole("button", { name: "回到最新消息" }),
    ).not.toBeInTheDocument();
    scrolling.mockClear();
    sendDelta(2, " 后续输出");
    expect(scrolling).toHaveBeenCalledExactlyOnceWith({
      top: 1400,
      behavior: "instant",
    });
  });
});
