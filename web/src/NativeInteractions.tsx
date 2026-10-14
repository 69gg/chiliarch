import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import { ErrorNotice, JsonView, time } from "./components";
import type { JsonObject, PlatformState, Timestamp } from "./types";

interface Choice {
  id: string;
  label: string;
}
interface NativeField {
  id: string;
  label: string;
  options: Choice[];
  secret: boolean;
  multiple: boolean;
}
export interface NativeInteraction {
  id: string;
  agent_id: string;
  task_id: string;
  kind: "permission" | "input";
  title: string;
  native_method: string;
  details: JsonObject;
  options: Choice[];
  fields: NativeField[];
  status: "pending" | "answered" | "expired";
  created_at: Timestamp;
  expires_at: Timestamp | null;
  answered_at: Timestamp | null;
}

export default function NativeInteractions({
  state,
  refresh,
}: {
  state: PlatformState;
  refresh: () => void;
}): ReactNode {
  const [interactions, setInteractions] = useState<NativeInteraction[]>([]);
  const [revision, setRevision] = useState(0);
  const [opened, setOpened] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const pollSeconds = Number(
    (state.config.server as JsonObject | undefined)?.config_poll_seconds ?? 5,
  );
  useEffect(() => {
    const timer = setInterval(
      () => setRevision((value) => value + 1),
      pollSeconds * 1000,
    );
    return () => clearInterval(timer);
  }, [pollSeconds]);
  useEffect(() => {
    let active = true;
    void api
      .operate<{ interactions: NativeInteraction[] }>(
        "list_native_interactions",
        { status: "pending" },
      )
      .then((result) => {
        if (active) {
          setInteractions(result.interactions);
          setError(null);
        }
      })
      .catch((failure: unknown) => {
        if (active)
          setError(
            failure instanceof Error ? failure.message : String(failure),
          );
      });
    return () => {
      active = false;
    };
  }, [revision]);
  if (!interactions.length && !opened && !error && !notice) return null;
  return (
    <div className="native-interactions">
      {interactions.length > 0 && (
        <button
          className="native-pending-button"
          onClick={() => {
            setOpened(true);
            setNotice(null);
          }}
          aria-haspopup="dialog"
        >
          <span className="status-dot" />
          {interactions.length} 个原生请求待处理
        </button>
      )}
      {error && (
        <div className="native-load-error">
          <ErrorNotice error={`原生请求读取失败：${error}`} />
          <button onClick={() => setRevision((value) => value + 1)}>
            重新读取请求
          </button>
        </div>
      )}
      {notice && (
        <div className="notice native-notice" role="status">
          {notice}
          <button
            className="plain"
            aria-label="关闭请求提示"
            onClick={() => setNotice(null)}
          >
            ×
          </button>
        </div>
      )}
      {opened && (
        <InteractionDialog
          interactions={interactions}
          state={state}
          onClose={() => setOpened(false)}
          onAnswered={(id) => {
            setInteractions((items) => items.filter((item) => item.id !== id));
            setNotice("回答已提交，等待原生 agent 接收并继续。");
            setRevision((value) => value + 1);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function InteractionDialog({
  interactions,
  state,
  onClose,
  onAnswered,
}: {
  interactions: NativeInteraction[];
  state: PlatformState;
  onClose: () => void;
  onAnswered: (id: string) => void;
}): ReactNode {
  const dialog = useRef<HTMLDialogElement>(null);
  const [selected, setSelected] = useState(interactions[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const interaction =
    interactions.find((item) => item.id === selected) ?? interactions[0];
  useEffect(() => {
    const element = dialog.current;
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    element?.showModal();
    return () => {
      element?.close();
      previousFocus?.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className="agent-pairing-dialog native-dialog"
      aria-labelledby="native-dialog-title"
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onClose();
      }}
    >
      <div className="agent-dialog-header">
        <h2 id="native-dialog-title">原生 agent 请求</h2>
        <button
          className="plain"
          aria-label="关闭原生请求"
          disabled={busy}
          onClick={onClose}
        >
          ×
        </button>
      </div>
      {interactions.length > 1 && (
        <label className="native-request-select">
          选择待处理请求
          <select
            disabled={busy}
            value={interaction?.id}
            onChange={(event) => setSelected(event.target.value)}
          >
            {interactions.map((item) => (
              <option key={item.id} value={item.id}>
                {state.agents.find((agent) => agent.id === item.agent_id)
                  ?.name ?? item.agent_id}{" "}
                · {item.title}
              </option>
            ))}
          </select>
        </label>
      )}
      {interaction ? (
        <NativeForm
          key={interaction.id}
          interaction={interaction}
          state={state}
          onBusy={setBusy}
          onAnswered={() => onAnswered(interaction.id)}
        />
      ) : (
        <p className="native-empty" role="status">
          当前没有待处理的原生请求。已回答或失效的请求不会继续接受输入。
        </p>
      )}
    </dialog>
  );
}

function NativeForm({
  interaction,
  state,
  onBusy,
  onAnswered,
}: {
  interaction: NativeInteraction;
  state: PlatformState;
  onBusy: (value: boolean) => void;
  onAnswered: () => void;
}): ReactNode {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef(false);
  async function answer(nativeAnswer: JsonObject): Promise<void> {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    onBusy(true);
    setError(null);
    try {
      await api.operate("resolve_native_interaction", {
        interaction_id: interaction.id,
        native_answer: nativeAnswer,
      });
      onAnswered();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      pending.current = false;
      setBusy(false);
      onBusy(false);
    }
  }
  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const fields: JsonObject = {};
    for (const field of interaction.fields)
      fields[field.id] = field.multiple
        ? form.getAll(field.id).map(String)
        : String(form.get(field.id) ?? "");
    void answer({ fields });
  }
  const agent = state.agents.find((item) => item.id === interaction.agent_id);
  return (
    <div className="native-request-body">
      <p className="muted small">
        {agent?.name ?? interaction.agent_id} · {agent?.machine ?? ""} ·{" "}
        {time(interaction.created_at)}
      </p>
      <h3>{interaction.title}</h3>
      <p className="muted small">
        任务 {interaction.task_id}
        {interaction.expires_at
          ? ` · 截止 ${time(interaction.expires_at)}`
          : ""}
      </p>
      <details className="native-original">
        <summary>原生请求详情</summary>
        <p className="small muted">{interaction.native_method}</p>
        <JsonView value={interaction.details} />
      </details>
      <ErrorNotice error={error} />
      {interaction.kind === "permission" ? (
        <>
          <p className="small muted">
            这次原生权限请求需要你的决定。将按原始选项返回给 agent。
          </p>
          <div className="actions native-choices">
            {interaction.options.map((option) => (
              <button
                key={option.id}
                disabled={busy}
                onClick={() => void answer({ choice: option.id })}
              >
                {option.label}
              </button>
            ))}
          </div>
          {!interaction.options.length && (
            <p className="notice">
              原生请求没有可用选项，请在该 agent 的本机查看请求。
            </p>
          )}
        </>
      ) : (
        <form className="project-form" onSubmit={submit}>
          {interaction.fields.map((field) => (
            <label key={field.id}>
              {field.label}
              {field.options.length ? (
                <select
                  name={field.id}
                  multiple={field.multiple}
                  required
                  disabled={busy}
                  defaultValue={field.multiple ? [] : ""}
                >
                  {!field.multiple && (
                    <option value="" disabled>
                      请选择
                    </option>
                  )}
                  {field.options.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  name={field.id}
                  type={field.secret ? "password" : "text"}
                  required
                  disabled={busy}
                  autoComplete="off"
                />
              )}
            </label>
          ))}
          {interaction.fields.length > 0 ? (
            <button className="primary" disabled={busy}>
              {busy ? "提交中…" : "提交回答"}
            </button>
          ) : (
            <p className="notice">
              原生请求没有可填写字段，请在该 agent 的本机查看请求。
            </p>
          )}
        </form>
      )}
    </div>
  );
}
