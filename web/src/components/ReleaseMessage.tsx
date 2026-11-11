import type { ReactNode } from "react";
import type { Message } from "../api/types";
import { useConfirmRelease, useDeclineRelease } from "../hooks/useThread";
import { Failed } from "./Failed";

interface Props {
  message: Message;
  threadId: string;
  decision: "confirmed" | "declined" | undefined;
  children?: ReactNode;
}

/** The experiment agent's proposal to release a run, or to check its host again. */
export function ReleaseMessage({ message, threadId, decision, children }: Props) {
  const confirmed = useConfirmRelease(threadId);
  const declined = useDeclineRelease(threadId);
  const requestId = message.payload.request_id ?? "";
  const version = message.payload.version;
  const recheck = message.payload.action === "resume";
  const answering = confirmed.isPending || declined.isPending;

  return (
    <div className="bg-brand-danger/5 border border-brand-danger/30 rounded-lg p-4">
      <p className="text-sm leading-relaxed">{message.text}</p>
      {children && <div className="mt-3">{children}</div>}
      {decision === undefined ? (
        <div className="mt-3 flex gap-2">
          <button
            onClick={() => confirmed.mutate(requestId)}
            disabled={answering}
            className="px-3 py-1.5 rounded-md text-sm font-medium bg-brand-danger text-white disabled:opacity-40"
          >
            {recheck
              ? "Check the host again"
              : version
                ? `Release and build version ${version}`
                : "Release"}
          </button>
          <button
            onClick={() => declined.mutate(requestId)}
            disabled={answering}
            className="px-3 py-1.5 rounded-md text-sm border border-grey-light bg-white hover:bg-grey-lighter disabled:opacity-40"
          >
            {recheck ? "Don't check" : "Keep"}
          </button>
        </div>
      ) : (
        <p className="mt-3 text-sm text-grey">
          {recheck
            ? decision === "confirmed"
              ? "You confirmed checking the host again."
              : "You declined checking the host again."
            : decision === "confirmed"
              ? "You confirmed the release."
              : "You kept them."}
        </p>
      )}
      <Failed error={confirmed.error ?? declined.error} />
    </div>
  );
}
