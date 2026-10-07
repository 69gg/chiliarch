import type {
  ChatEvent,
  ChatMessage,
  ConversationDetail,
  Run,
  ToolCall,
} from "./types";

export type ChatPart =
  | {
      id: string;
      run_id: string;
      kind: "text" | "reasoning";
      content: string;
      message_id?: string;
    }
  | { id: string; run_id: string; kind: "tool"; tool: ToolCall };

export interface ChatState {
  messages: ChatMessage[];
  runs: Run[];
  tools: ToolCall[];
  parts?: ChatPart[];
  lastEventSeq?: number;
  context?: {
    run_id: string;
    tokens: number;
    contextWindow: number;
    estimated: boolean;
    compacting: boolean;
  };
}

const RUN_EVENT_STATUS: Record<string, string> = {
  run_started: "running",
  approval_required: "awaiting_approval",
  completed: "completed",
  cancelled: "cancelled",
  interrupted: "interrupted",
};

/** Event sequence, rather than the final message fields, is the display order. */
export function applyChatEvent(state: ChatState, event: ChatEvent): ChatState {
  if (state.lastEventSeq !== undefined && event.seq <= state.lastEventSeq)
    return state;
  const data = event.data;
  const runId = event.run_id ?? "";
  let { messages, runs, tools } = state;
  let parts = state.parts ?? [];
  let context = state.context;
  if (
    event.type === "context_usage" &&
    runId &&
    typeof data.tokens === "number" &&
    typeof data.context_window === "number"
  ) {
    context = {
      run_id: runId,
      tokens: data.tokens,
      contextWindow: data.context_window,
      estimated: Boolean(data.estimated),
      compacting: false,
    };
  }
  if (event.type === "context_compacting" && context?.run_id === runId)
    context = { ...context, compacting: true };
  if (
    event.type === "usage" &&
    context?.run_id === runId &&
    typeof data.input_tokens === "number" &&
    data.input_tokens > 0 &&
    typeof data.output_tokens === "number"
  )
    context = {
      ...context,
      tokens: data.input_tokens + data.output_tokens,
      estimated: false,
    };
  if (
    event.type === "context_compacted" &&
    context?.run_id === runId &&
    typeof data.tokens_after === "number"
  )
    context = {
      ...context,
      tokens: data.tokens_after,
      estimated: true,
      compacting: false,
    };
  if (event.type === "context_compaction_failed" && context?.run_id === runId)
    context = { ...context, compacting: false };
  if (
    ["run_started", "text_delta", "reasoning_delta"].includes(event.type) &&
    runId
  ) {
    const id =
      typeof data.message_id === "string"
        ? data.message_id
        : `${runId}:assistant`;
    const existing = messages.find((message) => message.id === id);
    const message: ChatMessage = existing ?? {
      id,
      role: "assistant",
      content: "",
      reasoning: "",
      created_at: Date.now() / 1000,
      run_id: runId,
    };
    const delta = typeof data.delta === "string" ? data.delta : "";
    const next = {
      ...message,
      content: message.content + (event.type === "text_delta" ? delta : ""),
      reasoning:
        message.reasoning + (event.type === "reasoning_delta" ? delta : ""),
    };
    messages = existing
      ? messages.map((item) => (item.id === id ? next : item))
      : [...messages, next];
    if (delta && event.type !== "run_started") {
      const kind = event.type === "text_delta" ? "text" : "reasoning";
      const previous = parts.at(-1);
      parts =
        previous?.kind === kind &&
        previous.run_id === runId &&
        previous.message_id === id
          ? [
              ...parts.slice(0, -1),
              { ...previous, content: previous.content + delta },
            ]
          : [
              ...parts,
              {
                id: `event:${event.seq}`,
                run_id: runId,
                message_id: id,
                kind,
                content: delta,
              },
            ];
    }
  }
  if (
    ["tool_start", "tool_end", "approval_required"].includes(event.type) &&
    runId
  ) {
    const id = String(data.id);
    const key = typeof data.key === "string" ? data.key : undefined;
    const candidates = tools.filter(
      (tool) => tool.id === id && tool.run_id === runId,
    );
    const previous =
      (key ? candidates.find((tool) => tool.key === key) : undefined) ??
      [...candidates]
        .reverse()
        .find(
          (tool) =>
            (!key || !tool.key) &&
            (event.type === "tool_end" || tool.status !== "completed"),
        );
    const tool: ToolCall = {
      id,
      key: key ?? previous?.key,
      name: String(data.name),
      arguments: data.arguments ?? previous?.arguments ?? {},
      run_id: runId,
      approval_id:
        typeof data.approval_id === "string"
          ? data.approval_id
          : previous?.approval_id,
      status:
        event.type === "tool_end"
          ? "completed"
          : event.type === "approval_required"
            ? "approval"
            : "running",
      ...(event.type === "tool_end" ? { result: data.result } : {}),
    };
    tools = previous
      ? tools.map((item) => (item === previous ? tool : item))
      : [...tools, tool];
    const part = parts.find(
      (item) => item.kind === "tool" && item.tool === previous,
    );
    parts = part
      ? parts.map((item) => (item === part ? { ...part, tool } : item))
      : [
          ...parts,
          { id: `event:${event.seq}`, run_id: runId, kind: "tool", tool },
        ];
  }
  const status =
    event.type === "error"
      ? typeof data.status === "string"
        ? data.status
        : "failed"
      : RUN_EVENT_STATUS[event.type];
  if (status && runId) {
    if (
      ["completed", "cancelled", "interrupted", "failed"].includes(status) &&
      context?.run_id === runId
    )
      context = { ...context, compacting: false };
    const previous = runs.find((run) => run.id === runId);
    const run: Run = {
      ...(previous ?? {
        id: runId,
        conversation_id: "",
        created_at: Date.now() / 1000,
      }),
      status,
      updated_at: Date.now() / 1000,
      error:
        event.type === "error" || event.type === "interrupted"
          ? typeof data.message === "string"
            ? data.message
            : undefined
          : undefined,
    };
    runs = previous
      ? runs.map((item) => (item.id === runId ? run : item))
      : [...runs, run];
    if (["cancelled", "interrupted", "failed", "unknown"].includes(status)) {
      const terminal = (status === "unknown" ? "interrupted" : status) as
        "cancelled" | "interrupted" | "failed";
      const updates = new Map<ToolCall, ToolCall>();
      tools = tools.map((tool) => {
        if (
          tool.run_id !== runId ||
          !["running", "approval"].includes(tool.status)
        )
          return tool;
        const next = { ...tool, status: terminal };
        updates.set(tool, next);
        return next;
      });
      parts = parts.map((part) =>
        part.kind === "tool" && updates.has(part.tool)
          ? { ...part, tool: updates.get(part.tool)! }
          : part,
      );
    }
  }
  if (
    event.type === "agent_contact" &&
    typeof data.message_id === "string" &&
    !messages.some((message) => message.id === data.message_id)
  ) {
    messages = [
      ...messages,
      {
        id: data.message_id,
        role: "agent",
        content:
          typeof data.content === "string"
            ? data.content
            : JSON.stringify(data.content ?? {}, null, 2),
        reasoning: "",
        created_at: Date.now() / 1000,
        source: String(data.agent_id ?? ""),
      },
    ];
  }
  return { messages, runs, tools, parts, context, lastEventSeq: event.seq };
}

/** Snapshot messages are already aggregated; replay only reconstructs display parts. */
export function restoreChatState(detail: ConversationDetail): ChatState {
  const hasAgentMessage = detail.messages.some(
    (message) => message.role === "agent",
  );
  const events = [...(detail.events ?? [])]
    .filter((event) => !hasAgentMessage || event.type !== "agent_contact")
    .sort((a, b) => a.seq - b.seq);
  const restored = events.reduce(applyChatEvent, {
    messages: [],
    runs: [],
    tools: detail.tool_calls ?? [],
    parts: [],
  } as ChatState);
  const parts = [...(restored.parts ?? [])];
  // Older snapshots can contain aggregate messages without text events. Keep their
  // content visible and use it as the baseline when new deltas arrive.
  for (const message of detail.messages) {
    if (message.role !== "assistant" || !message.run_id) continue;
    const baseline: ChatPart[] = [];
    for (const kind of ["reasoning", "text"] as const) {
      const content = kind === "text" ? message.content : message.reasoning;
      if (
        content &&
        !parts.some(
          (part) => part.run_id === message.run_id && part.kind === kind,
        )
      )
        baseline.push({
          id: `snapshot:${message.id}:${kind}`,
          run_id: message.run_id,
          message_id: message.id,
          kind,
          content,
        });
    }
    const first = parts.findIndex((part) => part.run_id === message.run_id);
    parts.splice(first < 0 ? parts.length : first, 0, ...baseline);
  }
  return {
    ...restored,
    messages: [
      ...detail.messages,
      ...restored.messages.filter(
        (message) =>
          !detail.messages.some((stored) => stored.id === message.id),
      ),
    ],
    runs: detail.runs,
    parts,
    lastEventSeq: detail.last_event_seq ?? restored.lastEventSeq,
  };
}

export function messageParts(
  message: ChatMessage,
  state: ChatState,
): ChatPart[] {
  if (message.role !== "assistant" || !message.run_id) return [];
  const parts = (state.parts ?? []).filter(
    (part) => part.run_id === message.run_id,
  );
  const represented = new Set(
    parts.filter((part) => part.kind === "tool").map((part) => part.tool),
  );
  return [
    ...parts,
    ...state.tools
      .filter(
        (tool) => tool.run_id === message.run_id && !represented.has(tool),
      )
      .map((tool, index): ChatPart => ({
        id: `snapshot-tool:${tool.key ?? tool.id}:${index}`,
        run_id: tool.run_id,
        kind: "tool",
        tool,
      })),
  ];
}
