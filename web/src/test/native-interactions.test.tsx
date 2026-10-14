import {
  fireEvent,
  render,
  screen,
  within,
  waitFor,
  act,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import NativeInteractions from "../NativeInteractions";
import type { NativeInteraction } from "../NativeInteractions";
import { api, ApiError } from "../api";
import { platformState } from "./fixtures";
import type { JsonObject } from "../types";

const permission: NativeInteraction = {
  id: "native-one",
  agent_id: "agent-one",
  task_id: "task-one",
  kind: "permission",
  title: "允许在工作区执行测试吗？",
  native_method: "requestPermission",
  details: { command: "uv run pytest", directory: "/managed/project" },
  options: [
    { id: "allow_once", label: "仅允许这次" },
    { id: "reject", label: "拒绝" },
  ],
  fields: [],
  status: "pending",
  created_at: 2,
  expires_at: null,
  answered_at: null,
};
const input: NativeInteraction = {
  ...permission,
  id: "input-one",
  kind: "input",
  title: "需要部署参数",
  options: [],
  details: {},
  fields: [
    {
      id: "token",
      label: "临时令牌",
      secret: true,
      multiple: false,
      options: [],
    },
    {
      id: "targets",
      label: "验证系统",
      secret: false,
      multiple: true,
      options: [
        { id: "linux", label: "Linux" },
        { id: "windows", label: "Windows" },
      ],
    },
  ],
};
function operations(
  initial: NativeInteraction[],
  resolve?: (args: JsonObject) => unknown,
) {
  let pending = initial;
  const operate = vi
    .spyOn(api, "operate")
    .mockImplementation(async (name, args) => {
      if (name === "list_native_interactions")
        return { interactions: pending } as never;
      if (name === "resolve_native_interaction") {
        const response = resolve ? await resolve(args) : {};
        pending = pending.filter((item) => item.id !== args.interaction_id);
        return response as never;
      }
      throw new Error(`Unexpected operation ${name}`);
    });
  return {
    operate,
    replace: (value: NativeInteraction[]) => {
      pending = value;
    },
  };
}
afterEach(() => vi.useRealTimers());

describe("原生交互桥", () => {
  it("没有请求时不显示按钮，有请求时只显示计数不强制弹窗", async () => {
    const { operate } = operations([]);
    const app = render(
      <NativeInteractions state={platformState()} refresh={vi.fn()} />,
    );
    await waitFor(() =>
      expect(operate).toHaveBeenCalledWith("list_native_interactions", {
        status: "pending",
      }),
    );
    expect(screen.queryByRole("button")).toBeNull();
    app.unmount();
    operations([permission]);
    render(<NativeInteractions state={platformState()} refresh={vi.fn()} />);
    expect(
      await screen.findByRole("button", { name: "1 个原生请求待处理" }),
    ).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  it("按原始选项 ID 回答权限请求，提交后不声称已执行", async () => {
    const { operate } = operations([permission]);
    const refresh = vi.fn();
    const user = userEvent.setup();
    render(<NativeInteractions state={platformState()} refresh={refresh} />);
    await user.click(
      await screen.findByRole("button", { name: "1 个原生请求待处理" }),
    );
    expect(
      screen.getByRole("heading", { name: permission.title }),
    ).toBeVisible();
    await user.click(screen.getByText("原生请求详情"));
    expect(screen.getByText(/uv run pytest/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "仅允许这次" }));
    expect(operate).toHaveBeenCalledWith("resolve_native_interaction", {
      interaction_id: permission.id,
      native_answer: { choice: "allow_once" },
    });
    expect(
      await screen.findByText("回答已提交，等待原生 agent 接收并继续。"),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "仅允许这次" })).toBeNull();
    expect(refresh).toHaveBeenCalledOnce();
  });
  it("密码输入和多选以原始字段传递，成功后清除输入且不回显回答", async () => {
    const { operate } = operations([input]);
    const user = userEvent.setup();
    render(<NativeInteractions state={platformState()} refresh={vi.fn()} />);
    await user.click(
      await screen.findByRole("button", { name: "1 个原生请求待处理" }),
    );
    const secret = screen.getByLabelText("临时令牌");
    expect(secret).toHaveAttribute("type", "password");
    fireEvent.change(secret, { target: { value: "secret-native-token" } });
    await user.selectOptions(screen.getByLabelText("验证系统"), [
      "linux",
      "windows",
    ]);
    await user.click(screen.getByRole("button", { name: "提交回答" }));
    expect(operate).toHaveBeenCalledWith("resolve_native_interaction", {
      interaction_id: input.id,
      native_answer: {
        fields: { token: "secret-native-token", targets: ["linux", "windows"] },
      },
    });
    expect(screen.queryByDisplayValue("secret-native-token")).toBeNull();
    expect(screen.queryByText("secret-native-token")).toBeNull();
  });
  it("多请求独立选择，不能把上一请求的答案发给另一请求", async () => {
    const { operate } = operations([permission, input]);
    const user = userEvent.setup();
    render(<NativeInteractions state={platformState()} refresh={vi.fn()} />);
    await user.click(
      await screen.findByRole("button", { name: "2 个原生请求待处理" }),
    );
    await user.selectOptions(screen.getByLabelText("选择待处理请求"), input.id);
    fireEvent.change(screen.getByLabelText("临时令牌"), {
      target: { value: "draft-value" },
    });
    await user.selectOptions(
      screen.getByLabelText("选择待处理请求"),
      permission.id,
    );
    await user.click(screen.getByRole("button", { name: "拒绝" }));
    expect(operate).toHaveBeenCalledWith("resolve_native_interaction", {
      interaction_id: permission.id,
      native_answer: { choice: "reject" },
    });
    expect(await screen.findByLabelText("临时令牌")).toHaveValue("");
  });
  it("服务端拒绝过期响应时保留错误，不显示成功", async () => {
    operations([permission], () => {
      throw new ApiError("Native interaction expired", 409);
    });
    const user = userEvent.setup();
    render(<NativeInteractions state={platformState()} refresh={vi.fn()} />);
    await user.click(
      await screen.findByRole("button", { name: "1 个原生请求待处理" }),
    );
    await user.click(screen.getByRole("button", { name: "仅允许这次" }));
    expect(
      await within(screen.getByRole("dialog")).findByRole("alert"),
    ).toHaveTextContent("Native interaction expired");
    expect(
      screen.queryByText("回答已提交，等待原生 agent 接收并继续。"),
    ).toBeNull();
  });
  it("请求轮询失效后移除旧表单，不允许再提交", async () => {
    vi.useFakeTimers();
    const { replace } = operations([permission]);
    render(<NativeInteractions state={platformState()} refresh={vi.fn()} />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "1 个原生请求待处理" }));
    replace([]);
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.queryByRole("button", { name: "仅允许这次" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("当前没有待处理");
  });
});
