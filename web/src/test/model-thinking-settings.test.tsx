import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import ConfigPage from "../ConfigPage";
import { api } from "../api";
import type { JsonObject } from "../types";
import { platformState } from "./fixtures";

it("独立关闭 enable_thinking 参数并保存，保留 reasoning 选择", async () => {
  const state = platformState();
  (state.config.model as JsonObject).reasoning_enabled = true;
  const config = {
    ...state.config,
    model: {
      ...(state.config.model as JsonObject),
      enable_thinking_param_enabled: false,
    },
  };
  const operate = vi
    .spyOn(api, "operate")
    .mockResolvedValue({ config, revision: 2 });
  const user = userEvent.setup();
  const refresh = vi.fn();
  const { rerender } = render(<ConfigPage state={state} refresh={refresh} />);
  const control = screen.getByRole("checkbox", {
    name: "发送 enable_thinking 参数",
  });
  expect(control).toBeChecked();
  expect(control).toHaveAccessibleDescription(/省略此参数/);
  await user.click(control);
  expect(control).not.toBeChecked();
  expect(
    screen.getByRole("checkbox", { name: "启用 reasoning" }),
  ).toBeChecked();
  await user.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  expect(operate).toHaveBeenCalledExactlyOnceWith("update_config", {
    patch: { model: { enable_thinking_param_enabled: false } },
  });
  expect(control).not.toBeChecked();
  expect(
    screen.getByRole("checkbox", { name: "启用 reasoning" }),
  ).toBeChecked();
  rerender(
    <ConfigPage
      state={{
        ...state,
        config,
        runtime: { ...state.runtime, config_revision: 2 },
      }}
      refresh={refresh}
    />,
  );
  expect(control).not.toBeChecked();
  expect(screen.getByRole("button", { name: "保存配置" })).toBeDisabled();
});

it("未编辑时响应服务端热重载后的参数开关", () => {
  const state = platformState();
  const { rerender } = render(<ConfigPage state={state} refresh={vi.fn()} />);
  expect(
    screen.getByRole("checkbox", { name: "发送 enable_thinking 参数" }),
  ).toBeChecked();
  const next = platformState();
  (next.config.model as JsonObject).enable_thinking_param_enabled = false;
  next.runtime.config_revision += 1;
  rerender(<ConfigPage state={next} refresh={vi.fn()} />);
  expect(
    screen.getByRole("checkbox", { name: "发送 enable_thinking 参数" }),
  ).not.toBeChecked();
});

it("保存中到达的旧快照不覆盖结果，之后热重载及服务重启仍更新表单", async () => {
  let complete!: (result: JsonObject) => void;
  vi.spyOn(api, "operate").mockReturnValue(
    new Promise<JsonObject>((resolve) => {
      complete = resolve;
    }),
  );
  const state = platformState();
  const refresh = vi.fn();
  const { rerender } = render(<ConfigPage state={state} refresh={refresh} />);
  fireEvent.click(
    screen.getByRole("checkbox", { name: "发送 enable_thinking 参数" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  const intermediate = platformState();
  intermediate.runtime.config_revision += 1;
  rerender(<ConfigPage state={intermediate} refresh={refresh} />);
  const saved = platformState();
  (saved.config.model as JsonObject).enable_thinking_param_enabled = false;
  saved.runtime.config_revision += 2;
  await act(async () => {
    complete({ config: saved.config, revision: saved.runtime.config_revision });
  });
  expect(refresh).toHaveBeenCalledOnce();
  expect(
    screen.getByRole("checkbox", { name: "发送 enable_thinking 参数" }),
  ).not.toBeChecked();
  rerender(<ConfigPage state={saved} refresh={refresh} />);
  expect(
    screen.getByRole("checkbox", { name: "发送 enable_thinking 参数" }),
  ).not.toBeChecked();
  // A new server process starts its in-memory revision counter again.
  rerender(<ConfigPage state={state} refresh={refresh} />);
  expect(
    screen.getByRole("checkbox", { name: "发送 enable_thinking 参数" }),
  ).toBeChecked();
});
