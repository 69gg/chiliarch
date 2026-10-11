import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import { parseObject } from "./AgentsPage";
import { ErrorNotice, JsonView } from "./components";
import type { Json, JsonObject, PlatformState } from "./types";

const MASK = "********";
const SECTION_LABELS: Record<string, string> = {
  model: "总管模型",
  supervisor: "总管运行",
  transport: "连接与队列",
  artifacts: "共享产物",
  logs: "日志存储与轮换",
  server: "平台服务",
  auth: "登录与会话",
  database: "核心存储",
};
const LABELS: Record<string, string> = {
  api_mode: "模型 API",
  model: "模型名称",
  base_url: "API 地址",
  api_key: "API Key",
  headers: "额外请求头（JSON）",
  request_params: "额外请求参数（JSON）",
  timeout: "请求超时（秒）",
  retries: "请求重试次数",
  context_window: "模型上下文窗口（tokens）",
  anthropic_max_tokens: "Anthropic 最大输出 tokens",
  temperature: "Temperature",
  reasoning_effort: "推理强度",
  reasoning_enabled: "启用 reasoning",
  thinking_enabled: "启用 thinking",
  thinking_budget_tokens: "思考 token 预算",
  reasoning_replay: "回传兼容的 reasoning",
  include_usage: "请求 usage 信息",
  system_prompt_as_user: "系统提示词使用 user 角色",
  tool_choice: "Tool choice",
  mode: "运行模式",
  run_timeout: "运行超时（秒）",
  event_poll_seconds: "事件刷新间隔（秒）",
  inbox_poll_seconds: "下级请求检查间隔（秒）",
  task_stall_seconds: "任务进度停滞判定（秒）",
  model_recovery_attempts: "模型故障自动恢复次数",
  model_recovery_base_seconds: "模型恢复起始等待（秒）",
  model_recovery_max_seconds: "模型恢复最长等待（秒）",
  system_prompt: "总管系统提示词",
  heartbeat_seconds: "心跳间隔（秒）",
  offline_after_seconds: "离线判定（秒）",
  poll_timeout_seconds: "长轮询超时（秒）",
  retry_seconds: "重投间隔（秒）",
  queue_limit: "离线队列上限",
  max_artifact_bytes: "单个产物大小上限（字节）",
  chunk_bytes: "产物分块大小（字节）",
  max_total_bytes: "产物总存储上限（字节）",
  message_ttl_seconds: "消息有效期（秒）",
  max_message_bytes: "单条消息上限（字节）",
  reconnect_base_seconds: "重连起始间隔（秒）",
  reconnect_max_seconds: "重连最大间隔（秒）",
  upgrade_interval_seconds: "传输升级检查（秒）",
  directory: "日志目录",
  max_bytes: "每个日志文件上限（字节）",
  backup_count: "保留轮换文件数",
  level: "日志级别",
  host: "监听地址",
  port: "监听端口",
  public_url: "平台公开地址",
  config_poll_seconds: "配置文件检查间隔（秒）",
  graceful_shutdown_seconds: "关闭时等待请求结束（秒）",
  session_ttl_seconds: "会话有效期（秒）",
  max_login_attempts: "登录尝试上限",
  login_window_seconds: "登录限流窗口（秒）",
  secure_cookie: "仅通过 HTTPS 发送 cookie",
  url: "数据库连接地址",
};
const OPTIONS: Record<string, [string, string][]> = {
  api_mode: [
    ["chat_completions", "OpenAI · Chat Completions"],
    ["responses", "OpenAI · Responses"],
    ["anthropic", "Anthropic · Messages"],
  ],
  mode: [
    ["ask", "ask · 每次变更询问"],
    ["auto", "auto · 高风险询问"],
    ["yolo", "yolo · 自主运行"],
  ],
  level: [
    ["DEBUG", "DEBUG"],
    ["INFO", "INFO"],
    ["WARNING", "WARNING"],
    ["ERROR", "ERROR"],
  ],
};
const NULLABLE_NUMBERS = new Set([
  "temperature",
  "thinking_budget_tokens",
  "anthropic_max_tokens",
]);

export function configDraft(config: JsonObject): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [section, values] of Object.entries(config)) {
    if (!values || typeof values !== "object" || Array.isArray(values))
      continue;
    for (const [field, value] of Object.entries(values)) {
      if (field === "password_hash") continue;
      result[`${section}.${field}`] =
        field === "api_key"
          ? ""
          : value === null
            ? ""
            : typeof value === "object"
              ? JSON.stringify(value, null, 2)
              : String(value);
    }
  }
  return result;
}

export function configPatch(
  config: JsonObject,
  draft: Record<string, string>,
  clearKey: boolean,
): JsonObject {
  const original = configDraft(config);
  const patch: JsonObject = {};
  for (const [path, text] of Object.entries(draft)) {
    const [section, field] = path.split(".");
    if (
      field === "anthropic_max_tokens" &&
      draft["model.api_mode"] !== "anthropic"
    )
      continue;
    if (field === "api_key" && !text) continue;
    if (text === original[path]) continue;
    const values = config[section] as JsonObject;
    const previous = values[field];
    let value: Json;
    if (field === "headers" || field === "request_params")
      value = parseObject(text, LABELS[field]);
    else if (typeof previous === "boolean") value = text === "true";
    else if (typeof previous === "number" || NULLABLE_NUMBERS.has(field)) {
      if (!text && NULLABLE_NUMBERS.has(field)) value = null;
      else {
        value = Number(text);
        if (!text || !Number.isFinite(value))
          throw new Error(`${LABELS[field] ?? field}需要有效数字。`);
        if (
          field === "anthropic_max_tokens" &&
          (!Number.isInteger(value) || value < 1)
        )
          throw new Error(`${LABELS[field]}需要正整数。`);
      }
    } else value = text || (previous === null ? null : "");
    const part = (patch[section] ??= {}) as JsonObject;
    part[field] = value;
  }
  if (clearKey) {
    const model = (patch.model ??= {}) as JsonObject;
    model.api_key = "";
  }
  return patch;
}

export default function ConfigPage({
  state,
  refresh,
}: {
  state: PlatformState;
  refresh: () => void;
}): ReactNode {
  const [original, setOriginal] = useState(state.config);
  const [draft, setDraft] = useState(() => configDraft(state.config));
  const [revision, setRevision] = useState(state.runtime.config_revision);
  const [clearKey, setClearKey] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [logs, setLogs] = useState<Json | undefined>();
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const initial = configDraft(original);
  const dirty =
    clearKey ||
    Object.entries(draft).some(
      ([path, value]) =>
        (path !== "model.anthropic_max_tokens" ||
          draft["model.api_mode"] === "anthropic") &&
        initial[path] !== value,
    );

  useEffect(() => {
    if (!dirty && revision !== state.runtime.config_revision) {
      setOriginal(state.config);
      setDraft(configDraft(state.config));
      setRevision(state.runtime.config_revision);
    }
  }, [state.config, state.runtime.config_revision, dirty, revision]);

  async function save(event: FormEvent): Promise<void> {
    event.preventDefault();
    setError(null);
    setNotice(null);
    let patch: JsonObject;
    try {
      patch = configPatch(original, draft, clearKey);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      return;
    }
    setBusy(true);
    try {
      const result = await api.operate("update_config", { patch });
      const next = result.config;
      if (next && typeof next === "object" && !Array.isArray(next)) {
        setOriginal(next);
        setDraft(configDraft(next));
      }
      if (typeof result.revision === "number") setRevision(result.revision);
      setClearKey(false);
      const pending = Array.isArray(result.pending_restart)
        ? result.pending_restart.filter(
            (value): value is string => typeof value === "string",
          )
        : [];
      setNotice(
        pending.length
          ? `配置已保存。${pending.map((path) => LABELS[path.split(".")[1]] ?? path).join("、")}需重启后生效。`
          : "配置已保存。运行中的模型调用保留原配置，后续运行使用新配置。",
      );
      refresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  async function readLogs(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      setLogs(await api.operate("get_logs", {}));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  async function changePassword(event: FormEvent): Promise<void> {
    event.preventDefault();
    setError(null);
    setNotice(null);
    if (password !== confirmPassword) {
      setError("两次输入的密码不一致。");
      return;
    }
    setBusy(true);
    try {
      await api.operate("change_password", { password });
      setPassword("");
      setConfirmPassword("");
      setNotice("登录密码已更新。");
      refresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  const groups = [
    "model",
    "supervisor",
    "transport",
    "artifacts",
    "logs",
    "server",
    "auth",
    "database",
  ].filter((section) => original[section]);

  return (
    <div className="configuration-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">平台管理</p>
          <h1>配置</h1>
        </div>
        <span className="header-meta">
          配置版本 {state.runtime.config_revision}
        </span>
      </header>
      <div className="configuration-content">
        <ErrorNotice error={state.runtime.last_config_error} />
        <ErrorNotice error={error} onDismiss={() => setError(null)} />
        {notice && (
          <div className="notice" role="status">
            {notice}
          </div>
        )}
        {revision !== state.runtime.config_revision && dirty && (
          <div className="notice" role="status">
            配置已在其他位置更新。当前保留你的编辑，保存时仅提交改动字段。
          </div>
        )}
        <form onSubmit={(event) => void save(event)}>
          {groups.map((section) => (
            <section className="settings-section" key={section}>
              <div className="settings-heading">
                <h2>{SECTION_LABELS[section]}</h2>
                {section === "model" && (
                  <p className="muted">
                    OpenAI
                    默认不设置输出上限，可通过额外请求参数显式配置；Anthropic
                    需要单独填写输出上限。上下文达到窗口的 90% 时自动压缩。
                  </p>
                )}
                {section === "server" && (
                  <p className="muted">
                    监听地址、端口与数据库连接地址的修改需要重启。
                  </p>
                )}
                {section === "supervisor" && (
                  <p className="muted">
                    任务停滞时通知总管核查；临时模型故障按退避间隔恢复。用户主动停止不会自动恢复。
                  </p>
                )}
              </div>
              <div className="settings-grid">
                {Object.entries(original[section] as JsonObject)
                  .filter(([field]) => field !== "password_hash")
                  .filter(
                    ([field]) =>
                      field !== "anthropic_max_tokens" ||
                      draft["model.api_mode"] === "anthropic",
                  )
                  .map(([field, value]) => (
                    <ConfigField
                      key={`${section}.${field}`}
                      section={section}
                      field={field}
                      original={value}
                      value={draft[`${section}.${field}`] ?? ""}
                      onChange={(text) =>
                        setDraft((previous) => ({
                          ...previous,
                          [`${section}.${field}`]: text,
                        }))
                      }
                    />
                  ))}
                {section === "model" && (
                  <label className="checkbox-field">
                    <input
                      type="checkbox"
                      checked={clearKey}
                      onChange={(event) => setClearKey(event.target.checked)}
                    />
                    清除已保存的 API Key
                  </label>
                )}
              </div>
            </section>
          ))}
          <div className="settings-save">
            <p className="muted small">
              {dirty ? "有尚未保存的修改" : "配置没有未保存的修改"}
            </p>
            <div className="actions">
              <button
                type="button"
                disabled={busy || !dirty}
                onClick={() => {
                  setOriginal(state.config);
                  setDraft(configDraft(state.config));
                  setRevision(state.runtime.config_revision);
                  setClearKey(false);
                }}
              >
                放弃编辑
              </button>
              <button className="primary" disabled={busy || !dirty}>
                {busy ? "保存中…" : "保存配置"}
              </button>
            </div>
          </div>
        </form>
        <section className="settings-section">
          <div className="settings-heading">
            <h2>修改登录密码</h2>
            <p className="muted">密码只用于单用户登录，不会提供给总管模型。</p>
          </div>
          <form
            className="password-form"
            onSubmit={(event) => void changePassword(event)}
          >
            <label htmlFor="new-password">
              新密码
              <input
                id="new-password"
                autoComplete="new-password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
              />
            </label>
            <label htmlFor="confirm-password">
              确认新密码
              <input
                id="confirm-password"
                autoComplete="new-password"
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                required
              />
            </label>
            <button disabled={busy || !password || !confirmPassword}>
              更新密码
            </button>
          </form>
        </section>
        <section className="settings-section">
          <div className="row-between">
            <h2>最近日志</h2>
            <button disabled={busy} onClick={() => void readLogs()}>
              读取日志
            </button>
          </div>
          {logs === undefined ? (
            <p className="empty-line">按需读取平台日志</p>
          ) : (
            <JsonView value={logs} />
          )}
        </section>
      </div>
    </div>
  );
}

function ConfigField({
  section,
  field,
  original,
  value,
  onChange,
}: {
  section: string;
  field: string;
  original: Json;
  value: string;
  onChange: (value: string) => void;
}): ReactNode {
  const id = `config-${section}-${field}`;
  const label = LABELS[field] ?? field;
  if (typeof original === "boolean")
    return (
      <label className="checkbox-field" htmlFor={id}>
        <input
          id={id}
          type="checkbox"
          checked={value === "true"}
          onChange={(event) => onChange(String(event.target.checked))}
        />
        {label}
      </label>
    );
  if (OPTIONS[field])
    return (
      <label htmlFor={id}>
        {label}
        <select
          id={id}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        >
          {OPTIONS[field].map(([key, text]) => (
            <option value={key} key={key}>
              {text}
            </option>
          ))}
        </select>
      </label>
    );
  if (
    (typeof original === "object" && original !== null) ||
    field === "system_prompt"
  )
    return (
      <label className="wide-field" htmlFor={id}>
        {label}
        <textarea
          id={id}
          aria-label={label}
          className={field === "system_prompt" ? "" : "mono"}
          rows={field === "system_prompt" ? 5 : 4}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          spellCheck={false}
        />
        {field === "headers" && (
          <small className="muted">
            保留 ******** 会沿用已保存的值；移除键可以删除请求头。
          </small>
        )}
      </label>
    );
  const secret = field === "api_key";
  const numeric = typeof original === "number" || NULLABLE_NUMBERS.has(field);
  const positiveInteger =
    field === "context_window" || field === "anthropic_max_tokens";
  return (
    <label htmlFor={id}>
      {label}
      <input
        id={id}
        aria-label={label}
        aria-describedby={secret || positiveInteger ? `${id}-help` : undefined}
        type={secret ? "password" : numeric ? "number" : "text"}
        step={positiveInteger ? "1" : numeric ? "any" : undefined}
        min={positiveInteger ? 1 : undefined}
        value={value}
        autoComplete={secret ? "new-password" : "off"}
        onChange={(event) => onChange(event.target.value)}
        placeholder={
          secret
            ? original === MASK
              ? "已设置 · 留空保持原值"
              : "尚未设置"
            : field === "anthropic_max_tokens"
              ? "发送 Anthropic 请求前请填写"
              : original === null
                ? "使用模型默认值"
                : undefined
        }
      />
      {secret && (
        <small id={`${id}-help`} className="muted">
          只写入新凭证，现有凭证不回传。
        </small>
      )}
      {field === "context_window" && (
        <small id={`${id}-help`} className="muted">
          默认 200,000；请填写模型实际支持的窗口。压缩后保留完整聊天记录。
        </small>
      )}
      {field === "anthropic_max_tokens" && (
        <small id={`${id}-help`} className="muted">
          Anthropic Messages 必填，请按模型能力设置正整数；不影响 OpenAI 请求。
        </small>
      )}
    </label>
  );
}
