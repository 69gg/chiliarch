import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import ConfigPage from "../ConfigPage";
import { api } from "../api";
import type { JsonObject } from "../types";
import { platformState } from "./fixtures";

const outputLabel = "Anthropic 最大输出 tokens";

it("仅选择 Anthropic 时展示无默认值的输出上限，并保存用户填写的整数", async () => {
  const state = platformState();
  const operate = vi.spyOn(api, "operate").mockResolvedValue({
    config: state.config,
    revision: 2,
  });
  const user = userEvent.setup();
  render(<ConfigPage state={state} refresh={vi.fn()} />);
  expect(screen.queryByLabelText(outputLabel)).not.toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("模型 API"), "anthropic");
  const input = screen.getByLabelText(outputLabel);
  expect(input).toHaveValue(null);
  expect(input).toHaveAttribute("min", "1");
  expect(input).toHaveAttribute("step", "1");
  await user.type(input, "8192");
  await user.click(screen.getByRole("button", { name: "保存配置" }));
  expect(operate).toHaveBeenCalledWith("update_config", {
    patch: { model: { api_mode: "anthropic", anthropic_max_tokens: 8192 } },
  });
});

it.each(["chat_completions", "responses"])(
  "切换到 %s 后隐藏 Anthropic 上限，不提交隐藏字段的修改",
  async (mode) => {
    const state = platformState();
    Object.assign(state.config.model as JsonObject, {
      api_mode: "anthropic",
      anthropic_max_tokens: 4096,
    });
    const operate = vi.spyOn(api, "operate").mockResolvedValue({
      config: state.config,
      revision: 2,
    });
    const user = userEvent.setup();
    render(<ConfigPage state={state} refresh={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(outputLabel), {
      target: { value: "8192" },
    });
    await user.selectOptions(screen.getByLabelText("模型 API"), mode);
    expect(screen.queryByLabelText(outputLabel)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存配置" }));
    expect(operate).toHaveBeenCalledWith("update_config", {
      patch: { model: { api_mode: mode } },
    });
  },
);

it.each(["0", "-1", "1.5"])(
  "Anthropic 上限 %s 不是正整数，不发起配置请求",
  (value) => {
    const state = platformState();
    (state.config.model as JsonObject).api_mode = "anthropic";
    const operate = vi.spyOn(api, "operate").mockResolvedValue({});
    render(<ConfigPage state={state} refresh={vi.fn()} />);
    const input = screen.getByLabelText(outputLabel);
    fireEvent.change(input, { target: { value } });
    fireEvent.submit(input.closest("form")!);
    expect(screen.getByRole("alert")).toHaveTextContent("需要正整数");
    expect(operate).not.toHaveBeenCalled();
  },
);
