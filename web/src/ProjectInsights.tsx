import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import { ErrorNotice, JsonView, time } from "./components";
import { errorText } from "./projectUtils";
import type { JsonObject } from "./types";

interface Insights {
  timeline: JsonObject[];
  blockers: JsonObject[];
  usage: {
    totals: Record<string, number>;
    attempts: JsonObject[];
    missing_usage_count: number;
    source: string;
  };
}
export function ProjectInsights({
  projectId,
  revision,
}: {
  projectId: string;
  revision: number;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState<Insights | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    let active = true;
    void api
      .operate<Insights>("get_project_insights", { project_id: projectId })
      .then((result) => {
        if (active) {
          setValue(result);
          setError(null);
        }
      })
      .catch((failure: unknown) => {
        if (active) setError(errorText(failure));
      });
    return () => {
      active = false;
    };
  }, [projectId, revision, open]);
  return (
    <details
      className="project-insights"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>协作时间线与用量</summary>
      <ErrorNotice error={error} />
      {open && !value && !error && <p role="status">读取协作记录…</p>}
      {value && (
        <>
          <h4>已报告用量</h4>
          <p className="muted small">
            原生 agent 上报值，不代表账单或订阅剩余额度。
            {value.usage.missing_usage_count} 次执行尚无用量报告。
          </p>
          {Object.keys(value.usage.totals).length ? (
            <dl className="project-facts">
              {Object.entries(value.usage.totals).map(([key, amount]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>{amount.toLocaleString("zh-CN")}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="empty-line">暂无已报告用量</p>
          )}
          {value.blockers.length > 0 && (
            <details>
              <summary>需要处理的状态（{value.blockers.length}）</summary>
              <JsonView value={value.blockers} />
            </details>
          )}
          <h4>时间线</h4>
          {value.timeline.length ? (
            <ol className="project-timeline">
              {value.timeline.map((event, index) => (
                <li key={index}>
                  <div>
                    <strong>
                      {(
                        {
                          work_item: "创建工作项",
                          attempt: "执行尝试",
                          task: "任务状态",
                          command: "仓库操作",
                          delivery: "交付观察",
                          review: "评审",
                          fact: "共同事实",
                        } as Record<string, string>
                      )[String(event.kind)] ?? String(event.kind)}
                    </strong>
                    <time>
                      {time(
                        typeof event.at === "number" ||
                          typeof event.at === "string"
                          ? event.at
                          : null,
                      )}
                    </time>
                  </div>
                  <p>
                    {String(
                      event.title ??
                        event.branch ??
                        event.action ??
                        event.key ??
                        event.decision ??
                        event.state ??
                        "",
                    )}
                  </p>
                  <details>
                    <summary>关联对象与依据</summary>
                    <JsonView value={event} />
                  </details>
                </li>
              ))}
            </ol>
          ) : (
            <p className="empty-line">尚无协作事件</p>
          )}
        </>
      )}
    </details>
  );
}

interface Candidate {
  agent_id: string;
  name: string;
  eligible: boolean;
  reasons: string[];
  available_slots: number;
  operating_system: string | null;
  repository_observed_at: number | null;
  push_authentication: string;
  signing_availability: string;
}
const reasons: Record<string, string> = {
  offline_or_revoked: "节点离线或凭证已吊销",
  excluded_identity: "不符合独立身份要求",
  repository_executor_unavailable: "尚未上报仓库执行能力",
  interactive_agent_requires_manual_claim: "需要会话主动领取",
  task_workspace_unavailable: "不支持任务工作目录",
  native_execution_blocked: "原生执行受阻",
  operating_system_mismatch_or_unknown: "系统不符合要求或尚未知",
  missing_capabilities: "缺少所需能力",
  no_observed_capacity: "暂无已确认的空闲容量",
  repository_access_unverified: "仓库访问尚未检查",
  repository_identity_mismatch: "仓库身份与登记不一致",
};
export function ProjectCandidates({
  projectId,
}: {
  projectId: string;
}): ReactNode {
  const [os, setOs] = useState("");
  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    setCandidates(null);
    void api
      .operate<{ candidates: Candidate[] }>("recommend_project_agents", {
        project_id: projectId,
        operating_system: os || null,
        capabilities: [],
        exclude_agent_ids: [],
      })
      .then((result) => {
        if (active) {
          setCandidates(result.candidates);
          setError(null);
        }
      })
      .catch((failure: unknown) => {
        if (active) setError(errorText(failure));
      });
    return () => {
      active = false;
    };
  }, [projectId, os]);
  return (
    <details className="project-candidates">
      <summary>查看节点选择依据</summary>
      <label>
        建议节点的操作系统
        <select value={os} onChange={(event) => setOs(event.target.value)}>
          <option value="">不限</option>
          <option value="linux">Linux</option>
          <option value="darwin">macOS</option>
          <option value="windows">Windows</option>
        </select>
      </label>
      <ErrorNotice error={error} />
      {candidates?.map((candidate) => (
        <div className="project-candidate" key={candidate.agent_id}>
          <strong>
            {candidate.name} · {candidate.eligible ? "符合要求" : "待处理"}
          </strong>
          <p className="muted small">
            {candidate.operating_system ?? "系统未知"} · 可用容量{" "}
            {candidate.available_slots} · 仓库检查{" "}
            {time(candidate.repository_observed_at)}
          </p>
          {candidate.reasons.length > 0 && (
            <ul>
              {candidate.reasons.map((reason) => (
                <li key={reason}>{reasons[reason] ?? reason}</li>
              ))}
            </ul>
          )}
          <p className="muted small">
            推送认证：{candidate.push_authentication} · 签名：
            {candidate.signing_availability}
          </p>
        </div>
      ))}
    </details>
  );
}
