import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import { ErrorNotice, JsonView, time } from "./components";
import type {
  ArtifactReference,
  JsonObject,
  PlatformState,
  Timestamp,
} from "./types";

interface Checkpoint {
  id: string;
  task_id: string;
  agent_id: string;
  summary: string;
  artifacts: ArtifactReference[];
  waiting_for: string[];
  state: string;
  resumed_task_id: string | null;
  updated_at: Timestamp;
  dependencies: {
    task_id: string;
    original_task_id: string;
    status: string;
    review_status: string;
  }[];
}
const stageNames: Record<string, string> = {
  awaiting_exit: "等待原生进程结束",
  paused: "等待依赖验收",
  ready: "可以继续",
  unknown: "执行结果未知",
  failed: "上次执行未成功",
  resumed: "已续接",
};
export default function TaskCheckpoints({
  agentId,
  state,
  refresh,
}: {
  agentId: string;
  state: PlatformState;
  refresh: () => void;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [revision, setRevision] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const pollSeconds = Number(
    (state.config.server as JsonObject | undefined)?.config_poll_seconds ?? 5,
  );
  useEffect(() => {
    if (!open) return;
    const timer = setInterval(
      () => setRevision((value) => value + 1),
      pollSeconds * 1000,
    );
    return () => clearInterval(timer);
  }, [open, pollSeconds]);
  useEffect(() => {
    if (!open) return;
    let active = true;
    void api
      .operate<{ checkpoints: Checkpoint[] }>("list_task_checkpoints", {})
      .then((result) => {
        if (active) {
          setCheckpoints(
            result.checkpoints.filter(
              (checkpoint) => checkpoint.agent_id === agentId,
            ),
          );
          setLoaded(true);
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
  }, [open, revision, agentId]);
  const tasks = state.tasks.filter(
    (task) =>
      task.agent_id === agentId &&
      ["running", "accepted", "delivered"].includes(task.status) &&
      !checkpoints.some((checkpoint) => checkpoint.task_id === task.id),
  );
  return (
    <details
      className="task-checkpoints"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>阶段与续接</summary>
      <p className="muted small">
        保存工作进度与等待条件。原生任务实际结束、依赖验收通过后，才能在原会话和目录继续。
      </p>
      <ErrorNotice error={error} />
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {open && !loaded && !error && (
        <p role="status" className="muted small">
          读取阶段记录…
        </p>
      )}
      {loaded && (
        <>
          <button onClick={() => setSaving(true)} disabled={!tasks.length}>
            保存任务阶段
          </button>
          {!checkpoints.length && <p className="empty-line">尚无阶段记录</p>}
          {checkpoints.map((checkpoint) => (
            <article
              className="checkpoint-record"
              key={checkpoint.id}
              aria-label={`阶段 ${checkpoint.task_id}`}
            >
              <div className="row-between">
                <strong>
                  {state.tasks.find((task) => task.id === checkpoint.task_id)
                    ?.instruction ?? checkpoint.task_id}
                </strong>
                <span className="muted small">
                  {stageNames[checkpoint.state] ?? checkpoint.state}
                </span>
              </div>
              <p className="checkpoint-summary">{checkpoint.summary}</p>
              <p className="muted small">{time(checkpoint.updated_at)}</p>
              {checkpoint.state === "awaiting_exit" && (
                <p className="notice">
                  阶段已保存，原生进程尚未确认结束；这不会终止任务或释放工作区。
                </p>
              )}
              {checkpoint.state === "unknown" && (
                <p className="notice">
                  先核查原生进程和已有副作用，不能直接续接未知状态。
                </p>
              )}
              {checkpoint.waiting_for.length > 0 && (
                <ul className="checkpoint-dependencies">
                  {checkpoint.waiting_for.map((id) => {
                    const dependency = checkpoint.dependencies.find(
                      (item) => item.original_task_id === id,
                    );
                    return (
                      <li key={id}>
                        {state.tasks.find((task) => task.id === id)
                          ?.instruction ?? id}{" "}
                        ·{" "}
                        {dependency?.status === "completed" &&
                        dependency.review_status === "accepted"
                          ? "已完成并验收"
                          : "等待完成与验收"}
                      </li>
                    );
                  })}
                </ul>
              )}
              {checkpoint.artifacts.length > 0 && (
                <details>
                  <summary>阶段产物</summary>
                  <JsonView
                    value={checkpoint.artifacts.map((artifact) => ({
                      ...artifact,
                    }))}
                  />
                </details>
              )}
              {checkpoint.resumed_task_id && (
                <p className="small">
                  续接任务：<code>{checkpoint.resumed_task_id}</code>
                </p>
              )}
              {checkpoint.state === "ready" && !checkpoint.resumed_task_id && (
                <ResumeStage
                  key={checkpoint.id}
                  checkpoint={checkpoint}
                  onResumed={(result) => {
                    setNotice(
                      result.status === "awaiting_approval"
                        ? "续接正在等待许可。"
                        : "续接指令已登记，可在任务列表查看实际执行状态。",
                    );
                    setRevision((value) => value + 1);
                    refresh();
                  }}
                />
              )}
            </article>
          ))}
        </>
      )}
      {saving && (
        <SaveStage
          agentId={agentId}
          state={state}
          excluded={checkpoints.map((item) => item.task_id)}
          onClose={() => setSaving(false)}
          onSaved={() => {
            setSaving(false);
            setNotice("阶段已保存；等待原生任务实际结束后判断是否可以继续。");
            setRevision((value) => value + 1);
            refresh();
          }}
        />
      )}
    </details>
  );
}
function ResumeStage({
  checkpoint,
  onResumed,
}: {
  checkpoint: Checkpoint;
  onResumed: (result: JsonObject) => void;
}): ReactNode {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);
  async function resume(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy || submitted) return;
    const instruction = String(
      new FormData(event.currentTarget).get("instruction") ?? "",
    ).trim();
    setBusy(true);
    setError(null);
    try {
      const result = await api.operate("resume_task_checkpoint", {
        checkpoint_id: checkpoint.id,
        instruction: instruction || null,
      });
      setSubmitted(true);
      onResumed(result);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      className="checkpoint-resume"
      onSubmit={(event) => void resume(event)}
    >
      <label>
        下一阶段指令（可选）
        <textarea
          name="instruction"
          rows={2}
          placeholder="留空按保存的摘要与新依赖结果继续"
        />
      </label>
      <ErrorNotice error={error} />
      <button disabled={busy || submitted}>
        {busy ? "登记中…" : submitted ? "已登记续接" : "继续下一阶段"}
      </button>
    </form>
  );
}
function SaveStage({
  agentId,
  state,
  excluded,
  onClose,
  onSaved,
}: {
  agentId: string;
  state: PlatformState;
  excluded: string[];
  onClose: () => void;
  onSaved: () => void;
}): ReactNode {
  const dialog = useRef<HTMLDialogElement>(null);
  const [taskId, setTaskId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactReference[]>([]);
  useEffect(() => {
    const element = dialog.current;
    const focus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    element?.showModal();
    element?.querySelector<HTMLElement>("select")?.focus();
    return () => {
      element?.close();
      focus?.focus();
    };
  }, []);
  async function save(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy) return;
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      await api.operate("save_task_checkpoint", {
        task_id: taskId,
        summary: String(data.get("summary") ?? "").trim(),
        artifacts: artifacts.map((artifact) => ({
          ...artifact,
          name: artifact.name.trim(),
          uri: artifact.uri.trim(),
        })),
        waiting_for: data.getAll("dependency").map(String),
      });
      onSaved();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }
  function updateArtifact(
    index: number,
    key: "name" | "uri",
    value: string,
  ): void {
    setArtifacts((items) =>
      items.map((item, i) => (i === index ? { ...item, [key]: value } : item)),
    );
  }
  return (
    <dialog
      ref={dialog}
      className="agent-pairing-dialog"
      aria-labelledby="save-stage-title"
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onClose();
      }}
    >
      <div className="agent-dialog-header">
        <h2 id="save-stage-title">保存任务阶段</h2>
        <button
          className="plain"
          disabled={busy}
          onClick={onClose}
          aria-label="关闭阶段表单"
        >
          ×
        </button>
      </div>
      <form className="project-form" onSubmit={(event) => void save(event)}>
        <label>
          原生任务
          <select
            required
            value={taskId}
            onChange={(event) => setTaskId(event.target.value)}
          >
            <option value="" disabled>
              选择正在执行的任务
            </option>
            {state.tasks
              .filter(
                (task) =>
                  task.agent_id === agentId &&
                  ["running", "accepted", "delivered"].includes(task.status) &&
                  !excluded.includes(task.id),
              )
              .map((task) => (
                <option key={task.id} value={task.id}>
                  {task.instruction}
                </option>
              ))}
          </select>
        </label>
        <label>
          阶段摘要与下一步
          <textarea
            name="summary"
            required
            rows={5}
            placeholder="已完成什么、保存的修改、下一步以及尚未确认的副作用"
          />
        </label>
        <fieldset>
          <legend>等待完成与验收的任务</legend>
          {state.tasks
            .filter((task) => task.id !== taskId)
            .map((task) => (
              <label className="project-check" key={task.id}>
                <input type="checkbox" name="dependency" value={task.id} />
                {task.instruction}
              </label>
            ))}
        </fieldset>
        <fieldset>
          <legend>已保存的产物引用</legend>
          {artifacts.map((artifact, index) => (
            <div className="checkpoint-artifact" key={index}>
              <label>
                产物 {index + 1} 名称
                <input
                  required
                  value={artifact.name}
                  onChange={(event) =>
                    updateArtifact(index, "name", event.target.value)
                  }
                />
              </label>
              <label>
                产物 {index + 1} 地址
                <input
                  required
                  value={artifact.uri}
                  onChange={(event) =>
                    updateArtifact(index, "uri", event.target.value)
                  }
                />
              </label>
              <button
                type="button"
                onClick={() =>
                  setArtifacts((items) => items.filter((_, i) => i !== index))
                }
              >
                移除产物 {index + 1}
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={() =>
              setArtifacts((items) => [
                ...items,
                { name: "", uri: "", kind: "reference" },
              ])
            }
          >
            添加产物引用
          </button>
        </fieldset>
        <p className="muted small">
          只保存阶段信息，不会停止进程。agent
          需要正常结束这一阶段，平台才会确认释放资源。
        </p>
        <ErrorNotice error={error} />
        <div className="actions goal-dialog-actions">
          <button type="button" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button className="primary" disabled={busy}>
            {busy ? "保存中…" : "保存阶段"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
