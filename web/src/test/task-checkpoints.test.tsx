import {
  act,
  fireEvent,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import TaskCheckpoints from "../TaskCheckpoints";
import { api, ApiError } from "../api";
import type { JsonObject, Task } from "../types";
import { platformState } from "./fixtures";

const source: Task = {
  id: "task-one",
  agent_id: "agent-one",
  instruction: "实现接口",
  status: "running",
  parameters: {},
  progress: 0,
  result: null,
  error: null,
  created_at: 1,
  updated_at: 1,
  requested_by: "user",
};
const dependency: Task = {
  ...source,
  id: "task-two",
  agent_id: "agent-two",
  instruction: "完成测试",
  status: "completed",
};
const checkpoint = {
  id: "checkpoint-one",
  task_id: source.id,
  agent_id: source.agent_id,
  summary: "代码已保存，等待测试后整合",
  artifacts: [],
  waiting_for: [dependency.id],
  state: "ready",
  resumed_task_id: null as string | null,
  updated_at: 2,
  dependencies: [
    {
      task_id: dependency.id,
      original_task_id: dependency.id,
      status: "completed",
      review_status: "accepted",
    },
  ],
};
function state() {
  return { ...platformState(), tasks: [source, dependency] };
}
function mock(
  checkpoints: (typeof checkpoint)[],
  overrides: Record<string, (args: JsonObject) => unknown> = {},
) {
  return vi.spyOn(api, "operate").mockImplementation(async (name, args) => {
    if (overrides[name]) return (await overrides[name](args)) as never;
    if (name === "list_task_checkpoints") return { checkpoints } as never;
    throw new Error(`Unexpected operation ${name}`);
  });
}
async function open() {
  await userEvent.setup().click(screen.getByText("阶段与续接"));
  await screen.findByRole("button", { name: "保存任务阶段" });
}

describe("任务阶段", () => {
  it("按需读取阶段且只显示当前 agent 的记录", async () => {
    const operate = mock([
      checkpoint,
      {
        ...checkpoint,
        id: "other",
        agent_id: "agent-two",
        summary: "其他机器阶段",
      },
    ]);
    render(
      <TaskCheckpoints
        agentId={source.agent_id}
        state={state()}
        refresh={vi.fn()}
      />,
    );
    expect(operate).not.toHaveBeenCalled();
    await open();
    expect(screen.getByText(checkpoint.summary)).toBeVisible();
    expect(screen.queryByText("其他机器阶段")).toBeNull();
  });
  it("保存摘要、产物和依赖，但不停止任务也不报告资源已释放", async () => {
    const operate = mock([], {
      save_task_checkpoint: () => ({
        checkpoint: { ...checkpoint, state: "awaiting_exit" },
      }),
    });
    const user = userEvent.setup();
    render(
      <TaskCheckpoints
        agentId={source.agent_id}
        state={state()}
        refresh={vi.fn()}
      />,
    );
    await open();
    await user.click(screen.getByRole("button", { name: "保存任务阶段" }));
    const dialog = screen.getByRole("dialog");
    await user.selectOptions(screen.getByLabelText("原生任务"), source.id);
    fireEvent.change(screen.getByLabelText("阶段摘要与下一步"), {
      target: { value: checkpoint.summary },
    });
    expect(
      within(dialog).queryByRole("checkbox", { name: source.instruction }),
    ).toBeNull();
    await user.click(
      screen.getByRole("checkbox", { name: dependency.instruction }),
    );
    await user.click(screen.getByRole("button", { name: "添加产物引用" }));
    fireEvent.change(screen.getByLabelText("产物 1 名称"), {
      target: { value: "代码提交" },
    });
    fireEvent.change(screen.getByLabelText("产物 1 地址"), {
      target: { value: "git:commit-one" },
    });
    await user.click(screen.getByRole("button", { name: "保存阶段" }));
    expect(operate).toHaveBeenCalledWith("save_task_checkpoint", {
      task_id: source.id,
      summary: checkpoint.summary,
      artifacts: [
        { name: "代码提交", uri: "git:commit-one", kind: "reference" },
      ],
      waiting_for: [dependency.id],
    });
    expect(operate).not.toHaveBeenCalledWith("cancel_task", expect.anything());
    expect(screen.getByRole("status")).toHaveTextContent(
      "等待原生任务实际结束",
    );
  });
  it.each(["awaiting_exit", "paused", "unknown", "failed"])(
    "%s 阶段没有续接入口",
    async (status) => {
      mock([{ ...checkpoint, state: status }]);
      render(
        <TaskCheckpoints
          agentId={source.agent_id}
          state={state()}
          refresh={vi.fn()}
        />,
      );
      await open();
      expect(screen.queryByRole("button", { name: "继续下一阶段" })).toBeNull();
    },
  );
  it("ready 时按 checkpoint ID 续接一次，不重选 agent 或目录", async () => {
    let resolve!: (value: unknown) => void;
    const result = new Promise((done) => {
      resolve = done;
    });
    const operate = mock([checkpoint], {
      resume_task_checkpoint: () => result,
    });
    const user = userEvent.setup();
    render(
      <TaskCheckpoints
        agentId={source.agent_id}
        state={state()}
        refresh={vi.fn()}
      />,
    );
    await open();
    fireEvent.change(screen.getByLabelText("下一阶段指令（可选）"), {
      target: { value: "读取测试结果后整合" },
    });
    await user.click(screen.getByRole("button", { name: "继续下一阶段" }));
    expect(screen.getByRole("button", { name: "登记中…" })).toBeDisabled();
    await act(async () => resolve({ task: { id: "resumed-one" } }));
    expect(operate).toHaveBeenCalledWith("resume_task_checkpoint", {
      checkpoint_id: checkpoint.id,
      instruction: "读取测试结果后整合",
    });
    expect(screen.getByRole("button", { name: "已登记续接" })).toBeDisabled();
    expect(
      operate.mock.calls.filter(([name]) => name === "resume_task_checkpoint"),
    ).toHaveLength(1);
  });
  it("续接受阻时不标记成功，保留输入和实际错误", async () => {
    mock([checkpoint], {
      resume_task_checkpoint: () => {
        throw new ApiError("Original process is active", 409);
      },
    });
    const user = userEvent.setup();
    render(
      <TaskCheckpoints
        agentId={source.agent_id}
        state={state()}
        refresh={vi.fn()}
      />,
    );
    await open();
    await user.click(screen.getByRole("button", { name: "继续下一阶段" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Original process is active",
    );
    expect(screen.getByRole("button", { name: "继续下一阶段" })).toBeEnabled();
  });
  it("已经续接的阶段展示新任务 ID，不允许重复创建", async () => {
    mock([{ ...checkpoint, state: "resumed", resumed_task_id: "resumed-one" }]);
    render(
      <TaskCheckpoints
        agentId={source.agent_id}
        state={state()}
        refresh={vi.fn()}
      />,
    );
    await open();
    await waitFor(() => expect(screen.getByText("resumed-one")).toBeVisible());
    expect(screen.queryByRole("button", { name: "继续下一阶段" })).toBeNull();
  });
});
