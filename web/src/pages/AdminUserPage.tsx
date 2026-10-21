import { useState } from "react";
import { useParams } from "react-router-dom";
import type { Quota } from "../api/types";
import { Failed } from "../components/Failed";
import { Section } from "../components/Section";
import { useEndSessions, usePatchUser, useUser } from "../hooks/useAdmin";
import { QUOTA_LABELS } from "../quotas";
import { stamp } from "../time";

function QuotaRow({ quota, userId }: { quota: Quota; userId: string }) {
  const changed = usePatchUser(userId);
  const [value, setValue] = useState(quota.override?.toString() ?? "");
  const label = QUOTA_LABELS[quota.name];

  return (
    <tr className="border-t border-grey-light">
      <td className="py-2 pr-4">{label}</td>
      <td className="py-2 pr-4">{quota.used}</td>
      <td className="py-2 pr-4">{quota.default}</td>
      <td className="py-2 pr-4">
        <input
          type="number"
          min={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="default"
          aria-label={`${label} override`}
          className="w-24 border border-grey-light rounded-md px-2 py-1"
        />
      </td>
      <td className="py-2 whitespace-nowrap">
        <button
          onClick={() => changed.mutate({ [quota.name]: value ? Number(value) : null })}
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

export function AdminUserPage() {
  const userId = useParams().userId ?? "";
  const user = useUser(userId);
  const changed = usePatchUser(userId);
  const decided = usePatchUser(userId);
  const ended = useEndSessions(userId);
  const [reason, setReason] = useState("");

  if (!user.data) return <Failed error={user.error} />;
  const u = user.data;
  const when = u.approval === "waiting" ? u.approval_requested_at : u.approval_decided_at;

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-4xl mx-auto p-6">
        <h1 className="text-xl font-bold text-cc-navy">{u.username}</h1>

        <Section title="Account">
          <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
            <dt className="text-grey">Testbed</dt>
            <dd>{u.testbed}</dd>
            <dt className="text-grey">User ID</dt>
            <dd className="font-mono">{u.user_id}</dd>
            <dt className="text-grey">Email</dt>
            <dd>{u.email ?? "none"}</dd>
            <dt className="text-grey">Admin</dt>
            <dd>{u.admin ? "yes" : "no"}</dd>
            <dt className="text-grey">Logs in with</dt>
            <dd>{u.local ? "a local password" : u.testbed}</dd>
            <dt className="text-grey">First login</dt>
            <dd>{u.first_login_at ? stamp(u.first_login_at) : "never"}</dd>
            <dt className="text-grey">Last login</dt>
            <dd>{u.last_login_at ? stamp(u.last_login_at) : "never"}</dd>
            <dt className="text-grey">Last checked with {u.testbed}</dt>
            <dd>{u.checked_at ? stamp(u.checked_at) : "never"}</dd>
            <dt className="text-grey">Projects</dt>
            <dd>{u.projects.map((p) => p.id).join(", ") || "none"}</dd>
          </dl>
        </Section>

        {u.approval !== "approved" && (
          <Section title="Approval">
            <div className="flex items-center gap-3">
              <p>
                {u.approval === "waiting" ? "Waiting since" : "Denied"}
                {when ? ` ${stamp(when)}` : ""}.
              </p>
              <button
                onClick={() => decided.mutate({ approval: "approved" })}
                disabled={decided.isPending}
                className="px-3 py-1 rounded-md bg-brand-info text-white disabled:opacity-40"
              >
                Approve
              </button>
              {u.approval === "waiting" && (
                <button
                  onClick={() => decided.mutate({ approval: "denied" })}
                  disabled={decided.isPending}
                  className="px-3 py-1 rounded-md bg-brand-danger text-white disabled:opacity-40"
                >
                  Deny
                </button>
              )}
            </div>
            <Failed error={decided.error} />
          </Section>
        )}

        <Section title="Status">
          {u.disabled_at ? (
            <div className="flex items-center gap-3">
              <p>
                Disabled {stamp(u.disabled_at)}: {u.disabled_reason}
              </p>
              <button
                onClick={() => changed.mutate({ disabled: false })}
                disabled={changed.isPending}
                className="px-3 py-1 rounded-md bg-brand-info text-white disabled:opacity-40"
              >
                Enable
              </button>
            </div>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                changed.mutate({ disabled: true, reason }, { onSuccess: () => setReason("") });
              }}
              className="flex items-center gap-3"
            >
              <p>Enabled.</p>
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                maxLength={500}
                placeholder="Why disable this account?"
                aria-label="Reason"
                className="flex-1 border border-grey-light rounded-md px-2 py-1"
              />
              <button
                type="submit"
                disabled={!reason.trim() || changed.isPending}
                className="px-3 py-1 rounded-md bg-brand-danger text-white disabled:opacity-40"
              >
                Disable
              </button>
            </form>
          )}
          <Failed error={changed.error} />
        </Section>

        <Section title="Sessions">
          <div className="flex items-center gap-3">
            <p>{u.open_sessions} open</p>
            <button
              onClick={() => ended.mutate(undefined)}
              disabled={u.open_sessions === 0 || ended.isPending}
              className="text-link disabled:opacity-40"
            >
              End all
            </button>
          </div>
          <Failed error={ended.error} />
        </Section>

        <Section title="Limits">
          <table className="w-full">
            <thead className="text-left text-grey">
              <tr>
                <th className="pb-1 pr-4 font-medium">Limit</th>
                <th className="pb-1 pr-4 font-medium">In use</th>
                <th className="pb-1 pr-4 font-medium">Default</th>
                <th className="pb-1 pr-4 font-medium">Override</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {u.quotas.map((q) => (
                <QuotaRow key={q.name} quota={q} userId={u.user_id} />
              ))}
            </tbody>
          </table>
        </Section>
      </div>
    </div>
  );
}
