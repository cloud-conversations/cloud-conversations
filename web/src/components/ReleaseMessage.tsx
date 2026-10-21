import type { Message } from "../api/types";
import { useConfirmRelease, useDeclineRelease } from "../hooks/useThread";
import { Failed } from "./Failed";

interface Props {
  message: Message;
  threadId: string;
  decision: "confirmed" | "declined" | undefined;
}

/** The experiment agent's proposal to release a run. */
export function ReleaseMessage({ message, threadId, decision }: Props) {
  const confirmed = useConfirmRelease(threadId);
  const declined = useDeclineRelease(threadId);
  const requestId = message.payload.request_id ?? "";
  const version = message.payload.version;
  const answering = confirmed.isPending || declined.isPending;

  return (
    <div className="bg-brand-danger/5 border border-brand-danger/30 rounded-lg p-4">
      <p className="text-sm leading-relaxed">{message.text}</p>
      {decision === undefined ? (
        <div className="mt-3 flex gap-2">
          <button
            onClick={() => confirmed.mutate(requestId)}
            disabled={answering}
            className="px-3 py-1.5 rounded-md text-sm font-medium bg-brand-danger text-white disabled:opacity-40"
          >
            {version ? `Release and build version ${version}` : "Release"}
          </button>
          <button
            onClick={() => declined.mutate(requestId)}
            disabled={answering}
            className="px-3 py-1.5 rounded-md text-sm border border-grey-light bg-white hover:bg-grey-lighter disabled:opacity-40"
          >
            Keep
          </button>
        </div>
      ) : (
        <p className="mt-3 text-sm text-grey">
          {decision === "confirmed" ? "You confirmed the release." : "You kept them."}
        </p>
      )}
      <Failed error={confirmed.error ?? declined.error} />
    </div>
  );
}
