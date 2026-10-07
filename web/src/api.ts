import type {
  ChatEvent,
  Conversation,
  ConversationDetail,
  JsonObject,
  Operation,
  PlatformState,
  Session,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export class ApiClient {
  private csrfToken = "";

  async request<T>(
    path: string,
    options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
  ): Promise<T> {
    const method = options.method ?? "GET";
    const headers: Record<string, string> = { Accept: "application/json" };
    if (options.body !== undefined)
      headers["Content-Type"] = "application/json";
    if (method !== "GET" && this.csrfToken)
      headers["X-CSRF-Token"] = this.csrfToken;
    const response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      signal: options.signal,
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    if (!response.ok) {
      const body: unknown = await response.json().catch(() => null);
      throw new ApiError(
        errorMessage(body, response.statusText),
        response.status,
      );
    }
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  }

  async session(): Promise<Session> {
    const session = await this.request<Session>("/api/auth/session");
    this.csrfToken = session.csrf_token ?? "";
    return session;
  }

  async login(password: string): Promise<Session> {
    await this.request("/api/auth/login", {
      method: "POST",
      body: { password },
    });
    return this.session();
  }

  async logout(): Promise<void> {
    await this.request("/api/auth/logout", { method: "POST" });
    this.csrfToken = "";
  }

  state = (): Promise<PlatformState> => this.request("/api/state");
  async operations(): Promise<Operation[]> {
    const result = await this.request<{ operations: Operation[] }>(
      "/api/operations",
    );
    return result.operations;
  }
  operate = <T = JsonObject>(name: string, args: JsonObject): Promise<T> =>
    this.request(`/api/operations/${encodeURIComponent(name)}`, {
      method: "POST",
      body: { arguments: args, idempotency_key: randomOperationKey() },
    });

  async conversations(): Promise<Conversation[]> {
    const result = await this.request<{ conversations: Conversation[] }>(
      "/api/chat/conversations",
    );
    return result.conversations;
  }
  createConversation = (): Promise<Conversation> =>
    this.request("/api/chat/conversations", { method: "POST", body: {} });
  conversation = (id: string): Promise<ConversationDetail> =>
    this.request(`/api/chat/conversations/${encodeURIComponent(id)}`);
  sendMessage = (
    id: string,
    content: string,
  ): Promise<{ run_id: string; conversation_id: string }> =>
    this.request(`/api/chat/conversations/${encodeURIComponent(id)}/messages`, {
      method: "POST",
      body: { content },
    });
  cancelRun = (id: string): Promise<unknown> =>
    this.request(`/api/chat/runs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      body: {},
    });

  streamConversation(
    id: string,
    after: number,
    receive: (event: ChatEvent) => void,
    status: (connected: boolean, error?: Error) => void,
  ): () => void {
    const controller = new AbortController();
    let cursor = after;
    const connect = async (): Promise<void> => {
      let failures = 0;
      while (!controller.signal.aborted) {
        try {
          const response = await fetch(
            `/api/chat/conversations/${encodeURIComponent(id)}/events?after=${cursor}`,
            {
              headers: { Accept: "text/event-stream" },
              credentials: "same-origin",
              signal: controller.signal,
            },
          );
          if (!response.ok)
            throw new ApiError(
              errorMessage(
                await response.json().catch(() => null),
                response.statusText,
              ),
              response.status,
            );
          if (!response.body) throw new Error("服务器没有返回事件流。");
          status(true);
          failures = 0;
          await consumeSse(
            response.body,
            (data) => {
              const event = JSON.parse(data) as ChatEvent;
              if (event.seq > cursor) {
                cursor = event.seq;
                receive(event);
              }
            },
            controller.signal,
          );
          if (!controller.signal.aborted) status(false);
        } catch (error) {
          if (controller.signal.aborted) return;
          const failure =
            error instanceof Error ? error : new Error(String(error));
          status(false, failure);
          if (
            failure instanceof ApiError &&
            [401, 403, 404].includes(failure.status)
          )
            return;
          failures += 1;
        }
        await abortableDelay(
          Math.min(1000 * 2 ** failures, 15000),
          controller.signal,
        );
      }
    };
    void connect().catch((error: unknown) => {
      if (!controller.signal.aborted)
        status(
          false,
          error instanceof Error ? error : new Error(String(error)),
        );
    });
    return () => controller.abort();
  }
}

function errorMessage(body: unknown, fallback: string): string {
  if (typeof body === "object" && body !== null) {
    const value = body as Record<string, unknown>;
    if (typeof value.detail === "string") return value.detail;
    if (typeof value.message === "string") return value.message;
    if (typeof value.error === "string") return value.error;
    if (Array.isArray(value.detail))
      return value.detail
        .map((item: { msg?: string }) => item.msg ?? "参数格式无效")
        .join("；");
  }
  return fallback || "请求失败，请检查服务日志。";
}

export async function consumeSse(
  body: ReadableStream<Uint8Array>,
  receive: (data: string) => void,
  signal: AbortSignal,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const cancel = (): void => {
    void reader.cancel();
  };
  signal.addEventListener("abort", cancel, { once: true });
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let match: RegExpExecArray | null;
      while ((match = /\r?\n\r?\n/.exec(buffer)) !== null) {
        const frame = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const data = frame
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).replace(/^ /, ""))
          .join("\n");
        if (data) receive(data);
      }
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}

async function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return;
  await new Promise<void>((resolve) => {
    const finish = (): void => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, ms);
    signal.addEventListener("abort", finish, { once: true });
  });
}

export const api = new ApiClient();

function randomOperationKey(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}
