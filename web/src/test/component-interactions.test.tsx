import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnimatedDisclosure, MarkdownContent, ToolView } from "../components";
import { api, ApiError } from "../api";
import type { Approval, ToolCall } from "../types";

afterEach(() => vi.useRealTimers());

function tool(): ToolCall {
  return {
    id: "tool-one",
    run_id: "run-one",
    name: "dispatch_task",
    arguments: { agent_id: "machine-a", instruction: "读取当前进度" },
    result: { task_id: "task-one", status: "queued", progress: 0.4 },
    status: "completed",
    approval_id: "approval-one",
  };
}

function approval(): Approval {
  return {
    id: "approval-one",
    operation: "dispatch_task",
    arguments: { agent_id: "machine-a", instruction: "读取当前进度" },
    requested_by: "supervisor",
    status: "pending",
    reason: "需要授权在该机器上执行任务。",
    created_at: 0,
    resolved_at: null,
    result: null,
  };
}

describe("共享折叠交互", () => {
  it("工具摘要可展开读取真实参数和结果，再关闭时隐藏内容", async () => {
    render(<ToolView tool={tool()} />);
    const group = screen.getByRole("group", { name: "工具调用 dispatch_task" });
    const toggle = within(group).getByRole("button", { name: /派发任务/ });
    const panel = document.getElementById(
      toggle.getAttribute("aria-controls")!,
    )!;
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(panel).toHaveAttribute("hidden");
    expect(
      within(group).queryByRole("heading", { name: "参数" }),
    ).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(panel).not.toHaveAttribute("hidden");
    expect(within(group).getByRole("heading", { name: "参数" })).toBeVisible();
    expect(within(group).getByRole("heading", { name: "结果" })).toBeVisible();
    const data = Array.from(panel.querySelectorAll("pre code"), (node) =>
      JSON.parse(node.textContent!),
    );
    expect(data).toEqual([
      { agent_id: "machine-a", instruction: "读取当前进度" },
      { task_id: "task-one", status: "queued", progress: 0.4 },
    ]);
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(panel).not.toBeVisible();
    expect(
      within(group).queryByRole("heading", { name: "结果" }),
    ).not.toBeInTheDocument();
  });

  it("思考组件支持键盘展开，多个组件的内容关联不冲突", async () => {
    render(
      <>
        <AnimatedDisclosure title="思考过程" icon="◇">
          <MarkdownContent content="**先读取配置**，然后调度。" />
          <button>查看记录</button>
        </AnimatedDisclosure>
        <AnimatedDisclosure title="补充记录">独立的内容</AnimatedDisclosure>
      </>,
    );
    const first = screen.getByRole("button", { name: "思考过程" });
    const second = screen.getByRole("button", { name: "补充记录" });
    expect(first.getAttribute("aria-controls")).not.toBe(
      second.getAttribute("aria-controls"),
    );
    expect(
      screen.queryByRole("button", { name: "查看记录" }),
    ).not.toBeInTheDocument();
    const user = userEvent.setup();
    first.focus();
    await user.keyboard("{Enter}");
    expect(first).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("先读取配置")).toBeVisible();
    expect(screen.getByText("先读取配置").tagName).toBe("STRONG");
    await user.tab();
    expect(screen.getByRole("button", { name: "查看记录" })).toHaveFocus();
    first.focus();
    await user.keyboard(" ");
    expect(first).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("先读取配置")).not.toBeVisible();
    expect(
      screen.queryByRole("button", { name: "查看记录" }),
    ).not.toBeInTheDocument();
    expect(second).toHaveAttribute("aria-expanded", "false");
  });
});

describe("工具审批", () => {
  it("审批失败保留按钮和明确 ID，重试成功后才回调", async () => {
    const waiting = {
      ...tool(),
      result: undefined,
      status: "approval" as const,
    };
    const resolved = vi.fn();
    const operate = vi
      .spyOn(api, "operate")
      .mockRejectedValueOnce(new ApiError("当前操作暂不可用，请重试。", 409))
      .mockResolvedValueOnce({ status: "approved" });
    render(
      <ToolView tool={waiting} approval={approval()} onResolved={resolved} />,
    );
    const group = screen.getByRole("group", { name: "工具调用 dispatch_task" });
    const allow = within(group).getByRole("button", { name: "允许这次操作" });
    const user = userEvent.setup();
    await user.click(allow);
    expect(await within(group).findByRole("alert")).toHaveTextContent(
      "当前操作暂不可用，请重试。",
    );
    expect(allow).toBeEnabled();
    expect(within(group).getByRole("button", { name: "拒绝" })).toBeEnabled();
    expect(resolved).not.toHaveBeenCalled();
    await user.click(allow);
    expect(resolved).toHaveBeenCalledOnce();
    expect(operate.mock.calls).toEqual([
      ["approve_operation", { approval_id: "approval-one" }],
      ["approve_operation", { approval_id: "approval-one" }],
    ]);
    expect(within(group).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("不向错误审批 ID 暴露按钮，已完成工具不会恢复审批入口", () => {
    const resolved = vi.fn();
    const waiting = { ...tool(), status: "approval" as const };
    const { rerender } = render(
      <ToolView
        tool={waiting}
        approval={{ ...approval(), id: "another-approval" }}
        onResolved={resolved}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "允许这次操作" }),
    ).not.toBeInTheDocument();
    rerender(
      <ToolView tool={tool()} approval={approval()} onResolved={resolved} />,
    );
    expect(
      screen.queryByRole("button", { name: "允许这次操作" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "拒绝" }),
    ).not.toBeInTheDocument();
    expect(resolved).not.toHaveBeenCalled();
  });
});

describe("Markdown 代码复制", () => {
  it("复制真实代码并显示成功状态，周围流式正文更新不会重建代码组件", async () => {
    const user = userEvent.setup();
    const clipboard = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue();
    const source = "```python\nprint('你好')\n```";
    const { rerender } = render(<MarkdownContent content={source} />);
    const copy = screen.getByRole("button", { name: "复制代码" });
    await user.click(copy);
    expect(clipboard).toHaveBeenCalledExactlyOnceWith("print('你好')");
    expect(screen.getByRole("button", { name: "已复制" })).toBe(copy);
    expect(screen.getByRole("status")).toHaveTextContent("代码已复制");
    rerender(<MarkdownContent content={`${source}\n\n后续回答已经到达。`} />);
    expect(screen.getByRole("button", { name: "已复制" })).toBe(copy);
    expect(copy).toHaveFocus();
    expect(screen.getByText("后续回答已经到达。")).toBeVisible();
  });

  it("剪贴板失败给出可重试反馈，重试后提示恢复正常", async () => {
    const user = userEvent.setup();
    const clipboard = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockRejectedValueOnce(new Error("permission denied"))
      .mockResolvedValueOnce();
    render(<MarkdownContent content={"```text\n实际代码\n```"} />);
    await user.click(screen.getByRole("button", { name: "复制代码" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "无法访问剪贴板，请手动选择代码。",
    );
    expect(screen.getByRole("button", { name: "复制代码" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "复制代码" }));
    expect(clipboard).toHaveBeenCalledTimes(2);
    expect(clipboard).toHaveBeenLastCalledWith("实际代码");
    expect(screen.getByRole("status")).toHaveTextContent("代码已复制");
    expect(
      screen.queryByText("无法访问剪贴板，请手动选择代码。"),
    ).not.toBeInTheDocument();
  });

  it("复制成功反馈自动收起，变化后的代码需要重新复制", async () => {
    userEvent.setup();
    vi.useFakeTimers();
    const clipboard = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue();
    const { rerender } = render(
      <MarkdownContent content={"```text\n第一版\n```"} />,
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "复制代码" })),
    );
    expect(screen.getByRole("button", { name: "已复制" })).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByRole("button", { name: "复制代码" })).toBeVisible();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "复制代码" })),
    );
    rerender(<MarkdownContent content={"```text\n第二版\n```"} />);
    expect(screen.getByRole("button", { name: "复制代码" })).toBeVisible();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "复制代码" })),
    );
    expect(clipboard).toHaveBeenLastCalledWith("第二版");
  });
});
