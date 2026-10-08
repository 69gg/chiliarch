import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentExecution } from "../AgentExecution";
import type { Agent } from "../types";

const agent: Agent = {
  id: "agent-one",
  name: "Codex",
  machine: "workstation",
  online: true,
  revoked: false,
  created_at: 1,
  last_seen: 1,
  capabilities: [],
  metadata: { execution_mode: "managed", state: "blocked" },
};

describe("执行器恢复指引", () => {
  it.each([
    ["workspace_unavailable", "工作目录不存在或无法访问", "访问权限"],
    ["workspace_busy", "工作目录正由其他任务使用", "会自动继续"],
    [
      "workspace_unverified",
      "上次执行的清理结果尚未确认",
      "确认相关进程已停止并核对已有改动",
    ],
    ["handler_unavailable", "尚未配置任务执行器", "检查本机 agent 配置"],
  ])("解释 %s，提供对应处理指引", (reason, label, guidance) => {
    render(
      <AgentExecution
        agent={{
          ...agent,
          metadata: { ...agent.metadata, blocked_reason: reason },
        }}
      />,
    );
    const detail = screen.getByLabelText("执行状态");
    expect(detail).toHaveTextContent(label);
    expect(detail).toHaveTextContent(guidance);
    expect(detail).not.toHaveTextContent(reason);
  });

  it("离线的旧受阻信息不会建议盲目解除隔离", () => {
    render(
      <AgentExecution
        agent={{
          ...agent,
          online: false,
          metadata: {
            ...agent.metadata,
            blocked_reason: "workspace_unverified",
          },
        }}
      />,
    );
    expect(screen.getByLabelText("执行状态")).toHaveTextContent(
      "检查守护进程和出站连接",
    );
    expect(screen.queryByText(/--confirm-stopped/)).toBeNull();
  });

  it("扩展执行器的自定义受阻原因保留原文", () => {
    render(
      <AgentExecution
        agent={{
          ...agent,
          metadata: { ...agent.metadata, blocked_reason: "远程开发卷尚未挂载" },
        }}
      />,
    );
    expect(screen.getByLabelText("执行状态")).toHaveTextContent(
      "远程开发卷尚未挂载",
    );
    expect(screen.getByLabelText("执行状态")).toHaveTextContent("执行器日志");
  });
});
