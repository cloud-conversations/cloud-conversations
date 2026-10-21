import { useCallback, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import type { Feedback, Message, RunSummary, Version } from "../api/types";
import { AnswerMessage } from "../components/AnswerMessage";
import { Composer } from "../components/Composer";
import { DescriptionCard } from "../components/DescriptionCard";
import { Failed, said } from "../components/Failed";
import { ChatFeedback, Rate } from "../components/Feedback";
import { HoldingsMessage } from "../components/HoldingsMessage";
import { NoticeMessage } from "../components/NoticeMessage";
import { PendingRow } from "../components/PendingRow";
import { KeypairPicker } from "../components/KeypairPicker";
import { keypairLabel } from "../keypairs";
import { ProjectPicker } from "../components/ProjectPicker";
import { ReleaseMessage } from "../components/ReleaseMessage";
import { RouteChip } from "../components/RouteChip";
import { RunCard } from "../components/RunCard";
import { useMe } from "../hooks/useMe";
import { useApprove, usePostMessage, useThread } from "../hooks/useThread";

// How close to the bottom still counts as reading the newest message.
const AT_BOTTOM_PX = 120;

type Entry =
  | { at: string; kind: "message"; message: Message }
  | { at: string; kind: "run"; run: RunSummary };

/** Messages and runs in the order they happened. */
function timeline(messages: Message[], runs: RunSummary[]): Entry[] {
  const entries: Entry[] = [
    ...messages.map((message) => ({
      at: message.created_at,
      kind: "message" as const,
      message,
    })),
    ...runs.map((run) => ({ at: run.created_at, kind: "run" as const, run })),
  ];
  return entries.sort((a, b) => a.at.localeCompare(b.at));
}

function key(descriptionId: string, version: number): string {
  return `${descriptionId}:${version}`;
}

export function ThreadPage() {
  const { threadId } = useParams();
  const { data, isPending, error } = useThread(threadId);
  const me = useMe();
  const [draft, setDraft] = useState("");
  const posted = usePostMessage(threadId ?? "");
  const approved = useApprove(threadId ?? "");
  const scroller = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const growth = useRef<ResizeObserver | null>(null);

  /** Keeps the newest message in view while the chat grows. */
  const content = useCallback((el: HTMLDivElement | null) => {
    growth.current?.disconnect();
    growth.current = null;
    if (!el) return;
    growth.current = new ResizeObserver(() => {
      const outer = scroller.current;
      if (outer && following.current) outer.scrollTop = outer.scrollHeight;
    });
    growth.current.observe(el);
  }, []);

  if (!threadId) {
    return (
      <Centred>Start a new experiment, or pick one you were working on.</Centred>
    );
  }
  if (isPending) return <Centred>Loading…</Centred>;
  if (error || !data) return <Centred>That did not work: {said(error)}</Centred>;

  const { thread, messages, versions, jobs, runs, decisions, feedback } = data;
  const given = new Map(feedback.map((f) => [f.message_id ?? f.run_id, f]));
  const exchange = jobs.find((job) => job.kind === "exchange");
  const buildInFlight = jobs.some((job) => job.kind === "build");
  const byVersion = new Map<string, Version>(
    versions.map((v) => [key(v.description_id, v.version), v]),
  );
  const newest = new Map<string, number>();
  for (const v of versions) {
    newest.set(v.description_id, Math.max(newest.get(v.description_id) ?? 0, v.version));
  }
  const lineages = [...newest.keys()];
  const latest = [...messages]
    .reverse()
    .map((m) =>
      m.description_id && m.description_version
        ? byVersion.get(key(m.description_id, m.description_version))
        : undefined,
    )
    .find((v) => v !== undefined);

  function send() {
    following.current = true;
    posted.mutate(draft.trim(), { onSuccess: () => setDraft("") });
  }

  /** Resend the user's last message. */
  function askAgain() {
    const asked = [...messages].reverse().find((m) => m.role === "user");
    if (asked) posted.mutate(asked.text);
  }

  /** Records whether the reader is at the newest message. */
  function follow() {
    const el = scroller.current;
    if (!el) return;
    following.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < AT_BOTTOM_PX;
  }

  return (
    <>
      <div
        ref={scroller}
        onScroll={follow}
        className="flex-1 overflow-y-auto overscroll-contain"
      >
        <div ref={content} className="max-w-5xl mx-auto p-4 space-y-4">
          {messages.length === 0 && me.data && (
            <p className="text-sm text-grey">
              Ask about {me.data.testbed}, or describe an experiment you want to run on it.
            </p>
          )}

          {timeline(messages, runs).map((entry) => {
            if (entry.kind === "run") {
              const release = jobs.find(
                (job) => job.kind === "teardown" && job.run_id === entry.run.run_id,
              );
              return (
                <div key={entry.run.run_id} className="space-y-1">
                  <RunCard
                    runId={entry.run.run_id}
                    threadId={threadId}
                    releasing={release !== undefined}
                    waiting={release?.waiting ?? false}
                    building={buildInFlight}
                  />
                  <Rate
                    threadId={threadId}
                    target={{ run_id: entry.run.run_id }}
                    given={given.get(entry.run.run_id)}
                  />
                </div>
              );
            }
            const { description_id, description_version } = entry.message;
            const version =
              description_id && description_version
                ? byVersion.get(key(description_id, description_version))
                : undefined;
            return (
              <Said
                key={entry.message.message_id}
                message={entry.message}
                version={version}
                latest={version ? (newest.get(version.description_id) ?? 0) : 0}
                experiment={
                  version && lineages.length > 1
                    ? lineages.indexOf(version.description_id) + 1
                    : null
                }
                threadId={threadId}
                feedback={given.get(entry.message.message_id)}
                decision={decisions[entry.message.payload.request_id ?? ""]}
                keypair={keypairLabel(thread.ssh_key_ref, thread.site_keypairs)}
                buildInFlight={buildInFlight}
                onApprove={(descriptionId, chosen) =>
                  approved.mutate({ descriptionId, version: chosen })
                }
                onPrefill={setDraft}
                onAskAgain={askAgain}
              />
            );
          })}

          {jobs.map((job) => (
            <PendingRow key={job.job_id} job={job} />
          ))}
          <Failed error={approved.error} />
          <Failed error={posted.error} />
        </div>
      </div>

      <div className="max-w-5xl mx-auto w-full border-t border-grey-light bg-white">
        <div className="flex flex-wrap items-start gap-6 px-3 pt-2">
          <ProjectPicker threadId={thread.thread_id} project={thread.project_id} />
          <KeypairPicker
            threadId={thread.thread_id}
            keypair={thread.ssh_key_ref}
            siteKeypairs={thread.site_keypairs}
            site={latest?.requirements.site ?? null}
          />
          <div className="ml-auto">
            <ChatFeedback threadId={thread.thread_id} />
          </div>
        </div>
        <Composer
          value={draft}
          onChange={setDraft}
          onSend={send}
          disabled={Boolean(exchange) || posted.isPending}
          placeholder={
            exchange
              ? "waiting for the last message to be answered…"
              : thread.ssh_key_ref || thread.site_keypairs
                ? "Ask a question, or describe an experiment"
                : "Ask a question, or describe an experiment (a keypair is needed to build one)"
          }
        />
      </div>
    </>
  );
}

interface SaidProps {
  message: Message;
  version: Version | undefined;
  latest: number;
  experiment: number | null;
  threadId: string;
  feedback: Feedback | undefined;
  decision: "confirmed" | "declined" | undefined;
  keypair: string | null;
  buildInFlight: boolean;
  onApprove: (descriptionId: string, version: number) => void;
  onPrefill: (text: string) => void;
  onAskAgain: () => void;
}

function Said(props: SaidProps) {
  const { message, version } = props;

  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <p className="bg-brand-info/10 border border-brand-info/20 rounded-lg px-3 py-2 text-sm max-w-xl whitespace-pre-wrap">
          {message.text}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-1">
      <RouteChip message={message} />
      {message.kind === "answer" && <AnswerMessage message={message} />}
      {message.kind === "holdings" && <HoldingsMessage message={message} />}
      {message.kind === "release" && (
        <ReleaseMessage message={message} threadId={props.threadId} decision={props.decision} />
      )}
      {message.kind === "notice" && (
        <NoticeMessage
          message={message}
          threadId={props.threadId}
          keypair={props.keypair}
          onSuggestion={props.onPrefill}
          onSaved={
            version
              ? () => props.onApprove(version.description_id, version.version)
              : props.onAskAgain
          }
        />
      )}
      {message.kind === "description" && version && (
        <DescriptionCard
          version={version}
          latest={props.latest}
          experiment={props.experiment}
          buildInFlight={props.buildInFlight}
          onApprove={props.onApprove}
          onAnswer={(question) => props.onPrefill(`About "${question}": `)}
        />
      )}
      <Rate
        threadId={props.threadId}
        target={{ message_id: message.message_id }}
        given={props.feedback}
      />
    </div>
  );
}

function Centred({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex-1 flex items-center justify-center p-8">
      <p className="text-sm text-grey">{children}</p>
    </div>
  );
}
