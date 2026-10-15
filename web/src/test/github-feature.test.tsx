import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import App from "../App";
import ConfigPage from "../ConfigPage";
import { api, ApiError } from "../api";
import type { JsonObject, PlatformState } from "../types";
import { platformState } from "./fixtures";

vi.mock("../ChatPage", () => ({ default: () => <h1>对话页面</h1> }));
vi.mock("../AgentsPage", () => ({
  default: () => <h1>管理页面</h1>,
  parseObject: (text: string) => JSON.parse(text) as JsonObject,
}));
vi.mock("../CollaborationPage", () => ({ default: () => <h1>协作工作区</h1> }));

const label = "启用 GitHub 功能";

it.each([undefined, {}])(
  "旧配置的 GitHub 设置 %j 默认开启，开关保存布尔值且不修改其他配置",
  async (github) => {
    const state = platformState();
    if (github) state.config.github = github;
    const refresh = vi.fn();
    const operate = vi.spyOn(api, "operate").mockImplementation(
      async (_name, args) =>
        ({
          config: {
            ...state.config,
            github: (args.patch as JsonObject).github,
          },
          revision: 2,
        }) as never,
    );
    const user = userEvent.setup();
    render(<ConfigPage state={state} refresh={refresh} />);
    const toggle = screen.getByRole("checkbox", { name: label });
    expect(toggle).toBeChecked();
    expect(toggle).toHaveAccessibleDescription(/保存后即时生效/);
    expect(toggle).toHaveAccessibleDescription(/已下发的仓库命令仍会完成/);
    expect(screen.getByRole("button", { name: "保存配置" })).toBeDisabled();
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "保存配置" }));
    expect(operate).toHaveBeenCalledExactlyOnceWith("update_config", {
      patch: { github: { enabled: false } },
    });
    expect(refresh).toHaveBeenCalledOnce();
    expect(toggle).not.toBeChecked();
    expect(screen.getByRole("button", { name: "保存配置" })).toBeDisabled();
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "保存配置" }));
    expect(operate).toHaveBeenLastCalledWith("update_config", {
      patch: { github: { enabled: true } },
    });
    expect(toggle).toBeChecked();
  },
);

it("未编辑的 GitHub 开关跟随热重载，保存失败时保留改动并显示错误", async () => {
  const state = platformState();
  const refresh = vi.fn();
  const app = render(<ConfigPage state={state} refresh={refresh} />);
  const next = {
    ...state,
    config: { ...state.config, github: { enabled: false } },
    runtime: { ...state.runtime, config_revision: 2 },
  };
  app.rerender(<ConfigPage state={next} refresh={refresh} />);
  const toggle = screen.getByRole("checkbox", { name: label });
  expect(toggle).not.toBeChecked();
  const operate = vi
    .spyOn(api, "operate")
    .mockRejectedValue(new ApiError("配置写入失败", 503));
  const user = userEvent.setup();
  await user.click(toggle);
  await user.click(screen.getByRole("button", { name: "保存配置" }));
  expect(operate).toHaveBeenCalledWith("update_config", {
    patch: { github: { enabled: true } },
  });
  expect(await screen.findByRole("alert")).toHaveTextContent("配置写入失败");
  expect(toggle).toBeChecked();
  expect(refresh).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "保存配置" })).toBeEnabled();
});

function setupApp(initial: PlatformState) {
  let current = initial;
  vi.spyOn(api, "session").mockResolvedValue({
    authenticated: true,
    csrf_token: "csrf",
  });
  vi.spyOn(api, "state").mockImplementation(async () => current);
  const operate = vi.spyOn(api, "operate").mockImplementation(async (name) => {
    if (name === "list_projects") return { projects: [] } as never;
    if (name === "list_native_interactions")
      return {
        interactions: [
          {
            id: "permission-one",
            agent_id: "agent-one",
            task_id: "task-one",
            kind: "permission",
            title: "确认执行",
            native_method: "permission.request",
            details: {},
            options: [{ id: "once", label: "允许一次" }],
            fields: [],
            status: "pending",
            created_at: 1,
            expires_at: null,
            answered_at: null,
          },
        ],
      } as never;
    throw new Error(`Unexpected operation: ${name}`);
  });
  return {
    operate,
    update: (value: PlatformState) => {
      current = value;
    },
  };
}

it("关闭时直接访问项目地址不会挂载项目请求，普通协作与原生请求仍可使用", async () => {
  const state = platformState();
  state.config.github = { enabled: false };
  const { operate } = setupApp(state);
  history.replaceState(null, "", "#projects/project-one");
  render(<App />);
  expect(
    await screen.findByRole("heading", { name: "对话页面" }),
  ).toBeVisible();
  expect(location.hash).toBe("#ai");
  expect(screen.queryByRole("button", { name: "项目" })).toBeNull();
  expect(operate).not.toHaveBeenCalledWith("list_projects", expect.anything());
  expect(
    await screen.findByRole("button", { name: "1 个原生请求待处理" }),
  ).toBeVisible();
  await userEvent.setup().click(screen.getByRole("button", { name: "协作" }));
  expect(
    await screen.findByRole("heading", { name: "协作工作区" }),
  ).toBeVisible();
  act(() => {
    history.replaceState(null, "", "#projects/project-two");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await waitFor(() => expect(location.hash).toBe("#ai"));
  expect(operate).not.toHaveBeenCalledWith("list_projects", expect.anything());
});

it("热关闭卸载项目轮询并返回对话，重新开启恢复入口但不擅自重新进入项目", async () => {
  const state = platformState();
  state.config.server = { config_poll_seconds: 0.02 };
  const { operate, update } = setupApp(state);
  history.replaceState(null, "", "#projects");
  render(<App />);
  expect(
    await screen.findByRole("heading", { name: "把仓库变成协作项目" }),
  ).toBeVisible();
  expect(operate).toHaveBeenCalledWith("list_projects", {});
  const disabled = {
    ...state,
    config: { ...state.config, github: { enabled: false } },
    runtime: { ...state.runtime, config_revision: 2 },
  };
  update(disabled);
  expect(
    await screen.findByRole("heading", { name: "对话页面" }),
  ).toBeVisible();
  expect(location.hash).toBe("#ai");
  expect(screen.queryByRole("button", { name: "项目" })).toBeNull();
  const requests = () =>
    operate.mock.calls.filter(([name]) => name === "list_projects").length;
  const stoppedAt = requests();
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 80));
  });
  expect(requests()).toBe(stoppedAt);
  expect(
    screen.getByRole("button", { name: "1 个原生请求待处理" }),
  ).toBeVisible();
  update({
    ...disabled,
    config: { ...disabled.config, github: { enabled: true } },
    runtime: { ...state.runtime, config_revision: 3 },
  });
  const projects = await screen.findByRole("button", { name: "项目" });
  expect(location.hash).toBe("#ai");
  expect(requests()).toBe(stoppedAt);
  await userEvent.setup().click(projects);
  expect(
    await screen.findByRole("heading", { name: "把仓库变成协作项目" }),
  ).toBeVisible();
  expect(requests()).toBeGreaterThan(stoppedAt);
});
