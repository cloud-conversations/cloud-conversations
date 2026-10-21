import { useState } from "react";
import { Link } from "react-router-dom";
import { Failed } from "../components/Failed";
import type { EveryoneLimit, ListedFeedback, UserSummary } from "../api/types";
import {
  useAdminSettings,
  useAnnouncement,
  useClearAnnouncement,
  useFeedback,
  usePatchUser,
  usePostAnnouncement,
  useSetAdminSettings,
} from "../hooks/useAdmin";
import { useUsers } from "../hooks/useProfile";
import { useRotateSystemKey, useSystemKeys } from "../hooks/useSystemKeys";
import { QUOTA_LABELS } from "../quotas";
import { stamp } from "../time";

function AnnouncementSection() {
  const current = useAnnouncement();
  const posted = usePostAnnouncement();
  const cleared = useClearAnnouncement();
  const [text, setText] = useState("");

  return (
    <section>
      <h1 className="text-xl font-bold text-cc-navy">Announcement</h1>
      <p className="mt-1 text-sm text-grey">
        {current.data?.text
          ? `Every page shows: ${current.data.text}`
          : "No announcement is showing."}
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          posted.mutate(text, { onSuccess: () => setText("") });
        }}
        className="mt-3 flex items-center gap-3"
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={500}
          aria-label="Announcement"
          className="flex-1 text-sm border border-grey-light rounded-md px-2 py-1"
        />
        <button
          type="submit"
          disabled={!text.trim() || posted.isPending}
          className="text-sm px-3 py-1 rounded-md bg-brand-info text-white disabled:opacity-40"
        >
          Post
        </button>
        <button
          type="button"
          onClick={() => cleared.mutate(undefined)}
          disabled={!current.data || cleared.isPending}
          className="text-sm text-link disabled:opacity-40"
        >
          Clear
        </button>
      </form>
      <Failed error={posted.error ?? cleared.error} />
    </section>
  );
}

function LimitRow({ limit }: { limit: EveryoneLimit }) {
  const changed = useSetAdminSettings();
  const [value, setValue] = useState(limit.override?.toString() ?? "");
  const label = QUOTA_LABELS[limit.name];

  return (
    <tr className="border-t border-grey-light">
      <td className="py-2 pr-4">{label}</td>
      <td className="py-2 pr-4">{limit.default}</td>
      <td className="py-2 pr-4">
        <input
          type="number"
          min={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="default"
          aria-label={`${label} for everyone`}
          className="w-24 border border-grey-light rounded-md px-2 py-1"
        />
      </td>
      <td className="py-2 whitespace-nowrap">
        <button
          onClick={() => changed.mutate({ [limit.name]: value ? Number(value) : null })}
          disabled={changed.isPending}
          className="text-link disabled:opacity-40"
        >
          Save
        </button>
        <Failed error={changed.error} />
      </td>
    </tr>
  );
}

function LimitsSection() {
  const settings = useAdminSettings();

  return (
    <section className="mt-10">
      <h1 className="text-xl font-bold text-cc-navy">Limits</h1>
      <p className="mt-1 text-sm text-grey">
        Leave a box empty to use the default.
      </p>
      <Failed error={settings.error} />
      <table className="mt-4 w-full text-sm">
        <thead className="text-left text-grey">
          <tr>
            <th className="py-1 pr-4 font-medium">Limit</th>
            <th className="py-1 pr-4 font-medium">Default</th>
            <th className="py-1 pr-4 font-medium">For everyone</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(settings.data?.limits ?? []).map((l) => (
            <LimitRow key={l.name} limit={l} />
          ))}
        </tbody>
      </table>
    </section>
  );
}

function about(f: ListedFeedback): string {
  if (f.message_kind) return `${f.message_kind}: ${f.message_text}`;
  if (f.run_state) return `run ${f.run_state}${f.run_failure ? `: ${f.run_failure.message}` : ""}`;
  return "the chat";
}

function FeedbackSection() {
  const [page, setPage] = useState(1);
  const [size, setSize] = useState<number>();
  const feedback = useFeedback(page, size);

  return (
    <section className="mt-10">
      <h1 className="text-xl font-bold text-cc-navy">Feedback</h1>
      <p className="mt-1 text-sm text-grey">
        Open an ID to see what the model was sent.
      </p>
      <Failed error={feedback.error} />
      <table className="mt-4 w-full text-sm">
        <thead className="text-left text-grey">
          <tr>
            <th className="py-1 pr-4 font-medium">When</th>
            <th className="py-1 pr-4 font-medium">User</th>
            <th className="py-1 pr-4 font-medium">Rating</th>
            <th className="py-1 pr-4 font-medium">Comment</th>
            <th className="py-1 pr-4 font-medium">About</th>
            <th className="py-1 font-medium">ID</th>
          </tr>
        </thead>
        <tbody>
          {(feedback.data?.feedback ?? []).map((f) => (
            <tr key={f.feedback_id} className="border-t border-grey-light align-top">
              <td className="py-2 pr-4 whitespace-nowrap">{stamp(f.updated_at)}</td>
              <td className="py-2 pr-4">
                {f.username}
                {f.email ? ` (${f.email})` : ""}
              </td>
              <td className="py-2 pr-4">{f.rating === "up" ? "👍" : f.rating === "down" ? "👎" : ""}</td>
              <td className="py-2 pr-4 whitespace-pre-wrap">{f.comment}</td>
              <td className="py-2 pr-4 line-clamp-3">
                {f.thread_title} — {about(f)}
              </td>
              <td className="py-2 font-mono break-all">
                <Link to={`/admin/feedback/${f.feedback_id}`} className="text-link">
                  {f.feedback_id}
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-3 flex items-center gap-4 text-sm">
        <button
          onClick={() => setPage(page - 1)}
          disabled={page === 1}
          className="text-link disabled:opacity-40"
        >
          Newer
        </button>
        <span className="text-grey">Page {page}</span>
        <button
          onClick={() => setPage(page + 1)}
          disabled={!feedback.data?.more}
          className="text-link disabled:opacity-40"
        >
          Older
        </button>
        <label className="ml-auto flex items-center gap-2 text-grey">
          Per page
          <select
            value={feedback.data?.size}
            onChange={(e) => {
              setSize(Number(e.target.value));
              setPage(1);
            }}
            className="border border-grey-light rounded-md px-2 py-1 bg-white text-grey-dark"
          >
            {(feedback.data?.sizes ?? []).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>
    </section>
  );
}

function status(u: UserSummary): string {
  if (u.approval !== "approved") return u.approval;
  const notes = [u.disabled_at ? "disabled" : "enabled"];
  if (u.admin) notes.push("admin");
  if (u.overridden) notes.push("limits overridden");
  return notes.join(", ");
}

function UserRow({ u }: { u: UserSummary }) {
  const changed = usePatchUser(u.user_id);

  return (
    <tr className="border-t border-grey-light">
      <td className="py-2 pr-4">{u.testbed}</td>
      <td className="py-2 pr-4">
        <Link to={`/admin/users/${encodeURIComponent(u.user_id)}`} className="text-link">
          {u.username}
        </Link>
      </td>
      <td className="py-2 pr-4 whitespace-nowrap">{status(u)}</td>
      <td className="py-2 pr-4 whitespace-nowrap">
        {u.first_login_at ? stamp(u.first_login_at) : "never"}
      </td>
      <td className="py-2 pr-4 whitespace-nowrap">
        {u.last_login_at ? stamp(u.last_login_at) : "never"}
      </td>
      <td className="py-2 pr-4">{u.open_sessions}</td>
      <td className="py-2 pr-4">{u.chats}</td>
      <td className="py-2 pr-4 whitespace-nowrap">
        {u.latest_chat_at ? stamp(u.latest_chat_at) : "none"}
      </td>
      <td className="py-2 whitespace-nowrap">
        {u.approval === "waiting" && (
          <div className="flex items-center gap-3">
            <button
              onClick={() => changed.mutate({ approval: "approved" })}
              disabled={changed.isPending}
              className="text-link disabled:opacity-40"
            >
              Approve
            </button>
            <button
              onClick={() => changed.mutate({ approval: "denied" })}
              disabled={changed.isPending}
              className="text-link disabled:opacity-40"
            >
              Deny
            </button>
          </div>
        )}
        <Failed error={changed.error} />
      </td>
    </tr>
  );
}

function UsersSection() {
  const settings = useAdminSettings();
  const setting = useSetAdminSettings();
  const [page, setPage] = useState(1);
  const [size, setSize] = useState<number>();
  const users = useUsers(page, size);

  return (
    <section className="mt-10">
      <h1 className="text-xl font-bold text-cc-navy">Users</h1>
      <label className="mt-1 flex items-center gap-2 text-sm text-grey-dark">
        <input
          type="checkbox"
          checked={settings.data?.require_approval ?? false}
          disabled={!settings.data || setting.isPending}
          onChange={(e) => setting.mutate({ require_approval: e.target.checked })}
        />
        Require approval for new accounts
      </label>
      <Failed error={settings.error ?? setting.error ?? users.error} />
      <table className="mt-4 w-full text-sm">
        <thead className="text-left text-grey">
          <tr>
            <th className="py-1 pr-4 font-medium">Testbed</th>
            <th className="py-1 pr-4 font-medium">Username</th>
            <th className="py-1 pr-4 font-medium">Status</th>
            <th className="py-1 pr-4 font-medium">First login</th>
            <th className="py-1 pr-4 font-medium">Last login</th>
            <th className="py-1 pr-4 font-medium">Open sessions</th>
            <th className="py-1 pr-4 font-medium">Chats</th>
            <th className="py-1 pr-4 font-medium">Latest chat</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(users.data?.users ?? []).map((u) => (
            <UserRow key={u.user_id} u={u} />
          ))}
        </tbody>
      </table>
      <div className="mt-3 flex items-center gap-4 text-sm">
        <button
          onClick={() => setPage(page - 1)}
          disabled={page === 1}
          className="text-link disabled:opacity-40"
        >
          Previous
        </button>
        <span className="text-grey">Page {page}</span>
        <button
          onClick={() => setPage(page + 1)}
          disabled={!users.data?.more}
          className="text-link disabled:opacity-40"
        >
          Next
        </button>
        <label className="ml-auto flex items-center gap-2 text-grey">
          Users per page
          <select
            value={users.data?.size}
            onChange={(e) => {
              setSize(Number(e.target.value));
              setPage(1);
            }}
            className="border border-grey-light rounded-md px-2 py-1 bg-white text-grey-dark"
          >
            {(users.data?.sizes ?? []).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>
    </section>
  );
}

export function AdminPage() {
  const keys = useSystemKeys();
  const rotated = useRotateSystemKey();

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-5xl mx-auto p-6">
        <AnnouncementSection />

        <div className="mt-10 flex items-center gap-4">
          <h1 className="text-xl font-bold text-cc-navy">System keys</h1>
          <button
            onClick={() => rotated.mutate()}
            disabled={rotated.isPending}
            className="ml-auto text-sm px-3 py-1 rounded-md bg-brand-info text-white disabled:opacity-40"
          >
            Rotate
          </button>
        </div>
        <p className="mt-1 text-sm text-grey">
          New instances get the current key. Instances keep trusting the key they were
          booted with.
        </p>
        <Failed error={keys.error ?? rotated.error} />
        <table className="mt-4 w-full text-sm">
          <thead className="text-left text-grey">
            <tr>
              <th className="py-1 pr-4 font-medium">Fingerprint</th>
              <th className="py-1 pr-4 font-medium">Created</th>
              <th className="py-1 pr-4 font-medium">Status</th>
              <th className="py-1 font-medium">authorized_keys line</th>
            </tr>
          </thead>
          <tbody>
            {(keys.data ?? []).map((key) => (
              <tr key={key.fingerprint} className="border-t border-grey-light align-top">
                <td className="py-2 pr-4 font-mono">{key.fingerprint}</td>
                <td className="py-2 pr-4 whitespace-nowrap">{stamp(key.created_at)}</td>
                <td className="py-2 pr-4 whitespace-nowrap">
                  {key.retired_at ? `retired ${stamp(key.retired_at)}` : "current"}
                </td>
                <td className="py-2 font-mono break-all">{key.authorized_key}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <LimitsSection />

        <UsersSection />

        <FeedbackSection />
      </div>
    </div>
  );
}
