import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api";
import ChatPage from "../ChatPage";
import { applyChatEvent, restoreChatState } from "../ChatTimeline";
import type { ChatState } from "../ChatTimeline";
import type { ChatEvent, ConversationDetail, PlatformState } from "../types";
import { platformState } from "./fixtures";

function event(
  seq: number,
  type: string,
  data: ChatEvent["data"],
  runId = "run-one",
): ChatEvent {
  return { seq, type, data, run_id: runId };
}

function timelineEvents(): ChatEvent[] {
  return [
    event(1, "run_started", { message_id: "assistant-one" }),
    event(2, "reasoning_delta", {
      message_id: "assistant-one",
      delta: "先确认当前连接",
    }),
    event(3, "text_delta", { message_id: "assistant-one", delta: "我会检查" }),
    event(4, "text_delta", {
      message_id: "assistant-one",
      delta: "在线机器。",
    }),
    event(5, "tool_start", {
      id: "reused-call",
      key: "run-one:0:reused-call",
      name: "get_agents",
      arguments: {},
    }),
    event(6, "tool_end", {
      id: "reused-call",
      key: "run-one:0:reused-call",
      name: "get_agents",
      result: { agents: ["worker"] },
    }),
    event(7, "reasoning_delta", {
      message_id: "assistant-one",
      delta: "机器在线，下一步需要派发",
    }),
    event(8, "text_delta", {
      message_id: "assistant-one",
      delta: "发现一台可用机器。",
    }),
    event(9, "tool_start", {
      id: "reused-call",
      key: "run-one:1:reused-call",
      name: "dispatch_task",
      arguments: { agent_id: "worker" },
    }),
    event(10, "approval_required", {
      id: "reused-call",
      key: "run-one:1:reused-call",
      name: "dispatch_task",
      arguments: { agent_id: "worker" },
      approval_id: "exact-approval",
    }),
  ];
}

function detail(events = timelineEvents()): ConversationDetail {
  return {
    id: "conversation-one",
    title: "顺序验收",
    source: "user",
    created_at: 0,
    updated_at: 0,
    messages: [
      {
        id: "assistant-one",
        role: "assistant",
        run_id: "run-one",
        content: "我会检查在线机器。发现一台可用机器。",
        reasoning: "先确认当前连接机器在线，下一步需要派发",
        created_at: 0,
      },
    ],
    runs: [
      {
        id: "run-one",
        conversation_id: "conversation-one",
        status: "awaiting_approval",
        created_at: 0,
        updated_at: 0,
      },
    ],
    last_event_seq: events.at(-1)?.seq ?? 0,
    events,
  };
}

function pendingState(): PlatformState {
  const state = platformState();
  state.approvals = [
    {
      id: "exact-approval",
      operation: "dispatch_task",
      arguments: { agent_id: "worker" },
      requested_by: "supervisor",
      status: "pending",
      reason: "请允许派发",
      created_at: 0,
      resolved_at: null,
      result: null,
    },
  ];
  return state;
}

async function openChat(
  snapshot: ConversationDetail,
): Promise<(event: ChatEvent) => void> {
  let receive!: (event: ChatEvent) => void;
  vi.spyOn(api, "conversations").mockResolvedValue([snapshot]);
  vi.spyOn(api, "conversation").mockResolvedValue(snapshot);
  vi.spyOn(api, "streamConversation").mockImplementation(
    (_id, _after, callback) => {
      receive = callback;
      return vi.fn();
    },
  );
  render(
    <ChatPage state={pendingState()} refresh={vi.fn()} onConfigure={vi.fn()} />,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "展开会话列表" }));
  await user.click(await screen.findByRole("button", { name: /^顺序验收/ }));
  await waitFor(() => expect(receive).toBeDefined());
  return receive;
}

function expectBefore(first: Element, second: Element): void {
  expect(
    first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
}

describe("聊天事件时间线", () => {
  it("刷新重建正文、分段思考和多轮工具的真实顺序，快照正文不会重复", async () => {
    await openChat(detail());
    const thoughts = screen.getAllByRole("button", { name: "思考过程" });
    expect(thoughts).toHaveLength(2);
    const before = screen.getByText("我会检查在线机器。");
    const middle = screen.getByText("发现一台可用机器。");
    const query = screen.getByRole("group", { name: "工具调用 get_agents" });
    const dispatch = screen.getByRole("group", {
      name: "工具调用 dispatch_task",
    });
    expectBefore(thoughts[0], before);
    expectBefore(before, query);
    expectBefore(query, thoughts[1]);
    expectBefore(thoughts[1], middle);
    expectBefore(middle, dispatch);
    expect(screen.getAllByText("我会检查在线机器。")).toHaveLength(1);
    expect(
      screen.queryByText("我会检查在线机器。发现一台可用机器。"),
    ).not.toBeInTheDocument();
    expect(api.streamConversation).toHaveBeenCalledWith(
      "conversation-one",
      10,
      expect.any(Function),
      expect.any(Function),
    );
    const user = userEvent.setup();
    await user.click(thoughts[0]);
    await user.click(thoughts[1]);
    expect(screen.getByText("先确认当前连接")).toBeVisible();
    expect(screen.getByText("机器在线，下一步需要派发")).toBeVisible();
  });

  it("流式更新工具原位置，审批恢复不重复，后续答案出现在工具之后", async () => {
    const receive = await openChat(detail());
    const dispatch = screen.getByRole("group", {
      name: "工具调用 dispatch_task",
    });
    const operate = vi
      .spyOn(api, "operate")
      .mockResolvedValue({ status: "approved" });
    await userEvent
      .setup()
      .click(within(dispatch).getByRole("button", { name: "允许这次操作" }));
    expect(operate).toHaveBeenCalledExactlyOnceWith("approve_operation", {
      approval_id: "exact-approval",
    });
    act(() => {
      receive(
        event(10, "approval_required", {
          id: "reused-call",
          name: "dispatch_task",
          approval_id: "wrong-approval",
        }),
      );
      receive(
        event(11, "tool_start", {
          id: "reused-call",
          key: "run-one:1:reused-call",
          name: "dispatch_task",
          arguments: { agent_id: "worker" },
        }),
      );
      receive(
        event(12, "tool_end", {
          id: "reused-call",
          key: "run-one:1:reused-call",
          name: "dispatch_task",
          result: { task_id: "created-task" },
        }),
      );
      receive(
        event(13, "text_delta", {
          message_id: "assistant-one",
          delta: "任务已派发。",
        }),
      );
      receive(
        event(13, "text_delta", {
          message_id: "assistant-one",
          delta: "任务已派发。",
        }),
      );
      receive(event(14, "completed", {}));
    });
    expect(screen.getByRole("group", { name: "工具调用 dispatch_task" })).toBe(
      dispatch,
    );
    expect(screen.getAllByRole("group", { name: /工具调用/ })).toHaveLength(2);
    expectBefore(dispatch, screen.getByText("任务已派发。"));
    expect(screen.getAllByText("任务已派发。")).toHaveLength(1);
    expect(within(dispatch).getByText("已完成")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "允许这次操作" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "停止运行" }),
    ).not.toBeInTheDocument();
  });

  it("同一轮多个工具保持各自首次出现位置，结果逆序到达也不移动", async () => {
    const snapshot = detail([]);
    snapshot.messages = [];
    snapshot.runs = [];
    const receive = await openChat(snapshot);
    act(() => {
      receive(event(1, "run_started", { message_id: "assistant-one" }));
      receive(
        event(2, "text_delta", {
          message_id: "assistant-one",
          delta: "并行检查。",
        }),
      );
      receive(
        event(3, "tool_start", {
          id: "a",
          key: "run-one:0:a",
          name: "get_agents",
          arguments: {},
        }),
      );
      receive(
        event(4, "tool_start", {
          id: "b",
          key: "run-one:0:b",
          name: "get_tasks",
          arguments: {},
        }),
      );
      receive(
        event(5, "tool_end", {
          id: "b",
          key: "run-one:0:b",
          name: "get_tasks",
          result: {},
        }),
      );
      receive(
        event(6, "tool_end", {
          id: "a",
          key: "run-one:0:a",
          name: "get_agents",
          result: {},
        }),
      );
      receive(
        event(7, "text_delta", {
          message_id: "assistant-one",
          delta: "检查完成。",
        }),
      );
    });
    const query = screen.getByRole("group", { name: "工具调用 get_agents" });
    const tasks = screen.getByRole("group", { name: "工具调用 get_tasks" });
    expectBefore(screen.getByText("并行检查。"), query);
    expectBefore(query, tasks);
    expectBefore(tasks, screen.getByText("检查完成。"));
  });

  it("同 run 复用调用 ID 与其它 run 同 ID 都保留独立参数、结果和审批", () => {
    const events = [
      event(1, "tool_start", {
        id: "shared",
        key: "run-one:0:shared",
        name: "dispatch_task",
        arguments: { target: "first" },
      }),
      event(2, "tool_end", {
        id: "shared",
        key: "run-one:0:shared",
        name: "dispatch_task",
        result: { task_id: "first" },
      }),
      event(3, "tool_start", {
        id: "shared",
        key: "run-one:1:shared",
        name: "dispatch_task",
        arguments: { target: "second" },
      }),
      event(4, "approval_required", {
        id: "shared",
        key: "run-one:1:shared",
        name: "dispatch_task",
        approval_id: "second-approval",
      }),
      event(
        5,
        "approval_required",
        {
          id: "shared",
          key: "run-two:0:shared",
          name: "dispatch_task",
          arguments: { target: "third" },
          approval_id: "third-approval",
        },
        "run-two",
      ),
    ];
    const state = events.reduce(applyChatEvent, {
      messages: [],
      runs: [],
      tools: [],
    } as ChatState);
    expect(state.tools).toHaveLength(3);
    expect(state.tools).toMatchObject([
      {
        arguments: { target: "first" },
        result: { task_id: "first" },
        status: "completed",
      },
      {
        arguments: { target: "second" },
        approval_id: "second-approval",
        status: "approval",
      },
      {
        arguments: { target: "third" },
        approval_id: "third-approval",
        status: "approval",
      },
    ]);
    expect(state.parts).toHaveLength(3);
  });

  it.each(["cancelled", "interrupted", "error"])(
    "%s 保留已到达片段和原工具位置，停止等待许可",
    async (type) => {
      const receive = await openChat(detail());
      const dispatch = screen.getByRole("group", {
        name: "工具调用 dispatch_task",
      });
      act(() =>
        receive(
          event(11, type, type === "error" ? { message: "模型连接中断" } : {}),
        ),
      );
      expect(screen.getByText("我会检查在线机器。")).toBeVisible();
      expect(
        screen.getByRole("group", { name: "工具调用 dispatch_task" }),
      ).toBe(dispatch);
      expect(
        screen.queryByRole("button", { name: "允许这次操作" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "停止运行" }),
      ).not.toBeInTheDocument();
      expect(
        within(dispatch).getByText(
          type === "cancelled"
            ? "已取消"
            : type === "interrupted"
              ? "已中断"
              : "失败",
        ),
      ).toBeVisible();
    },
  );

  it("快照聚合正文只保留一份，忽略快照前的重传并继续后续增量", () => {
    const snapshot = detail(timelineEvents().slice(0, 4));
    snapshot.messages[0].content = "我会检查在线机器。";
    snapshot.messages[0].reasoning = "先确认当前连接";
    const restored = restoreChatState(snapshot);
    expect(restored.messages[0].content).toBe("我会检查在线机器。");
    expect(applyChatEvent(restored, snapshot.events![2])).toBe(restored);
    const updated = applyChatEvent(
      restored,
      event(5, "text_delta", { message_id: "assistant-one", delta: "稍等。" }),
    );
    expect(updated.messages[0].content).toBe("我会检查在线机器。稍等。");
    expect(updated.parts?.filter((part) => part.kind === "text")).toMatchObject(
      [{ content: "我会检查在线机器。稍等。" }],
    );
  });
});
