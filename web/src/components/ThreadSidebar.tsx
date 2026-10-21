import { Link, useNavigate, useParams } from "react-router-dom";
import { useCreateThread, useDeleteThread, useThreads } from "../hooks/useThreads";
import { Failed } from "./Failed";

const ACTIVITY = {
  running: {
    dot: "bg-brand-success motion-safe:animate-pulse",
    says: "A build or a release is running in this chat.",
  },
  reserved: {
    dot: "bg-orange-500",
    says: "This chat still has hardware reserved. Release it from the run card or your Profile when you're done.",
  },
};

export function ThreadSidebar({ width }: { width: number }) {
  const { threadId } = useParams();
  const { data: threads, isPending } = useThreads();
  const created = useCreateThread();
  const removed = useDeleteThread();
  const navigate = useNavigate();

  function startOne() {
    created.mutate(undefined, {
      onSuccess: (thread) => navigate(`/threads/${thread.thread_id}`),
    });
  }

  function remove(id: string, title: string) {
    if (!window.confirm(`Move "${title}" to the trash?`)) return;
    removed.mutate(id, {
      onSuccess: () => {
        if (id === threadId) navigate("/");
      },
    });
  }

  return (
    <aside
      style={{ width }}
      className="flex-shrink-0 bg-white flex flex-col overflow-hidden"
    >
      <div className="p-3">
        <button
          onClick={startOne}
          disabled={created.isPending}
          className="w-full py-2 rounded-md bg-brand-info text-white text-sm font-medium disabled:opacity-40"
        >
          New experiment
        </button>
      </div>
      <nav className="flex-1 overflow-y-auto pb-3" aria-label="Your experiments">
        {isPending && <p className="px-3 text-sm text-grey">Loading…</p>}
        {threads?.length === 0 && (
          <p className="px-3 text-sm text-grey">No experiments yet.</p>
        )}
        {threads?.map((thread) => (
          <div
            key={thread.thread_id}
            className={`group flex items-center border-l-2 ${
              thread.thread_id === threadId
                ? "border-brand-info bg-brand-info/5"
                : "border-transparent hover:bg-grey-lighter"
            }`}
          >
            <span className="w-2 ml-2 flex-shrink-0">
              {thread.activity && (
                <span
                  title={ACTIVITY[thread.activity].says}
                  className={`block w-2 h-2 rounded-full ${ACTIVITY[thread.activity].dot}`}
                >
                  <span className="sr-only">{ACTIVITY[thread.activity].says}</span>
                </span>
              )}
            </span>
            <Link
              to={`/threads/${thread.thread_id}`}
              className={`flex-1 min-w-0 px-2 py-2 text-sm truncate ${
                thread.thread_id === threadId ? "text-grey-dark" : "text-grey"
              }`}
              title={thread.title}
            >
              {thread.title}
            </Link>
            <button
              onClick={() => remove(thread.thread_id, thread.title)}
              disabled={removed.isPending}
              aria-label={`Delete ${thread.title}`}
              title="Delete this experiment"
              className="px-2 py-1 mr-1 text-grey-med opacity-0 group-hover:opacity-100 focus:opacity-100 hover:text-brand-danger disabled:opacity-40"
            >
              <svg viewBox="0 0 16 16" className="w-4 h-4" aria-hidden="true">
                <path
                  fill="currentColor"
                  d="M6 2h4v1h3v1.5H3V3h3V2Zm-2 4h8l-.7 8.1a1 1 0 0 1-1 .9H5.7a1 1 0 0 1-1-.9L4 6Z"
                />
              </svg>
            </button>
          </div>
        ))}
      </nav>
      {removed.error && (
        <div className="px-3 pb-3">
          <Failed error={removed.error} />
        </div>
      )}
      <Link to="/trash" className="px-3 py-2 text-sm text-grey hover:text-grey-dark">
        Trash
      </Link>
    </aside>
  );
}
