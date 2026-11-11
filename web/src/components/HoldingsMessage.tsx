import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type { HoldingKind, HoldingRow, Message } from "../api/types";
import { day } from "../time";
import { ACTIVITY } from "./ThreadSidebar";

const ENDED = ["released", "gone"];

export function HoldingsMessage({ message }: { message: Message }) {
  const { tables, kinds, rows, left_out, unread } = message.payload;
  return (
    <div className="bg-white border border-grey-light rounded-lg p-4 text-sm">
      {rows && kinds ? (
        <Listing text={message.text} kinds={kinds} rows={rows} />
      ) : (
        <>
          <p className="leading-relaxed">{message.text}</p>
          {(tables ?? []).map((table) => (
            <pre
              key={table}
              className="mt-3 text-xs font-mono bg-grey-lighter rounded p-3 overflow-x-auto"
            >
              {table}
            </pre>
          ))}
        </>
      )}
      {(left_out ?? []).map((line) => (
        <p key={line} className="mt-2 text-xs text-grey">
          {line}
        </p>
      ))}
      {Object.entries(unread ?? {}).map(([site, error]) => (
        <p key={site} className="mt-2 text-xs text-grey">
          {site} wasn't read. <span className="text-brand-danger">{error}</span>
        </p>
      ))}
    </div>
  );
}

function Listing({ text, kinds, rows }: { text: string; kinds: HoldingKind[]; rows: HoldingRow[] }) {
  if (rows.length === 0) return <p>{text}</p>;

  const hardware = new Set(kinds.filter((k) => k.holds_hardware).map((k) => k.name));
  const runs = byRun(rows.filter((r) => r.made_by === "experiment"));
  const live = runs.filter((run) => run.some((r) => !ENDED.includes(r.status)));
  const ended = runs.filter((run) => !live.includes(run));
  const others = rows.filter((r) => r.made_by !== "experiment");
  const running = others.filter((r) => hardware.has(r.kind));
  const rest = others.filter((r) => !hardware.has(r.kind));
  const restKinds = kinds.filter((k) => rest.some((r) => r.kind === k.name));
  const onlyRest = live.length + running.length + ended.length === 0;
  return (
    <div className="space-y-3">
      {live.length > 0 ? (
        <section className="rounded-md border border-brand-success/30 bg-brand-success/10 px-3 py-2">
          <p className="text-xs font-medium text-brand-success mb-0.5">Your running experiments</p>
          {live.map((run) => {
            const [first] = run;
            const held = run.some((r) => hardware.has(r.kind) && !ENDED.includes(r.status));
            return (
              <Line
                key={first.run_id}
                held={held}
                fact={[first.site, until(first)].filter(Boolean).join(", ")}
              >
                <Chat row={first} />
              </Line>
            );
          })}
        </section>
      ) : (
        hardware.size > 0 && (
          <p className="rounded-md bg-grey-lighter px-3 py-2">
            {running.length > 0 ? "No experiments are running." : "No resources are running."}
          </p>
        )
      )}
      {running.length > 0 && (
        <section className="rounded-md border border-grey-light px-3 py-2 text-grey-dark">
          <p className="text-xs text-grey mb-0.5">Other running resources</p>
          {running.map((r) => (
            <Line key={`${r.site}:${r.resource_id}`} held fact={fact(r)}>
              {r.name}
            </Line>
          ))}
        </section>
      )}
      {ended.length > 0 && (
        <Toggle label={`Already released (${ended.length})`}>
          {ended.map(([first]) => (
            <Line key={first.run_id} fact={first.released_at ? day(first.released_at) : "gone"}>
              <Chat row={first} />
            </Line>
          ))}
        </Toggle>
      )}
      {rest.length > 0 && (
        <Toggle label={`Also in your project (${rest.length})`} startOpen={onlyRest}>
          {restKinds.map((kind) => (
            <Folded
              key={kind.name}
              kind={kind}
              rows={rest.filter((r) => r.kind === kind.name)}
              startOpen={onlyRest && restKinds.length === 1}
            />
          ))}
        </Toggle>
      )}
    </div>
  );
}

function Toggle({
  label,
  startOpen = false,
  children,
}: {
  label: string;
  startOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(startOpen);
  return (
    <section className="text-grey">
      <button onClick={() => setOpen(!open)} aria-expanded={open}>
        {label}
      </button>
      {open && <div className="pl-4">{children}</div>}
    </section>
  );
}

function Folded({
  kind,
  rows,
  startOpen,
}: {
  kind: HoldingKind;
  rows: HoldingRow[];
  startOpen: boolean;
}) {
  const [open, setOpen] = useState(startOpen);
  return (
    <>
      <Line fact={String(rows.length)}>
        <button onClick={() => setOpen(!open)} aria-expanded={open}>
          {kind.label}
        </button>
      </Line>
      {open && (
        <div className="pl-4">
          {rows.map((r) => (
            <Line key={`${r.site}:${r.resource_id}`} fact={fact(r)}>
              {r.name}
            </Line>
          ))}
        </div>
      )}
    </>
  );
}

function Line({
  fact,
  held,
  children,
}: {
  fact?: ReactNode;
  held?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="flex items-baseline gap-4 py-1.5 border-t border-grey-light first-of-type:border-t-0">
      <span className="flex-1 min-w-0 truncate">
        {held && (
          <span
            title="Holds hardware"
            className={`inline-block w-2 h-2 mr-2 rounded-full ${ACTIVITY.reserved.dot}`}
          >
            <span className="sr-only">Holds hardware</span>
          </span>
        )}
        {children}
      </span>
      {fact && <span className="text-grey whitespace-nowrap">{fact}</span>}
    </div>
  );
}

function fact(row: HoldingRow): string {
  return [row.site, row.status.toLowerCase()].filter(Boolean).join(", ");
}

function until(row: HoldingRow): string {
  return row.lease_ends ? `until ${day(row.lease_ends)}` : "";
}

function Chat({ row }: { row: HoldingRow }) {
  const name = row.title ?? "your experiment";
  if (!row.thread_id) return <>{name}</>;
  return (
    <Link to={`/threads/${row.thread_id}#run-${row.run_id}`} className="text-link">
      {name}
    </Link>
  );
}

function byRun(rows: HoldingRow[]): HoldingRow[][] {
  const runs = new Map<string, HoldingRow[]>();
  for (const r of rows) {
    const id = r.run_id ?? "";
    runs.set(id, [...(runs.get(id) ?? []), r]);
  }
  return [...runs.values()];
}
