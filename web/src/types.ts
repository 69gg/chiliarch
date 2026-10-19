export type Json =
  null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export type Timestamp = string | number;

export interface Agent {
  id: string;
  name: string;
  machine: string;
  capabilities: string[];
  revoked: boolean;
  online: boolean;
  created_at: Timestamp;
  last_seen: Timestamp | null;
  transport?: string | null;
  metadata: JsonObject;
  introduction?: string | null;
}

export interface Task {
  id: string;
  agent_id: string;
  instruction: string;
  workdir?: string | null;
  parameters: JsonObject;
  status: string;
  progress: Json;
  result: Json;
  error: string | null;
  message?: string | null;
  created_at: Timestamp;
  updated_at: Timestamp;
  requested_by: string;
}

export interface ArtifactReference {
  name: string;
  uri: string;
  kind: string;
  digest?: string | null;
}

export interface Goal {
  id: string;
  title: string;
  objective: string;
  constraints: string[];
  acceptance_criteria: string[];
  status: "active" | "blocked" | "completed" | "cancelled";
  conversation_id: string | null;
  summary: string;
  created_at: Timestamp;
  updated_at: Timestamp;
}

export interface TaskLink {
  task_id: string;
  goal_id: string | null;
  conversation_id: string | null;
  depends_on: string[];
  acceptance_criteria: string[];
  review_status: "pending" | "accepted" | "changes_requested";
  review_note: string;
  artifacts: ArtifactReference[];
  retry_of: string | null;
}

export interface Approval {
  id: string;
  operation: string;
  arguments: JsonObject;
  requested_by: string;
  status: string;
  reason: string | null;
  created_at: Timestamp;
  resolved_at: Timestamp | null;
  result: Json;
}

export interface AgentRequest {
  id: string;
  agent_id: string;
  external_id: string;
  operation: string;
  arguments: JsonObject;
  message: string;
  status: string;
  result: Json;
  created_at: Timestamp;
  resolved_at: Timestamp | null;
}

export interface PlatformState {
  goals?: Goal[];
  task_links?: TaskLink[];
  agents: Agent[];
  tasks: Task[];
  approvals: Approval[];
  requests: AgentRequest[];
  config: JsonObject;
  runtime: {
    config_revision: number;
    pending_restart: string[];
    pending_restart_values?: JsonObject;
    last_config_error: string | null;
    protocol_version: string;
  };
}

export interface Operation {
  name: string;
  description: string;
  parameters: JsonObject;
  risk: string;
  mutating: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: Timestamp;
  updated_at: Timestamp;
  source: string;
  agent_id?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "agent";
  content: string;
  reasoning: string;
  created_at: Timestamp;
  run_id?: string;
  source?: string;
}

export interface Run {
  id: string;
  conversation_id: string;
  status: string;
  created_at: Timestamp;
  updated_at: Timestamp;
  error?: string;
  pending_approval_id?: string;
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[];
  runs: Run[];
  last_event_seq?: number;
  tool_calls?: ToolCall[];
  events?: ChatEvent[];
}

export interface ChatEvent {
  seq: number;
  type: string;
  run_id: string | null;
  data: JsonObject;
}

export interface ToolCall {
  id: string;
  key?: string;
  name: string;
  arguments: Json;
  result?: Json;
  run_id: string;
  approval_id?: string;
  status:
    | "running"
    | "completed"
    | "approval"
    | "cancelled"
    | "interrupted"
    | "failed";
}

export interface Session {
  authenticated: boolean;
  csrf_token?: string;
}

export interface CollaborationChannel {
  id: string;
  title: string;
  members: string[];
  created_by: string;
  goal_id: string | null;
  created_at: Timestamp;
}

export interface PeerMessage {
  id: string;
  sequence: number;
  sender_agent_id: string | null;
  sender_role: string;
  source_task_id: string | null;
  channel_id: string | null;
  recipient_agent_id: string | null;
  recipient_task_id: string | null;
  message: string;
  reply_to: string | null;
  created_at: Timestamp;
}

export interface PeerReceipt {
  agent_id: string;
  state: "queued" | "delivered" | "read" | "processed";
  wake_task_id: string | null;
  updated_at: Timestamp;
}

export interface TaskWait {
  task_id: string;
  waiting_for: string[];
  reason: string;
  updated_at: Timestamp;
}
