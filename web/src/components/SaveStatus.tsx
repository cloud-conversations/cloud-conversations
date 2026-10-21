/** Whether a change is on its way to the server, or the server has it. */
export function SaveStatus({ status }: { status: "idle" | "pending" | "success" | "error" }) {
  if (status === "pending") {
    return (
      <span role="status" className="text-sm text-grey">
        Saving…
      </span>
    );
  }
  if (status !== "success") return null;
  return (
    <span role="status" className="inline-flex items-center gap-1 text-sm text-brand-success">
      <svg viewBox="0 0 16 16" className="w-4 h-4" aria-hidden="true">
        <path
          d="M3 8.5l3 3 7-7"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      Saved
    </span>
  );
}
