import { useNavigate } from "react-router-dom";
import { Failed } from "../components/Failed";
import { useRestoreThread, useTrash } from "../hooks/useThreads";
import { stamp } from "../time";

export function TrashPage() {
  const trash = useTrash();
  const restored = useRestoreThread();
  const navigate = useNavigate();

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-3xl mx-auto p-6">
        <h1 className="text-xl font-bold text-cc-navy">Trash</h1>
        <Failed error={trash.error ?? restored.error} />
        {trash.data?.length === 0 && <p className="mt-4 text-sm text-grey">The trash is empty.</p>}
        <ul className="mt-4">
          {trash.data?.map((t) => (
            <li
              key={t.thread_id}
              className="flex items-center gap-4 py-2 border-t border-grey-light text-sm"
            >
              <span className="flex-1 min-w-0 truncate text-grey-dark">{t.title}</span>
              <span className="text-grey whitespace-nowrap">
                Deleted {stamp(t.deleted_at)}. Restorable until {stamp(t.restorable_until)}.
              </span>
              <button
                onClick={() =>
                  restored.mutate(t.thread_id, {
                    onSuccess: () => navigate(`/threads/${t.thread_id}`),
                  })
                }
                disabled={restored.isPending}
                className="px-3 py-1 rounded-md bg-brand-info text-white disabled:opacity-40"
              >
                Restore
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
