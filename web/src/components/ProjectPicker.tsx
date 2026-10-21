import { useMe } from "../hooks/useMe";
import { useSetProject } from "../hooks/useThread";
import { Failed } from "./Failed";
import { SaveStatus } from "./SaveStatus";

/** The project this chat builds in and is billed to. */
export function ProjectPicker({
  threadId,
  project,
}: {
  threadId: string;
  project: string | null;
}) {
  const me = useMe();
  const chosen = useSetProject(threadId);
  const projects = me.data?.projects ?? [];

  return (
    <div className="text-sm text-grey">
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-2">
          Project
          <select
            value={project ?? ""}
            onChange={(e) => chosen.mutate(e.target.value)}
            disabled={chosen.isPending}
            className="border border-grey-light rounded-md px-2 py-1 bg-white text-grey-dark"
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.nickname ? `${p.nickname} (${p.id})` : p.id}
              </option>
            ))}
          </select>
        </label>
        <SaveStatus status={chosen.status} />
      </div>
      <Failed error={chosen.error} />
    </div>
  );
}
