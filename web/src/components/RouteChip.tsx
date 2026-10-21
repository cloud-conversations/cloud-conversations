import type { Message } from "../api/types";

function label(message: Message): string | null {
  if (message.kind === "answer") return "from the documentation";
  if (message.kind === "holdings") return "read from the testbed";
  if (message.kind !== "description") return null;
  if (message.payload.again) return `version ${message.description_version} again`;
  return message.description_version === 1
    ? "experiment described"
    : `description revised to v${message.description_version}`;
}

export function RouteChip({ message }: { message: Message }) {
  const text = label(message);
  if (!text) return null;
  return (
    <div>
      <span className="text-xs text-grey bg-grey-lighter rounded px-1.5 py-0.5">
        {text}
      </span>
    </div>
  );
}
