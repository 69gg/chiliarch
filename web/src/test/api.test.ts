import { describe, expect, it, vi } from "vitest";
import { ApiClient, consumeSse } from "../api";

describe("HTTP 身份与幂等", () => {
  it("从服务端包装结构读取完整操作模型", async () => {
    const operation = {
      name: "dispatch_task",
      description: "向 agent 派发任务",
      parameters: {
        type: "object",
        properties: { agent_id: { type: "string" } },
        required: ["agent_id"],
      },
      risk: "moderate",
      mutating: true,
    };
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ operations: [operation] }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetcher);
    const operations = await new ApiClient().operations();
    expect(operations).toEqual([operation]);
    expect(fetcher.mock.calls[0][0]).toBe("/api/operations");
  });

  it("cookie 认证后变更操作附带 CSRF 与唯一幂等键", async () => {
    const getRandomValues = crypto.getRandomValues.bind(crypto);
    vi.stubGlobal("crypto", { getRandomValues });
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ authenticated: true, csrf_token: "session-csrf" }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));
    fetcher.mockResolvedValueOnce(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const client = new ApiClient();
    await client.session();
    await client.operate("dispatch_task", {
      agent_id: "target",
      instruction: "执行测试",
    });
    await client.operate("dispatch_task", {
      agent_id: "target",
      instruction: "执行测试",
    });
    const [url, request] = fetcher.mock.calls[1] as [string, RequestInit];
    expect(url).toBe("/api/operations/dispatch_task");
    expect(request.credentials).toBe("same-origin");
    expect(request.headers).toMatchObject({ "X-CSRF-Token": "session-csrf" });
    const body = JSON.parse(String(request.body)) as {
      arguments: unknown;
      idempotency_key: string;
    };
    expect(body.arguments).toEqual({
      agent_id: "target",
      instruction: "执行测试",
    });
    expect(body.idempotency_key).toBeTruthy();
    const secondBody = JSON.parse(
      String((fetcher.mock.calls[2][1] as RequestInit).body),
    ) as { idempotency_key: string };
    expect(secondBody.idempotency_key).not.toBe(body.idempotency_key);
  });
});

describe("事件流解析", () => {
  it("在任意字节切分、CRLF 和多行 data 下完整恢复中文 JSON", async () => {
    const encoder = new TextEncoder();
    const bytes = encoder.encode(
      ': heartbeat\r\n\r\ndata: {"seq":1,\r\ndata: "content":"你好"}\r\n\r\ndata: {"seq":2}\n\n',
    );
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let index = 0; index < bytes.length; index += 3)
          controller.enqueue(bytes.slice(index, index + 3));
        controller.close();
      },
    });
    const received: unknown[] = [];
    await consumeSse(
      body,
      (data) => received.push(JSON.parse(data) as unknown),
      new AbortController().signal,
    );
    expect(received).toEqual([{ seq: 1, content: "你好" }, { seq: 2 }]);
  });
  it("丢弃在断线时未完整结束的帧", async () => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode('data: {"seq":1}\n\ndata: {"seq":2}'),
        );
        controller.close();
      },
    });
    const receive = vi.fn();
    await consumeSse(body, receive, new AbortController().signal);
    expect(receive).toHaveBeenCalledOnce();
    expect(JSON.parse(receive.mock.calls[0][0] as string)).toEqual({ seq: 1 });
  });
});
