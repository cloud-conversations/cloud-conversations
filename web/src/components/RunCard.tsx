import type { Event, Run } from "../api/types";
import { isTerminal } from "../api/types";
import { useRun, useRunEvents, useTeardown } from "../hooks/useRun";
import { askToRelease, named, noun, verb } from "../resources";
import { Failed } from "./Failed";
import { WAITING } from "./PendingRow";
import { clock, stamp } from "../time";

const STATE_STYLE: Record<string, string> = {
  passed: "bg-brand-success text-white",
  failed: "bg-brand-danger text-white",
  abandoned: "bg-grey text-white",
};

/** One event, in plain words. */
function line(event: Event): string | null {
  const payload = event.payload as Record<string, string>;
  switch (event.kind) {
    case "run_started":
      return payload.resume ? "started again" : "started";
    case "step_succeeded":
      return `${payload.action} done`;
    case "step_failed":
      return `${payload.action} failed: ${payload.error}`;
    case "step_retried":
      return `retrying ${payload.action}`;
    case "resource_created":
      return `${verb(payload.kind)} ${noun(payload.kind)} ${payload.provider_id}`;
    case "resource_released":
      return `released ${noun(payload.kind)} ${payload.provider_id}`;
    case "connection_ready":
      return "the host answered SSH";
    case "validation_completed":
      return "measured against the rubric";
    case "model_turn":
      return `the experiment agent chose to ${payload.action}`;
    case "tool_called":
      return `it read ${payload.read}`;
    case "description_revision_requested":
      return `it asked for a new version: ${payload.change}`;
    case "run_terminal":
      return `run ${payload.state}`;
    default:
      return null;
  }
}

interface Props {
  runId: string;
  threadId: string;
  releasing: boolean;
  // The release is queued behind another build.
  waiting: boolean;
  // A build job on the chat is still running.
  building: boolean;
}

export function RunCard({ runId, threadId, releasing, waiting, building }: Props) {
  const released = useTeardown(runId, threadId);
  const inFlight = releasing || released.isPending;
  const { data: run, isPending } = useRun(runId, inFlight, building);
  const { data: events } = useRunEvents(runId, run?.state, building);

  if (isPending || !run) {
    return (
      <div className="bg-white border border-grey-light rounded-lg p-4 text-sm text-grey">
        Reading the run…
      </div>
    );
  }

  const holding = run.live_resources.length > 0;
  return (
    <div className="bg-white border border-grey-light rounded-lg overflow-hidden">
      <div className="px-4 py-2 bg-grey-lighter border-b border-grey-light flex items-center gap-2">
        <h3 className="text-sm font-semibold">
          Build of version {run.description_version}
        </h3>
        <span
          className={`text-xs rounded px-1.5 py-0.5 ${
            STATE_STYLE[run.state] ?? "bg-brand-info text-white"
          }`}
        >
          {run.state}
        </span>
        {run.resume_count > 0 && (
          <span className="text-xs text-grey">
            {run.resume_count} further attempt{run.resume_count > 1 ? "s" : ""}
          </span>
        )}
      </div>

      {run.connection && <Endpoint run={run} />}

      {run.failure && (
        <p className="px-4 py-3 border-b border-grey-light text-sm text-brand-danger">
          {run.failure.kind}: {run.failure.message}
          {run.failure.step_id ? ` (at ${run.failure.step_id})` : ""}
        </p>
      )}

      {run.results && (
        <table className="w-full text-sm border-b border-grey-light">
          <caption className="sr-only">Rubric results</caption>
          <tbody>
            {run.results.map((result) => (
              <tr key={result.check_id} className="border-b border-grey-lighter">
                <td className="px-4 py-1 w-16">
                  <span
                    className={result.passed ? "text-brand-success" : "text-brand-danger"}
                  >
                    {result.passed ? "PASS" : "FAIL"}
                  </span>
                </td>
                <th scope="row" className="text-left font-normal text-grey py-1">
                  {result.kind}
                </th>
                <td className="px-4 py-1 font-mono text-xs">
                  wanted {String(result.expected)}, got {String(result.actual)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <ol className="px-4 py-3 space-y-0.5 text-sm">
        {(events ?? []).map((event) => {
          const text = line(event);
          return text ? (
            <li key={event.seq} className="text-grey">
              <span className="text-grey-med font-mono text-xs mr-2">
                {clock(event.at)}
              </span>
              {text}
            </li>
          ) : null;
        })}
        {!isTerminal(run.state) && <li className="text-brand-info">working…</li>}
      </ol>

      {holding && (
        <div
          className={`px-4 py-3 border-t border-grey-light flex items-center gap-3 ${
            inFlight ? "bg-grey-lighter" : "bg-brand-danger/5"
          }`}
        >
          <button
            onClick={() => {
              if (askToRelease(run.live_resources)) released.mutate(run.live_resources);
            }}
            disabled={inFlight}
            className="px-3 py-1.5 rounded-md text-sm font-medium bg-brand-danger text-white disabled:opacity-40"
          >
            {inFlight ? "Releasing…" : "Release resources"}
          </button>
          <p className="text-xs text-grey">
            {inFlight
              ? waiting ? WAITING : "Releasing resources…"
              : named(run.live_resources)}
          </p>
        </div>
      )}
      {!holding && isTerminal(run.state) && (
        <p className="px-4 py-3 border-t border-grey-light text-xs text-grey">
          Resources released.
        </p>
      )}
      {released.error && (
        <div className="px-4 pb-3">
          <Failed error={released.error} />
        </div>
      )}
    </div>
  );
}

function Endpoint({ run }: { run: Run }) {
  const { connection } = run;
  if (!connection) return null;
  return (
    <div className="px-4 py-3 border-b border-grey-light">
      <p className="text-xs font-semibold text-grey uppercase tracking-wide mb-1">
        Log in
      </p>
      <code className="text-sm font-mono">
        ssh {connection.user}@{connection.host}
        {connection.port === 22 ? "" : ` -p ${connection.port}`}
      </code>
      {connection.expires_at && (
        <p className="text-xs text-grey mt-1">
          the lease ends {stamp(connection.expires_at)}
        </p>
      )}
    </div>
  );
}
