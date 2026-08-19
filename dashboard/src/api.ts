// Thin client over the fm daemon API — the dashboard holds no state of
// its own (PRD §6.8); everything shown comes from these calls plus the
// /ws event stream.

export interface ContextInfo {
  step_id: string | null;
  session_id: string;
  tokens: number;
  limit: number;
  wall_tokens: number;
  percent: number;
  band: "neutral" | "elevated" | "warning";
}

export interface TaskRow {
  id: string;
  status: string;
  repo: string;
  branch: string;
  goal: string;
  current_step: string | null;
  created_at: string;
  updated_at: string;
  generation?: number | null;
  running?: boolean;
  context?: ContextInfo | null;
}

export interface Question {
  id: string;
  task_id: string;
  step_id: string | null;
  type: "clarification" | "scope_change" | "decision" | "approval" | "fyi";
  question: string;
  urgency: "blocking" | "normal";
  options: string[];
  default: string | null;
  evidence: Record<string, unknown>;
  status: "open" | "answered" | "noted";
  answer: string | null;
  answered_by: string | null;
  asked_at: string;
  answered_at: string | null;
}

export interface SessionRecord {
  session_id: string;
  generation: number;
  attempt: number;
  window_id: string | null;
  started_at: string;
  ended_at: string | null;
  outcome: string | null;
  peak_tokens: number;
}

export interface StepState {
  id: string;
  status: string;
  attempt: number;
  generation: number;
  last_failure: string | null;
  sessions: SessionRecord[];
}

export interface Task {
  id: string;
  repo: string;
  branch: string;
  status: string;
  worktree: string;
  goal: string;
  current_step: string | null;
  created_at: string;
  updated_at: string;
  steps: StepState[];
}

export interface Criterion {
  id: string;
  command: string;
  kind: string;
  cwd: string;
  timeout: number;
}

export interface StepSpec {
  id: string;
  prompt: string;
  title: string;
  skill: string | null;
  model: string | null;
  allowed_tools: string[];
  criteria: string[];
}

export interface Amendment {
  at: string;
  question_id: string;
  question: string;
  answer: string;
  by: string;
}

export interface Contract {
  goal: string;
  repo: string;
  steps: StepSpec[];
  criteria: Criterion[];
  scope_in: string[];
  scope_out: string[];
  tripwires: Record<string, boolean | number>;
  tripwire_allow: string[];
  context: string;
  amendments: Amendment[];
}

export interface CriterionResult {
  id: string;
  passed: boolean;
  command: string;
  exit_status: number | null;
  duration_s: number;
  stdout: string;
  stderr: string;
  error: string | null;
}

export interface ValidationRecord {
  at: string;
  attempt: number;
  results: CriterionResult[];
}

export interface FmEvent {
  ts: string;
  event: string;
  task_id: string;
  step_id?: string | null;
  data?: Record<string, unknown>;
}

export interface TaskDetail {
  task: Task;
  contract: Contract | null;
  contract_md: string | null;
  questions: Question[];
  events: FmEvent[];
  attach: string | null;
  running: boolean;
  context: ContextInfo | null;
  handoffs: Record<string, { generation: number; text: string }>;
  validations: Record<string, ValidationRecord>;
}

export interface DiffFile {
  path: string;
  added: number | null;
  deleted: number | null;
  untracked: boolean;
}

export interface DiffInfo {
  files: DiffFile[];
  added: number;
  deleted: number;
  worktree?: string;
  branch?: string;
}

export interface StatusInfo {
  tasks: TaskRow[];
  questions: Question[];
  config: { max_workers: number; wall_tokens: number };
}

export interface MemoryProject {
  project: string;
  bytes: number;
  entries: number;
  updated_at: string;
}

export interface LivePayload {
  kind: "live";
  task_id: string;
  step_id: string | null;
  session_id: string;
  generation: number;
  output: string;
  context: ContextInfo | null;
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const resp = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try {
      const j = await resp.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* keep status code */
    }
    throw new Error(detail);
  }
  return (await resp.json()) as T;
}

export const api = {
  status: () => req<StatusInfo>("GET", "/status"),
  task: (id: string) => req<TaskDetail>("GET", `/tasks/${id}`),
  diff: (id: string) => req<DiffInfo>("GET", `/tasks/${id}/diff`),
  diffFile: (id: string, path: string) =>
    req<{ path: string; diff: string }>(
      "GET",
      `/tasks/${id}/diff/file?path=${encodeURIComponent(path)}`
    ),
  output: (id: string) =>
    req<{ live: boolean; output: string | null; context: ContextInfo | null }>(
      "GET",
      `/tasks/${id}/output`
    ),
  run: (id: string) => req("POST", `/tasks/${id}/run`),
  pause: (id: string) => req("POST", `/tasks/${id}/pause`),
  abandon: (id: string) => req("POST", `/tasks/${id}/abandon`),
  answer: (qid: string, answer: string) =>
    req<{ question: Question; resumed: boolean }>("POST", `/questions/${qid}/answer`, {
      answer,
      by: "dashboard",
    }),
  createTask: (contract: unknown, run: boolean) =>
    req<{ task: Task; started: boolean }>("POST", "/tasks", { contract, run }),
  editContract: (id: string, contract: unknown) =>
    req<{ contract: Contract }>("PUT", `/tasks/${id}/contract`, { contract }),
  memory: () => req<{ projects: MemoryProject[] }>("GET", "/memory"),
  memoryFile: (project: string) =>
    req<{ project: string; text: string }>("GET", `/memory/${project}`),
  remember: (project: string, fact: string) =>
    req<{ project: string; text: string }>("POST", `/memory/${project}`, { fact }),
  saveMemory: (project: string, text: string) =>
    req<{ project: string; text: string }>("PUT", `/memory/${project}`, { text }),
};

// ------------------------------------------------------------- websocket

export type WsMessage =
  | { kind: "snapshot"; tasks: TaskRow[]; questions: Question[] }
  | LivePayload
  | FmEvent;

type Listener = (msg: WsMessage) => void;

/** One auto-reconnecting socket for the whole app; components subscribe. */
class Socket {
  private listeners = new Set<Listener>();
  private ws: WebSocket | null = null;
  private timer: number | null = null;
  connected = false;

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    this.ensure();
    return () => {
      this.listeners.delete(fn);
    };
  }

  private ensure() {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => {
      this.connected = true;
      this.emit({ ts: "", event: "_ws_open", task_id: "" });
    };
    ws.onmessage = (e) => {
      try {
        this.emit(JSON.parse(e.data) as WsMessage);
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      this.connected = false;
      this.emit({ ts: "", event: "_ws_close", task_id: "" });
      if (this.timer == null) {
        this.timer = window.setTimeout(() => {
          this.timer = null;
          if (this.listeners.size) this.ensure();
        }, 2000);
      }
    };
    ws.onerror = () => ws.close();
  }

  private emit(msg: WsMessage) {
    this.listeners.forEach((fn) => fn(msg));
  }
}

export const socket = new Socket();
