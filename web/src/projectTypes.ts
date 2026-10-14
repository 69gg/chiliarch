import type { JsonObject, Timestamp } from "./types";

export interface ProjectPolicy {
  require_signed_commits: boolean;
  require_signed_tags: boolean;
  required_checks: string[];
  require_review: boolean;
  observation_interval_seconds: number;
  allow_auto_merge: boolean;
  allow_release: boolean;
  merge_method: "merge" | "squash" | "rebase";
  branch_prefix: string;
}
export interface ProjectRelease {
  commands: Record<string, string[]>;
  tag_template: string;
  workflow: string | null;
  verify_command: string | null;
  verify_timeout_seconds: number;
  release_owner: "workflow" | "daemon";
}
export interface Project {
  id: string;
  name: string;
  repository: string;
  github_host: string;
  target_branch: string;
  policy: ProjectPolicy;
  release: ProjectRelease;
  created_at: Timestamp;
}
export interface ProjectBinding {
  project_id: string;
  agent_id: string;
  repo_path: string | null;
  observation: JsonObject;
  observed_at: Timestamp | null;
}
export interface ProjectWorkItem {
  id: string;
  project_id: string;
  goal_id: string | null;
  title: string;
  instruction: string;
  delivery_mode: "separate" | "integrated";
  depends_on: string[];
  acceptance_criteria: string[];
  fact_versions: Record<string, number>;
  stale_facts: string[];
  state: string;
  current_attempt_id: string | null;
  created_at: Timestamp;
}
export interface ProjectAttempt {
  id: string;
  work_item_id: string;
  agent_id: string;
  number: number;
  base_sha: string;
  branch: string;
  target_branch: string;
  state: string;
  workdir: string | null;
  task_id: string | null;
}
export interface ProjectCommand {
  id: string;
  project_id: string;
  agent_id: string;
  attempt_id: string | null;
  action: string;
  status: string;
  arguments: JsonObject;
  result: JsonObject;
  error: string | null;
  created_at: Timestamp;
  updated_at: Timestamp;
}
export interface ProjectFact {
  id: string;
  key: string;
  content: string;
  source: string;
  version: number;
  created_at: Timestamp;
}
export interface ProjectDelivery {
  id: string;
  work_item_id: string;
  attempt_id: string;
  pr_number: number;
  url: string;
  head_sha: string;
  base_sha: string;
  state: string;
  observation: JsonObject;
  reviews: {
    id: string;
    reviewer: string;
    head_sha: string;
    base_sha: string;
    decision: string;
    note: string;
    evidence: JsonObject[];
    current: boolean;
  }[];
  observed_at: Timestamp;
  merge_commit_sha: string | null;
}
export interface ProjectRule {
  id: string;
  event_type: string;
  allowed_actors: string[];
  label: string | null;
  command: string | null;
  objective_template: string;
  enabled: boolean;
}

export interface ProjectIssue {
  id: string;
  number: number;
  goal_id: string | null;
  observation: JsonObject;
  observed_at: Timestamp;
}

export interface ProjectDetail {
  project: Project;
  bindings: ProjectBinding[];
  work_items: ProjectWorkItem[];
  attempts: ProjectAttempt[];
  deliveries: ProjectDelivery[];
  commands: ProjectCommand[];
  facts: ProjectFact[];
  rules: ProjectRule[];
  issues: ProjectIssue[];
}

export type ProjectAction =
  | { kind: "create" }
  | { kind: "issue_read"; project: Project }
  | {
      kind: "issue_comment" | "issue_update";
      project: Project;
      issue: ProjectIssue;
    }
  | { kind: "cleanup"; project: Project; work: ProjectWorkItem }
  | { kind: "edit"; project: Project }
  | { kind: "bind"; project: Project }
  | { kind: "work"; project: Project }
  | { kind: "deliver"; project: Project; work: ProjectWorkItem }
  | { kind: "work_facts"; project: Project; work: ProjectWorkItem }
  | { kind: "command"; project: Project; work: ProjectWorkItem }
  | {
      kind: "review" | "merge" | "dispatch_review";
      project: Project;
      delivery: ProjectDelivery;
    }
  | { kind: "release"; project: Project }
  | { kind: "inspect_release"; project: Project }
  | { kind: "finalize"; project: Project }
  | { kind: "poll"; project: Project }
  | { kind: "fact"; project: Project; fact?: ProjectFact }
  | { kind: "rule"; project: Project; rule?: ProjectRule }
  | { kind: "start"; project: Project; work: ProjectWorkItem };
