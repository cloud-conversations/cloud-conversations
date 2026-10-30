type Route = "describe" | "revise" | "build" | "inventory" | "ask";
type MessageKind =
  | "user"
  | "answer"
  | "description"
  | "run"
  | "notice"
  | "holdings"
  | "release";

export interface Thread {
  thread_id: string;
  title: string;
  ssh_key_ref: string | null;
  site_keypairs: Record<string, string> | null;
  project_id: string | null;
  activity?: "running" | "reserved" | null;
}

/** An imported key, or a keypair name at each site. */
export type KeyChoice = { ssh_key_ref: string } | { site_keypairs: Record<string, string> };

export interface TrashedThread {
  thread_id: string;
  title: string;
  deleted_at: string;
  restorable_until: string;
}

export interface Me {
  testbed: string;
  username: string;
  projects: { id: string; nickname?: string }[];
  admin: boolean;
  /** Accounts waiting for approval. 0 unless the caller is an admin. */
  waiting_accounts: number;
}

export interface SystemKey {
  fingerprint: string;
  created_at: string;
  retired_at: string | null;
  authorized_key: string;
}

export interface Quota {
  name: string;
  used: number;
  limit: number;
  default: number;
  override: number | null;
}

interface ActiveExperiment {
  run_id: string;
  state: string;
  site: string;
  ends_at: string | null;
  thread_id: string | null;
  title: string | null;
  resources: Resource[];
}

export interface Profile {
  testbed: string;
  username: string;
  projects: { id: string; nickname?: string }[];
  default_project_id: string | null;
  default_ssh_key_ref: string | null;
  default_site_keypairs: Record<string, string> | null;
  default_vm_site: string | null;
  default_baremetal_site: string | null;
  keys: ImportedKey[];
  sites: string[];
  vm_sites: string[];
  baremetal_sites: string[];
  quotas: Quota[];
  active_experiments: ActiveExperiment[];
}

interface SiteOutcome {
  site: string;
  outcome: "deleted" | "absent" | "failed";
  error: string | null;
}

export interface KeyDeleted {
  name: string;
  keypair_name: string;
  sites: SiteOutcome[];
}

export interface UserSummary {
  user_id: string;
  testbed: string;
  username: string;
  email: string | null;
  projects: { id: string; nickname?: string }[];
  local: boolean;
  first_login_at: string | null;
  last_login_at: string | null;
  checked_at: string | null;
  disabled_at: string | null;
  disabled_reason: string | null;
  open_sessions: number;
  chats: number;
  latest_chat_at: string | null;
  overridden: boolean;
  admin: boolean;
  approval: "approved" | "waiting" | "denied";
  approval_requested_at: string | null;
  approval_decided_at: string | null;
}

export interface UserDetail extends UserSummary {
  quotas: Quota[];
}

export interface UsersPage {
  users: UserSummary[];
  /** Whether there is a next page. */
  more: boolean;
  size: number;
  sizes: number[];
}

export interface EveryoneLimit {
  name: string;
  /** The value in the config file. */
  default: number;
  override: number | null;
}

export interface AdminSettings {
  require_approval: boolean;
  limits: EveryoneLimit[];
}

export interface Announcement {
  text: string;
  posted_at: string;
}

export interface ImportedKey {
  name: string;
  keypair_name: string;
  fingerprint: string;
}

export interface Message {
  message_id: string;
  role: "user" | "assistant";
  kind: MessageKind;
  text: string;
  payload: {
    sources?: string[];
    suggestions?: string[];
    needs?: string;
    tables?: string[];
    again?: boolean;
    request_id?: string;
    /** The version a proposal replaces the run with. */
    version?: number | null;
  };
  description_id: string | null;
  description_version: number | null;
  created_at: string;
}

export interface Job {
  job_id: string;
  kind: "exchange" | "build" | "teardown";
  route: Route | null;
  run_id: string | null;
  /** The job is queued behind another build. */
  waiting: boolean;
}

export type Source = "stated" | "inferred" | "default" | "asked";

export interface Requirement {
  key: string;
  value: unknown;
  source: Source;
}

interface Requirements {
  site: string | null;
  start_at: string | null;
  start_source: Source | null;
  duration_hours: number;
  duration_source: Source | null;
  items: Requirement[];
  image?: ImageChoice | null;
}

/** The image a description resolved to, and whether its testbed supports it. */
interface ImageChoice {
  image_id: string;
  name: string;
  supported: boolean;
  testbed: string;
}

export interface Check {
  id: string;
  kind: string;
  target?: string | null;
  expected: unknown;
  comparator: string;
  from_requirement?: string | null;
}

export interface Version {
  description_id: string;
  version: number;
  requirements: Requirements;
  rubric: Check[];
  unsupported: string[];
  open_questions: string[];
}

export interface RunSummary {
  run_id: string;
  created_at: string;
}

export interface ThreadDetail {
  thread: Thread;
  messages: Message[];
  versions: Version[];
  jobs: Job[];
  runs: RunSummary[];
  /** The user's answer to each agent proposal, by request ID. */
  decisions: Record<string, "confirmed" | "declined">;
  feedback: Feedback[];
}

export type Rating = "up" | "down";

export interface Feedback {
  feedback_id: string;
  message_id: string | null;
  run_id: string | null;
  rating: Rating | null;
  comment: string | null;
  updated_at: string;
}

/** One round with the model: what it was sent, and its reply. */
export interface Round {
  round: number;
  action: string;
  text: string;
  reasons: string[];
  sent: { role: string; content: string }[] | null;
  model: string;
  served_model: string;
  temperature: number | null;
  bundle_hash: string;
}

export interface FeedbackDetail extends ListedFeedback {
  created_at: string;
  project_id: string | null;
  message_id: string | null;
  message_payload: Record<string, unknown> | null;
  conversation_id: string | null;
  prompt: string | null;
  transcript: Round[] | null;
  routing_conversation_id: string | null;
  routing_transcript: Round[] | null;
  job_id: string | null;
  job_error: string | null;
  description_id: string | null;
  description_version: number | null;
  requirements: Record<string, unknown> | null;
  rubric: Record<string, unknown>[] | null;
  build_turns: Round[];
}

export interface FeedbackPage {
  feedback: ListedFeedback[];
  /** Whether there is an older page. */
  more: boolean;
  size: number;
  sizes: number[];
}

export interface ListedFeedback {
  feedback_id: string;
  updated_at: string;
  owner_id: string;
  username: string;
  email: string | null;
  thread_id: string;
  thread_title: string;
  rating: Rating | null;
  comment: string | null;
  message_kind: string | null;
  message_text: string | null;
  run_id: string | null;
  run_state: string | null;
  run_failure: Failure | null;
}

export interface CheckResult {
  check_id: string;
  kind: string;
  target?: string | null;
  expected: unknown;
  actual: unknown;
  passed: boolean;
}

interface Connection {
  host: string;
  user: string;
  port: number;
  expires_at: string | null;
}

interface Failure {
  kind: string;
  message: string;
  step_id: string | null;
}

export interface Resource {
  kind: string;
  provider_id: string;
  site: string;
}

export interface Run {
  description_version: number;
  state: string;
  resume_count: number;
  results: CheckResult[] | null;
  connection: Connection | null;
  failure: Failure | null;
  live_resources: Resource[];
}

export interface Event {
  seq: number;
  at: string;
  kind: string;
  payload: Record<string, unknown>;
}

export interface Posted {
  message: Message;
  job: Job;
}

const TERMINAL_RUN_STATES = ["passed", "failed", "abandoned"];

export function isTerminal(state: string): boolean {
  return TERMINAL_RUN_STATES.includes(state);
}

export interface AuthMethods {
  local: boolean;
  logins: { testbed: string; label: string }[];
  /** Why the last login was refused. */
  refused: string | null;
}
