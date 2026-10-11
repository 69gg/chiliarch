import type { PlatformState } from "../types";

export function platformState(): PlatformState {
  return {
    agents: [],
    tasks: [],
    approvals: [],
    requests: [],
    config: {
      model: {
        api_mode: "chat_completions",
        model: "test-model",
        base_url: "https://model.example/api",
        api_key: "********",
        headers: { Authorization: "********" },
        request_params: {},
        temperature: null,
        reasoning_enabled: false,
        context_window: 200000,
        anthropic_max_tokens: null,
      },
      supervisor: { mode: "auto" },
      logs: {
        level: "INFO",
        directory: "/tmp/logs",
        max_bytes: 10485760,
        backup_count: 10,
      },
      auth: { password_hash: "********", session_ttl_seconds: 86400 },
    },
    runtime: {
      config_revision: 1,
      pending_restart: [],
      last_config_error: null,
      protocol_version: "1.0",
    },
  };
}
