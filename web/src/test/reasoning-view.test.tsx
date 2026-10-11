import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ReasoningView } from "../ChatUI";

// Supply browser scroll geometry, including scrollTop's native clamping, so
// these tests exercise following/reading behavior rather than CSS text.
function scrollGeometry(area: HTMLElement): {
  height: number;
  viewport: number;
  top: number;
} {
  const geometry = { height: 1000, viewport: 320, top: 0 };
  Object.defineProperties(area, {
    scrollHeight: { get: () => geometry.height },
    clientHeight: { get: () => geometry.viewport },
    scrollTop: {
      get: () => geometry.top,
      set: (value: number) => {
        geometry.top = Math.max(
          0,
          Math.min(value, geometry.height - geometry.viewport),
        );
      },
    },
  });
  return geometry;
}

describe("思考阅读区", () => {
  it("默认展开 Markdown，键盘可折叠，后续增量不会重新展开", async () => {
    const { rerender } = render(
      <ReasoningView content="**先读取配置**，再协调任务。" streaming />,
    );
    const toggle = screen.getByRole("button", { name: "思考过程" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("先读取配置").tagName).toBe("STRONG");
    const area = screen.getByRole("region", { name: "思考内容" });
    expect(area).toBeVisible();
    expect(area).toHaveAttribute("tabindex", "0");

    const user = userEvent.setup();
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    rerender(<ReasoningView content="新的公开思考内容。" streaming />);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("新的公开思考内容。")).not.toBeVisible();
    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("新的公开思考内容。")).toBeVisible();
    await user.tab();
    expect(area).toHaveFocus();
  });

  it("流式输出跟随底部，上翻暂停，回到底部后继续跟随，结束不跳动", () => {
    const { rerender } = render(
      <ReasoningView content="开始分析。" streaming />,
    );
    const area = screen.getByRole("region", { name: "思考内容" });
    const geometry = scrollGeometry(area);
    rerender(<ReasoningView content="开始分析。检查机器。" streaming />);
    expect(area.scrollTop).toBe(680);

    area.scrollTop = 180;
    fireEvent.scroll(area);
    geometry.height = 1400;
    rerender(
      <ReasoningView content="开始分析。检查机器。整理结果。" streaming />,
    );
    expect(area.scrollTop).toBe(180);

    area.scrollTop = area.scrollHeight;
    fireEvent.scroll(area);
    geometry.height = 1800;
    rerender(<ReasoningView content="继续分析。" streaming />);
    expect(area.scrollTop).toBe(1480);

    geometry.height = 2000;
    rerender(<ReasoningView content="思考完成。" />);
    expect(area.scrollTop).toBe(1480);
  });

  it("历史思考从开头阅读，收起的流式区域不会滚动隐藏内容", async () => {
    const { rerender } = render(<ReasoningView content="已保存的思考。" />);
    const area = screen.getByRole("region", { name: "思考内容" });
    const geometry = scrollGeometry(area);
    rerender(<ReasoningView content="已保存的完整思考。" />);
    expect(area.scrollTop).toBe(0);
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "思考过程" }));
    geometry.viewport = 0;
    rerender(<ReasoningView content="收起期间的新内容。" streaming />);
    expect(area.scrollTop).toBe(0);
    expect(screen.getByText("收起期间的新内容。")).not.toBeVisible();
  });
});
