import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import CollaborationPage from "../CollaborationPage";
import ConfigPage from "../ConfigPage";
import { api, ApiError } from "../api";
import type {
  Agent,
  CollaborationChannel,
  JsonObject,
  PeerMessage,
} from "../types";
import { platformState } from "./fixtures";

const agents: Agent[] = [
  {
    id: "codex",
    name: "Codex",
    machine: "linux",
    capabilities: [],
    revoked: false,
    online: true,
    created_at: 1,
    last_seen: 1,
    metadata: { execution_mode: "managed" },
  },
  {
    id: "claude",
    name: "Claude",
    machine: "mac",
    capabilities: [],
    revoked: false,
    online: true,
    created_at: 1,
    last_seen: 1,
    metadata: { execution_mode: "manual" },
  },
];
const channel: CollaborationChannel = {
  id: "discussion",
  title: "登录功能协作",
  members: ["codex", "claude"],
  created_by: "user",
  goal_id: null,
  created_at: 1,
};
function message(sequence: number, text = `进展 ${sequence}`): PeerMessage {
  return {
    id: `message-${sequence}`,
    sequence,
    sender_agent_id: "codex",
    sender_role: "agent",
    source_task_id: null,
    channel_id: channel.id,
    recipient_agent_id: null,
    recipient_task_id: null,
    message: text,
    reply_to: null,
    created_at: sequence,
  };
}
function state() {
  return { ...platformState(), agents };
}

function mockOperations(
  overrides: Record<string, (args: JsonObject) => unknown> = {},
) {
  return vi.spyOn(api, "operate").mockImplementation(async (name, args) => {
    if (overrides[name]) return (await overrides[name](args)) as never;
    if (name === "list_channels") return { channels: [channel] } as never;
    if (name === "list_task_waits") return { waits: [] } as never;
    if (name === "get_channel")
      return { channel, messages: [], cursor: 0 } as never;
    if (name === "get_peer_message")
      return { message: message(1), receipts: [] } as never;
    throw new Error(`Unexpected operation ${name}`);
  });
}
afterEach(() => vi.useRealTimers());

describe("协作工作区", () => {
  it("历史分页未追完时立即展示已确认发送的消息，不跳过历史 cursor 且后续不重复", async () => {
    const history = Array.from({ length: 200 }, (_, index) =>
      message(index + 1),
    );
    const sent = message(201, "这是刚发送的最新消息");
    let saved = false;
    const operate = mockOperations({
      get_channel: (args) => {
        const all = saved ? [...history, sent] : history;
        const page = all
          .filter((item) => item.sequence > Number(args.after))
          .slice(0, Number(args.limit));
        return {
          channel,
          messages: page,
          cursor: page.at(-1)?.sequence ?? args.after,
        };
      },
      send_peer_message: () => {
        saved = true;
        return { message: sent };
      },
    });
    const user = userEvent.setup();
    render(<CollaborationPage state={state()} refresh={vi.fn()} />);
    await screen.findByRole("button", { name: "读取后续消息" });
    await user.type(screen.getByLabelText("消息内容"), sent.message);
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByText(sent.message)).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("消息已发送");
    await waitFor(() =>
      expect(operate).toHaveBeenCalledWith("get_channel", {
        channel_id: channel.id,
        after: 100,
        limit: 100,
      }),
    );
    expect(await screen.findByText("进展 200")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "读取后续消息" }));
    await waitFor(() =>
      expect(operate).toHaveBeenCalledWith("get_channel", {
        channel_id: channel.id,
        after: 200,
        limit: 100,
      }),
    );
    expect(screen.getAllByRole("article")).toHaveLength(201);
    expect(screen.getAllByText(sent.message)).toHaveLength(1);
  });

  it("任务定向私信只允许已完成且目标未关闭的任务唤醒接续，其他状态可只发送消息", async () => {
    const operate = mockOperations({
      send_peer_message: (args) => ({
        message: {
          ...message(1),
          recipient_agent_id: args.recipient_agent_id,
          channel_id: null,
        },
      }),
    });
    const current = state();
    current.tasks = ["running", "completed", "failed", "closed"].map((id) => ({
      id,
      agent_id: "codex",
      instruction: `任务 ${id}`,
      status: id === "closed" ? "completed" : id,
      parameters: {},
      progress: null,
      result: null,
      error: null,
      created_at: 1,
      updated_at: 1,
      requested_by: "user",
    }));
    current.goals = [
      {
        id: "closed-goal",
        title: "已完成目标",
        objective: "完成",
        status: "completed",
        constraints: [],
        acceptance_criteria: [],
        conversation_id: null,
        summary: "",
        created_at: 1,
        updated_at: 1,
      },
    ];
    current.task_links = [
      {
        task_id: "closed",
        goal_id: "closed-goal",
        conversation_id: null,
        depends_on: [],
        acceptance_criteria: [],
        review_status: "accepted",
        review_note: "",
        artifacts: [],
        retry_of: null,
      },
    ];
    const user = userEvent.setup();
    render(<CollaborationPage state={current} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "发送私信" }));
    const dialog = screen.getByRole("dialog", { name: "发送私信" });
    const target = within(dialog).getByLabelText("关联接收方任务（可选）");
    const wake = within(dialog).getByLabelText("唤醒 agent 处理来信");
    await user.selectOptions(target, "running");
    expect(wake).toBeDisabled();
    expect(within(dialog).getByText("运行中")).toBeVisible();
    await user.type(
      within(dialog).getByLabelText("消息内容"),
      "给当前任务补充信息",
    );
    await user.click(within(dialog).getByRole("button", { name: "发送消息" }));
    expect(operate).toHaveBeenCalledWith("send_peer_message", {
      recipient_agent_id: "codex",
      recipient_task_id: "running",
      wake: false,
      message: "给当前任务补充信息",
    });
    await user.selectOptions(target, "failed");
    expect(wake).toBeDisabled();
    await user.selectOptions(target, "completed");
    expect(wake).toBeEnabled();
    await user.click(wake);
    await user.selectOptions(target, "closed");
    expect(wake).toBeDisabled();
    expect(wake).not.toBeChecked();
    expect(within(dialog).getByText(/任务所属目标已关闭/)).toBeVisible();
    await user.selectOptions(target, "completed");
    expect(wake).not.toBeChecked();
    await user.click(wake);
    await user.type(within(dialog).getByLabelText("消息内容"), "继续处理结果");
    await user.click(within(dialog).getByRole("button", { name: "发送消息" }));
    expect(operate).toHaveBeenCalledWith("send_peer_message", {
      recipient_agent_id: "codex",
      recipient_task_id: "completed",
      wake: true,
      message: "继续处理结果",
    });
  });

  it("按消息顺序展示讨论，回复携带关联 ID，发送失败保留草稿后可重试", async () => {
    let attempt = 0;
    const operate = mockOperations({
      get_channel: () => ({
        channel,
        messages: [message(2, "评审完成"), message(1, "开始实现")],
        cursor: 2,
      }),
      send_peer_message: () => {
        if (!attempt++) throw new ApiError("队列暂时不可用", 503);
        return { message: message(3, "收到，谢谢") };
      },
    });
    const user = userEvent.setup();
    render(<CollaborationPage state={state()} refresh={vi.fn()} />);
    const messages = await screen.findByRole("list", { name: "讨论消息" });
    await within(messages).findByText("开始实现");
    expect(
      within(messages)
        .getAllByRole("article")
        .map((item) => item.getAttribute("aria-label")),
    ).toEqual(["消息 1", "消息 2"]);
    await user.click(
      within(screen.getByRole("article", { name: "消息 1" })).getByRole(
        "button",
        { name: "回复" },
      ),
    );
    await user.type(screen.getByLabelText("消息内容"), "收到，谢谢");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "队列暂时不可用",
    );
    expect(screen.getByLabelText("消息内容")).toHaveValue("收到，谢谢");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() =>
      expect(screen.getByLabelText("消息内容")).toHaveValue(""),
    );
    expect(operate).toHaveBeenCalledWith("send_peer_message", {
      channel_id: channel.id,
      message: "收到，谢谢",
      reply_to: "message-1",
    });
    expect(
      screen.queryByRole("button", { name: "取消回复" }),
    ).not.toBeInTheDocument();
  });

  it("创建讨论选择成员和目标，成功后打开新讨论", async () => {
    const created = {
      ...channel,
      id: "new",
      title: "集成验收",
      members: ["claude"],
      goal_id: "goal-one",
    };
    let saved = false;
    const operate = mockOperations({
      list_channels: () => ({
        channels: saved ? [channel, created] : [channel],
      }),
      create_channel: () => {
        saved = true;
        return { channel: created };
      },
    });
    const current = state();
    current.goals = [
      {
        id: "goal-one",
        title: "交付登录",
        objective: "验收",
        constraints: [],
        acceptance_criteria: [],
        status: "active",
        conversation_id: null,
        summary: "",
        created_at: 1,
        updated_at: 1,
      },
    ];
    const user = userEvent.setup();
    render(<CollaborationPage state={current} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "＋ 新建讨论" }));
    const dialog = screen.getByRole("dialog", { name: "新建讨论" });
    expect(
      within(dialog).getByRole("button", { name: "创建讨论" }),
    ).toBeDisabled();
    await user.type(within(dialog).getByLabelText("讨论名称"), "集成验收");
    await user.click(within(dialog).getByRole("checkbox", { name: /Claude/ }));
    await user.selectOptions(
      within(dialog).getByLabelText("关联目标（可选）"),
      "goal-one",
    );
    await user.click(within(dialog).getByRole("button", { name: "创建讨论" }));
    expect(operate).toHaveBeenCalledWith("create_channel", {
      title: "集成验收",
      members: ["claude"],
      goal_id: "goal-one",
    });
    expect(
      await screen.findByRole("region", { name: "讨论 集成验收" }),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: "目标：交付登录" }),
    ).toHaveAttribute("href", "#goals/goal-one");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("私信可明确唤醒受管执行器，切换 manual 接收方时取消唤醒并展示持久回执", async () => {
    const operate = mockOperations({
      send_peer_message: (args) => ({
        message: {
          ...message(1),
          recipient_agent_id: args.recipient_agent_id,
          channel_id: null,
        },
      }),
      get_peer_message: () => ({
        message: message(1),
        receipts: [
          {
            agent_id: "claude",
            state: "read",
            wake_task_id: null,
            updated_at: 2,
          },
        ],
      }),
    });
    const user = userEvent.setup();
    render(<CollaborationPage state={state()} refresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "发送私信" }));
    const dialog = screen.getByRole("dialog", { name: "发送私信" });
    const wake = within(dialog).getByLabelText("唤醒 agent 处理来信");
    await user.click(wake);
    await user.type(within(dialog).getByLabelText("消息内容"), "请评审");
    await user.click(within(dialog).getByRole("button", { name: "发送消息" }));
    expect(operate).toHaveBeenCalledWith("send_peer_message", {
      recipient_agent_id: "codex",
      wake: true,
      message: "请评审",
    });
    await user.selectOptions(
      within(dialog).getByLabelText("接收 agent"),
      "claude",
    );
    expect(wake).toBeDisabled();
    expect(wake).not.toBeChecked();
    expect(within(dialog).getByText(/需由现有会话主动读取/)).toBeVisible();
    await user.type(within(dialog).getByLabelText("消息内容"), "补充资料");
    await user.click(within(dialog).getByRole("button", { name: "发送消息" }));
    expect(operate).toHaveBeenCalledWith("send_peer_message", {
      recipient_agent_id: "claude",
      wake: false,
      message: "补充资料",
    });
    expect(await within(dialog).findByText("已读取")).toBeVisible();
    expect(within(dialog).getByRole("status")).toHaveTextContent(
      "消息已发送给 Claude",
    );
  });

  it("展示谁在等谁及原因，消息中的 HTML 无法生成可执行节点", async () => {
    mockOperations({
      list_task_waits: () => ({
        waits: [
          {
            task_id: "task-a",
            waiting_for: ["task-b"],
            reason: "等待接口评审",
            updated_at: 2,
          },
        ],
      }),
      get_channel: () => ({
        channel,
        messages: [
          message(
            1,
            '进展 <img src="x" onerror="alert(1)" /><script>alert(1)</script>',
          ),
        ],
        cursor: 1,
      }),
    });
    const current = state();
    current.tasks = [
      {
        id: "task-a",
        agent_id: "codex",
        instruction: "实现登录",
        parameters: {},
        status: "running",
        progress: null,
        result: null,
        error: null,
        created_at: 1,
        updated_at: 2,
        requested_by: "user",
      },
      {
        id: "task-b",
        agent_id: "claude",
        instruction: "评审接口",
        parameters: {},
        status: "running",
        progress: null,
        result: null,
        error: null,
        created_at: 1,
        updated_at: 2,
        requested_by: "user",
      },
    ];
    render(<CollaborationPage state={current} refresh={vi.fn()} />);
    const waits = screen.getByRole("region", { name: "任务等待关系" });
    expect(await within(waits).findByText("Codex · 实现登录")).toBeVisible();
    expect(within(waits).getByText("Claude · 评审接口")).toBeVisible();
    expect(within(waits).getByText("等待接口评审")).toBeVisible();
    const article = await screen.findByRole("article", { name: "消息 1" });
    expect(article.querySelector("img, script")).toBeNull();
  });

  it("切换讨论后忽略旧请求响应，离开页面后停止轮询", async () => {
    const other = { ...channel, id: "other", title: "另一讨论" };
    let resolveOld: ((value: unknown) => void) | undefined;
    const operate = mockOperations({
      list_channels: () => ({ channels: [channel, other] }),
      get_channel: (args) =>
        args.channel_id === channel.id
          ? new Promise((resolve) => {
              resolveOld = resolve;
            })
          : {
              channel: other,
              messages: [message(3, "当前讨论内容")],
              cursor: 3,
            },
    });
    const user = userEvent.setup();
    const view = render(
      <CollaborationPage state={state()} refresh={vi.fn()} />,
    );
    await user.click(await screen.findByRole("button", { name: /另一讨论/ }));
    expect(await screen.findByText("当前讨论内容")).toBeVisible();
    await act(async () => {
      resolveOld?.({
        channel,
        messages: [message(1, "过期讨论内容")],
        cursor: 1,
      });
    });
    expect(screen.queryByText("过期讨论内容")).not.toBeInTheDocument();
    vi.useFakeTimers();
    view.unmount();
    const calls = operate.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15000);
    });
    expect(operate.mock.calls.length).toBe(calls);
  });

  it("增量轮询携带 cursor，合并重复消息后保持顺序，卸载清除定时器", async () => {
    vi.useFakeTimers();
    const operate = mockOperations({
      get_channel: (args) => ({
        channel,
        messages: args.after === 0 ? [message(1)] : [message(1), message(2)],
        cursor: args.after === 0 ? 1 : 2,
      }),
    });
    const view = render(
      <CollaborationPage state={state()} refresh={vi.fn()} />,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getAllByRole("article")).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(operate).toHaveBeenCalledWith("get_channel", {
      channel_id: channel.id,
      after: 1,
      limit: 100,
    });
    expect(
      screen
        .getAllByRole("article")
        .map((item) => item.getAttribute("aria-label")),
    ).toEqual(["消息 1", "消息 2"]);
    view.unmount();
    const calls = operate.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(operate).toHaveBeenCalledTimes(calls);
  });
});

it("总管恢复配置读取服务器数值，仅提交用户修改的数字字段", async () => {
  const current = state();
  current.config.supervisor = {
    mode: "auto",
    task_stall_seconds: 300,
    model_recovery_attempts: 3,
    model_recovery_base_seconds: 5,
    model_recovery_max_seconds: 60,
  };
  const operate = vi
    .spyOn(api, "operate")
    .mockResolvedValue({ config: current.config, revision: 2 });
  const user = userEvent.setup();
  render(<ConfigPage state={current} refresh={vi.fn()} />);
  expect(screen.getByLabelText("任务进度停滞判定（秒）")).toHaveValue(300);
  expect(screen.getByLabelText("模型故障自动恢复次数")).toHaveValue(3);
  expect(screen.getByLabelText("模型恢复起始等待（秒）")).toHaveValue(5);
  expect(screen.getByLabelText("模型恢复最长等待（秒）")).toHaveValue(60);
  await user.clear(screen.getByLabelText("模型故障自动恢复次数"));
  await user.type(screen.getByLabelText("模型故障自动恢复次数"), "4");
  await user.click(screen.getByRole("button", { name: "保存配置" }));
  expect(operate).toHaveBeenCalledWith("update_config", {
    patch: { supervisor: { model_recovery_attempts: 4 } },
  });
});

it("共享产物设置显示服务端配置，并保存用户修改后的数字容量", async () => {
  const current = state();
  current.config.artifacts = {
    max_artifact_bytes: 67108864,
    chunk_bytes: 65536,
    max_total_bytes: 1073741824,
  };
  const operate = vi
    .spyOn(api, "operate")
    .mockResolvedValue({ config: current.config, revision: 2 });
  const user = userEvent.setup();
  render(<ConfigPage state={current} refresh={vi.fn()} />);
  expect(screen.getByRole("heading", { name: "共享产物" })).toBeVisible();
  const fields = [
    ["单个产物大小上限（字节）", 67108864, "134217728"],
    ["产物分块大小（字节）", 65536, "32768"],
    ["产物总存储上限（字节）", 1073741824, "2147483648"],
  ] as const;
  for (const [label, original, next] of fields) {
    const input = screen.getByLabelText(label);
    expect(input).toHaveValue(original);
    await user.clear(input);
    await user.type(input, next);
  }
  await user.click(screen.getByRole("button", { name: "保存配置" }));
  expect(operate).toHaveBeenCalledWith("update_config", {
    patch: {
      artifacts: {
        max_artifact_bytes: 134217728,
        chunk_bytes: 32768,
        max_total_bytes: 2147483648,
      },
    },
  });
});
