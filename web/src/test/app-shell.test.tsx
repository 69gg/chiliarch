import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App, { Login } from "../App";
import { api, ApiError } from "../api";
import { platformState } from "./fixtures";

vi.mock("../ChatPage", () => ({
  default: () => <h1>对话页面</h1>,
}));
vi.mock("../AgentsPage", () => ({
  default: () => <h1>管理页面</h1>,
}));
vi.mock("../ConfigPage", () => ({
  default: () => <h1>配置页面</h1>,
}));

function systemTheme(initialDark: boolean): {
  change: (dark: boolean) => void;
  removeListener: ReturnType<typeof vi.fn>;
} {
  let dark = initialDark;
  const listeners = new Set<() => void>();
  const removeListener = vi.fn((_event: string, callback: () => void) => {
    listeners.delete(callback);
  });
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      get matches() {
        return dark;
      },
      addEventListener: vi.fn((_event: string, callback: () => void) => {
        listeners.add(callback);
      }),
      removeEventListener: removeListener,
    })),
  );
  return {
    change: (next: boolean): void => {
      dark = next;
      for (const listener of listeners) listener();
    },
    removeListener,
  };
}

beforeEach(() => {
  localStorage.clear();
  location.hash = "";
  delete document.documentElement.dataset.theme;
  vi.spyOn(api, "session").mockResolvedValue({
    authenticated: true,
    csrf_token: "csrf",
  });
  vi.spyOn(api, "state").mockResolvedValue(platformState());
});

describe("应用外壳", () => {
  it("默认跟随系统，按博客顺序循环主题并保存，回到系统后继续跟随变化", async () => {
    const media = systemTheme(true);
    const user = userEvent.setup();
    const app = render(<App />);
    const theme = await screen.findByRole("button", {
      name: "当前跟随系统（深色），切换为浅色",
    });
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(theme).toHaveAttribute("title", "当前跟随系统（深色），切换为浅色");

    await user.click(theme);
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("chiliarch-theme")).toBe("light");
    act(() => media.change(false));
    act(() => media.change(true));
    expect(document.documentElement.dataset.theme).toBe("light");

    await user.click(
      screen.getByRole("button", { name: "当前浅色，切换为深色" }),
    );
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("chiliarch-theme")).toBe("dark");

    await user.click(
      screen.getByRole("button", { name: "当前深色，切换为跟随系统" }),
    );
    expect(localStorage.getItem("chiliarch-theme")).toBe("system");
    act(() => media.change(false));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(
      screen.getByRole("button", {
        name: "当前跟随系统（浅色），切换为浅色",
      }),
    ).toBeEnabled();
    app.unmount();
    expect(media.removeListener).toHaveBeenCalledOnce();
  });

  it("重新打开保留选定主题，导航切换管理页并保存地址", async () => {
    systemTheme(false);
    localStorage.setItem("chiliarch-theme", "dark");
    render(<App />);
    await screen.findByRole("button", { name: "当前深色，切换为跟随系统" });
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(
      screen.getByRole("button", { name: "Chiliarch 首页" }),
    ).toBeVisible();
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Agents 管理" }));
    expect(
      await screen.findByRole("heading", { name: "管理页面" }),
    ).toBeVisible();
    expect(location.hash).toBe("#agents");
    expect(screen.getByRole("button", { name: "Agents 管理" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("状态读取失败仍展示错误，重连成功后恢复工作区", async () => {
    systemTheme(false);
    const state = vi
      .spyOn(api, "state")
      .mockRejectedValueOnce(new ApiError("平台暂时不可达", 503))
      .mockResolvedValue(platformState());
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "平台暂时不可达",
    );
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "重新连接" }));
    expect(
      await screen.findByRole("heading", { name: "对话页面" }),
    ).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(state).toHaveBeenCalledTimes(2);
  });

  it("登录态过期退回登录，并保留服务器返回的错误", async () => {
    systemTheme(false);
    vi.spyOn(api, "state").mockRejectedValue(new ApiError("登录已过期", 401));
    render(<App />);
    expect(
      await screen.findByRole("heading", { name: "进入工作区" }),
    ).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("登录已过期");
    expect(screen.getByLabelText("登录密码")).toBeEnabled();
  });

  it("退出失败的 CSRF 错误可见，退出成功后回到登录", async () => {
    systemTheme(false);
    const logout = vi
      .spyOn(api, "logout")
      .mockRejectedValueOnce(new ApiError("CSRF 校验失败", 403))
      .mockResolvedValueOnce(undefined);
    render(<App />);
    const button = await screen.findByRole("button", { name: "退出登录 ↗" });
    await userEvent.setup().click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("CSRF 校验失败");
    expect(screen.getByRole("heading", { name: "对话页面" })).toBeVisible();
    await userEvent.setup().click(button);
    expect(
      await screen.findByRole("heading", { name: "进入工作区" }),
    ).toBeVisible();
    expect(logout).toHaveBeenCalledTimes(2);
  });

  it("认证成功后才进入工作区，精简登录文案仍保留错误反馈", async () => {
    const login = vi
      .spyOn(api, "login")
      .mockRejectedValueOnce(new ApiError("密码无效", 401))
      .mockResolvedValueOnce({ authenticated: true, csrf_token: "csrf" });
    const entered = vi.fn();
    const user = userEvent.setup();
    render(<Login onLogin={entered} />);
    expect(screen.getByRole("heading", { name: "进入工作区" })).toBeVisible();
    expect(screen.getByRole("button", { name: "登录 →" })).toBeDisabled();
    await user.type(screen.getByLabelText("登录密码"), "workspace-password");
    await user.click(screen.getByRole("button", { name: "登录 →" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("密码无效");
    expect(entered).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "登录 →" }));
    await waitFor(() => expect(entered).toHaveBeenCalledOnce());
    expect(login).toHaveBeenLastCalledWith("workspace-password");
  });
});
