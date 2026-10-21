import type {
  AdminSettings,
  Announcement,
  Event,
  Feedback,
  FeedbackDetail,
  FeedbackPage,
  ImportedKey,
  Job,
  KeyDeleted,
  Me,
  Posted,
  Profile,
  Rating,
  Resource,
  Run,
  SystemKey,
  Thread,
  ThreadDetail,
  TrashedThread,
  AuthMethods,
  UserDetail,
  UsersPage,
} from "./types";

const BASE = "/api";

export const LOGOUT_URL = `${BASE}/auth/logout`;

/** Log in at testbed, then return to next. */
export function loginUrl(testbed: string, next: string): string {
  return `${BASE}/auth/login?testbed=${encodeURIComponent(testbed)}&next=${encodeURIComponent(next)}`;
}

/** The login page, set to return to the current page. */
function loginPageUrl(): string {
  const here = window.location.pathname + window.location.search;
  return `/login?next=${encodeURIComponent(here)}`;
}

export class ApiError extends Error {
  constructor(readonly status: number, readonly detail: string) {
    super(detail || `API ${status}`);
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
    },
    ...init,
  });
  if (!res.ok) {
    throw await refusal(res);
  }
  return (res.status === 204 ? undefined : res.json()) as Promise<T>;
}

// Whether a 401 has already sent the browser to log in.
let leaving = false;

/** Sends the browser to log in on the first 401. */
async function refusal(res: Response): Promise<ApiError> {
  if (res.status === 401 && !leaving) {
    leaving = true;
    window.location.assign(loginPageUrl());
  }
  return new ApiError(res.status, await detailOf(res));
}

async function detailOf(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    return "";
  }
}

export function fetchMe(): Promise<Me> {
  return apiFetch<Me>("/me");
}

export function fetchAuthMethods(): Promise<AuthMethods> {
  return apiFetch<AuthMethods>("/auth/methods");
}

/** Raises ApiError on a wrong password. */
export async function logInLocally(name: string, password: string): Promise<void> {
  const res = await fetch(`${BASE}/auth/local`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ name, password }),
  });
  if (!res.ok) {
    throw new ApiError(res.status, await detailOf(res));
  }
}

export function fetchProfile(): Promise<Profile> {
  return apiFetch<Profile>("/profile");
}

export function setDefaults(changes: {
  default_project_id?: string;
  default_ssh_key_ref?: string | null;
  default_site_keypairs?: Record<string, string> | null;
  default_vm_site?: string | null;
  default_baremetal_site?: string | null;
}): Promise<Profile> {
  return apiFetch<Profile>("/profile", { method: "PATCH", body: JSON.stringify(changes) });
}

export function fetchSiteKeypairs(site: string): Promise<{ name: string }[]> {
  return apiFetch<{ name: string }[]>(`/profile/keypairs?site=${encodeURIComponent(site)}`);
}

export function deleteKey(name: string, atSites: boolean): Promise<KeyDeleted> {
  return apiFetch<KeyDeleted>(`/keys/${encodeURIComponent(name)}?at_sites=${atSites}`, {
    method: "DELETE",
  });
}

export function fetchUsers(page: number, size?: number): Promise<UsersPage> {
  return apiFetch<UsersPage>(`/admin/users?page=${page}${size ? `&size=${size}` : ""}`);
}

export function fetchAdminSettings(): Promise<AdminSettings> {
  return apiFetch<AdminSettings>("/admin/settings");
}

export function patchAdminSettings(changes: object): Promise<AdminSettings> {
  return apiFetch<AdminSettings>("/admin/settings", {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
}

function userPath(userId: string): string {
  return `/admin/users/${encodeURIComponent(userId)}`;
}

export function fetchUser(userId: string): Promise<UserDetail> {
  return apiFetch<UserDetail>(userPath(userId));
}

export function patchUser(userId: string, changes: object): Promise<UserDetail> {
  return apiFetch<UserDetail>(userPath(userId), {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
}

export function endSessions(userId: string): Promise<void> {
  return apiFetch<void>(`${userPath(userId)}/sessions`, { method: "DELETE" });
}

export function fetchAnnouncement(): Promise<Announcement | null> {
  return apiFetch<Announcement | null>("/announcement");
}

export function postAnnouncement(text: string): Promise<Announcement> {
  return apiFetch<Announcement>("/admin/announcement", {
    method: "PUT",
    body: JSON.stringify({ text }),
  });
}

export function clearAnnouncement(): Promise<void> {
  return apiFetch<void>("/admin/announcement", { method: "DELETE" });
}

export function fetchFeedback(page: number, size?: number): Promise<FeedbackPage> {
  return apiFetch<FeedbackPage>(`/admin/feedback?page=${page}${size ? `&size=${size}` : ""}`);
}

export function fetchFeedbackDetail(feedbackId: string): Promise<FeedbackDetail> {
  return apiFetch<FeedbackDetail>(`/admin/feedback/${encodeURIComponent(feedbackId)}`);
}

export function fetchSystemKeys(): Promise<SystemKey[]> {
  return apiFetch<SystemKey[]>("/admin/system-keys");
}

export function rotateSystemKey(): Promise<SystemKey> {
  return apiFetch<SystemKey>("/admin/system-keys/rotate", { method: "POST", body: "{}" });
}

export function fetchThreads(): Promise<Thread[]> {
  return apiFetch<Thread[]>("/threads");
}

export function createThread(): Promise<Thread> {
  return apiFetch<Thread>("/threads", { method: "POST", body: "{}" });
}

export function deleteThread(threadId: string): Promise<void> {
  return apiFetch<void>(`/threads/${encodeURIComponent(threadId)}`, { method: "DELETE" });
}

export function fetchTrash(): Promise<TrashedThread[]> {
  return apiFetch<TrashedThread[]>("/trash");
}

export function restoreThread(threadId: string): Promise<Thread> {
  return apiFetch<Thread>(`/trash/${encodeURIComponent(threadId)}/restore`, {
    method: "POST",
    body: "{}",
  });
}

export function fetchThread(threadId: string): Promise<ThreadDetail> {
  return apiFetch<ThreadDetail>(`/threads/${encodeURIComponent(threadId)}`);
}

export function postMessage(threadId: string, text: string): Promise<Posted> {
  return apiFetch<Posted>(`/threads/${encodeURIComponent(threadId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

export function sendFeedback(
  threadId: string,
  feedback: { message_id?: string; run_id?: string; rating?: Rating; comment?: string | null },
): Promise<Feedback> {
  return apiFetch<Feedback>(`/threads/${encodeURIComponent(threadId)}/feedback`, {
    method: "POST",
    body: JSON.stringify(feedback),
  });
}

export function patchThread(
  threadId: string,
  changes: { ssh_key_ref?: string; site_keypairs?: Record<string, string>; project_id?: string },
): Promise<Thread> {
  return apiFetch<Thread>(`/threads/${encodeURIComponent(threadId)}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
}

export function importKey(name: string, publicKey: string): Promise<ImportedKey> {
  return apiFetch<ImportedKey>("/keys", {
    method: "POST",
    body: JSON.stringify({ name, public_key: publicKey }),
  });
}

export function approve(
  threadId: string,
  descriptionId: string,
  version: number,
): Promise<Job> {
  return apiFetch<Job>(`/threads/${encodeURIComponent(threadId)}/builds`, {
    method: "POST",
    body: JSON.stringify({ description_id: descriptionId, version }),
  });
}

export function fetchRun(runId: string): Promise<Run> {
  return apiFetch<Run>(`/runs/${encodeURIComponent(runId)}`);
}

export function fetchRunEvents(runId: string): Promise<Event[]> {
  return apiFetch<Event[]>(`/runs/${encodeURIComponent(runId)}/events`);
}

/** Release the resources the user confirmed. */
export function teardown(runId: string, resources: Resource[]): Promise<Job> {
  return apiFetch<Job>(`/runs/${encodeURIComponent(runId)}/teardown`, {
    method: "POST",
    body: JSON.stringify({ resources }),
  });
}

export function confirmRelease(requestId: string): Promise<Job> {
  return apiFetch<Job>(`/releases/${encodeURIComponent(requestId)}/confirm`, {
    method: "POST",
    body: "{}",
  });
}

export function declineRelease(requestId: string): Promise<void> {
  return apiFetch<void>(`/releases/${encodeURIComponent(requestId)}/decline`, {
    method: "POST",
    body: "{}",
  });
}
