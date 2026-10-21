import { Link, useParams } from "react-router-dom";
import type { Round } from "../api/types";
import { Failed } from "../components/Failed";
import { Section } from "../components/Section";
import { useFeedbackDetail } from "../hooks/useAdmin";
import { stamp } from "../time";

const PRE = "text-xs font-mono bg-grey-lighter rounded p-3 overflow-x-auto whitespace-pre-wrap";

function Json({ value }: { value: unknown }) {
  return <pre className={PRE}>{JSON.stringify(value, null, 2)}</pre>;
}

function Rounds({ rounds }: { rounds: Round[] }) {
  return (
    <ol className="space-y-4">
      {rounds.map((r, i) => (
        <li key={i} className="border border-grey-light rounded-md p-3 space-y-2">
          <p className="font-medium">
            Round {r.round}: {r.action}
          </p>
          <p className="text-grey">
            {r.model || "no model"}, served as {r.served_model || "nothing"}, temperature{" "}
            {r.temperature ?? "none"}, bundle <span className="font-mono">{r.bundle_hash}</span>
          </p>
          {(r.sent ?? []).map((m, j) => (
            <div key={j}>
              <p className="text-grey">Sent as {m.role}</p>
              <pre className={PRE}>{m.content}</pre>
            </div>
          ))}
          <p className="text-grey">Reply</p>
          <pre className={PRE}>{r.text}</pre>
          {r.reasons.map((reason) => (
            <p key={reason}>Rejected: {reason}</p>
          ))}
        </li>
      ))}
    </ol>
  );
}

export function AdminFeedbackPage() {
  const feedbackId = useParams().feedbackId ?? "";
  const detail = useFeedbackDetail(feedbackId);

  if (!detail.data) return <Failed error={detail.error} />;
  const f = detail.data;

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-5xl mx-auto p-6">
        <h1 className="text-xl font-bold text-cc-navy">Feedback from {f.username}</h1>

        <Section title="Feedback">
          <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
            <dt className="text-grey">When</dt>
            <dd>{stamp(f.updated_at)}</dd>
            <dt className="text-grey">User</dt>
            <dd>
              <Link to={`/admin/users/${encodeURIComponent(f.owner_id)}`} className="text-link">
                {f.username}
              </Link>
              {f.email ? ` (${f.email})` : ""}
            </dd>
            <dt className="text-grey">Rating</dt>
            <dd>{f.rating === "up" ? "👍" : f.rating === "down" ? "👎" : "none"}</dd>
            <dt className="text-grey">Comment</dt>
            <dd className="whitespace-pre-wrap">{f.comment ?? "none"}</dd>
            <dt className="text-grey">Chat</dt>
            <dd>{f.thread_title}</dd>
            <dt className="text-grey">Project</dt>
            <dd>{f.project_id ?? "none"}</dd>
            <dt className="text-grey">ID</dt>
            <dd className="font-mono">{f.feedback_id}</dd>
          </dl>
        </Section>

        {f.message_kind && (
          <Section title={`The ${f.message_kind} rated`}>
            <pre className={PRE}>{f.message_text}</pre>
            {f.message_payload && Object.keys(f.message_payload).length > 0 && (
              <Json value={f.message_payload} />
            )}
          </Section>
        )}
        {f.run_state && (
          <Section title="The run rated">
            <p>{f.run_state}</p>
            {f.run_failure && <Json value={f.run_failure} />}
          </Section>
        )}
        {f.job_error && (
          <Section title="Job error">
            <pre className={PRE}>{f.job_error}</pre>
          </Section>
        )}
        {f.requirements && (
          <Section title={`Description, version ${f.description_version}`}>
            <Json value={{ requirements: f.requirements, rubric: f.rubric }} />
          </Section>
        )}
        {f.transcript && (
          <Section title="Conversation">
            <p className="mb-2">Asked: {f.prompt}</p>
            <Rounds rounds={f.transcript} />
          </Section>
        )}
        {f.routing_transcript && (
          <Section title="Routing">
            <Rounds rounds={f.routing_transcript} />
          </Section>
        )}
        {f.build_turns.length > 0 && (
          <Section title="Build-agent turns">
            <Rounds rounds={f.build_turns} />
          </Section>
        )}
      </div>
    </div>
  );
}
