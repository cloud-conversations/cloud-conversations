import { useState } from "react";
import type { Feedback, Rating } from "../api/types";
import { useSendFeedback } from "../hooks/useThread";
import { Failed } from "./Failed";

const THUMBS: [Rating, string, string][] = [
  ["up", "👍", "Helpful"],
  ["down", "👎", "Not helpful"],
];

const ASKED: Record<Rating, string> = {
  up: "What did you like?",
  down: "What could be better?",
};

interface RateProps {
  threadId: string;
  target: { message_id: string } | { run_id: string };
  given: Feedback | undefined;
}

/** Thumbs up or down on a message or run, then an optional comment. */
export function Rate({ threadId, target, given }: RateProps) {
  const send = useSendFeedback(threadId);
  const [chosen, setChosen] = useState<Rating | null>(null);
  const [commented, setCommented] = useState(false);
  const rating = chosen ?? given?.rating ?? null;

  function rate(picked: Rating) {
    setChosen(picked);
    setCommented(false);
    send.mutate({ ...target, rating: picked, comment: given?.comment ?? null });
  }

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      {THUMBS.map(([value, thumb, label]) => (
        <button
          key={value}
          type="button"
          onClick={() => rate(value)}
          aria-label={label}
          aria-pressed={rating === value}
          className={rating === value ? "opacity-100" : "opacity-40 hover:opacity-100"}
        >
          {thumb}
        </button>
      ))}
      {chosen && commented && <span className="text-grey">Thanks.</span>}
      {chosen && !commented && (
        <Comment
          label={ASKED[chosen]}
          pending={send.isPending}
          onSend={(comment) =>
            send.mutate(
              { ...target, rating: chosen, comment },
              { onSuccess: () => setCommented(true) },
            )
          }
        />
      )}
      <Failed error={send.error} />
    </div>
  );
}

/** A comment on the whole chat. */
export function ChatFeedback({ threadId }: { threadId: string }) {
  const send = useSendFeedback(threadId);
  const [state, setState] = useState<"closed" | "open" | "sent">("closed");

  if (state === "sent") return <p className="text-sm text-grey">Thanks for the feedback.</p>;
  if (state === "closed") {
    return (
      <button type="button" onClick={() => setState("open")} className="text-sm text-link">
        Send feedback
      </button>
    );
  }
  return (
    <div className="flex-1">
      <Comment
        label="Feedback on this chat"
        pending={send.isPending}
        onSend={(comment) => send.mutate({ comment }, { onSuccess: () => setState("sent") })}
      />
      <Failed error={send.error} />
    </div>
  );
}

interface CommentProps {
  label: string;
  pending: boolean;
  onSend: (comment: string) => void;
}

function Comment({ label, pending, onSend }: CommentProps) {
  const [text, setText] = useState("");

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSend(text.trim());
      }}
      className="flex flex-1 items-center gap-2"
    >
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        aria-label={label}
        placeholder={label}
        className="flex-1 min-w-48 text-sm border border-grey-light rounded-md px-2 py-1"
      />
      <button
        type="submit"
        disabled={!text.trim() || pending}
        className="text-sm text-link disabled:opacity-40"
      >
        Send
      </button>
    </form>
  );
}
