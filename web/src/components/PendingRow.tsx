import type { Job } from "../api/types";

const WORKING: Record<string, string> = {
  describe: "Describing your experiment…",
  revise: "Revising the description…",
  build: "Finding that version…",
  inventory: "Reading what you have…",
  ask: "Searching the documentation…",
};

export const WAITING = "Waiting for another build to finish…";

/** What a job in flight is doing. */
export function PendingRow({ job }: { job: Job }) {
  if (job.kind === "build" && !job.waiting) return null;
  const text = job.waiting
    ? WAITING
    : job.kind === "teardown"
      ? "Releasing resources…"
      : (job.route && WORKING[job.route]) || "Reading your message…";

  return (
    <p className="text-sm text-grey flex items-center gap-2" role="status">
      <span className="w-2 h-2 rounded-full bg-brand-info animate-pulse" />
      {text}
    </p>
  );
}
