import { useState } from "react";
import type { Check, CheckResult, Requirement } from "../api/types";
import { SOFTWARE_GROUPS, checksFor, itemText, shown } from "../software";

/** Items that name a group rather than sit in it. */
const HEADS = new Set(["python_version", "node_version", "docker"]);

function Group({
  label,
  summary,
  initiallyOpen = false,
  children,
}: {
  label: string;
  summary: string;
  initiallyOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <div className="border-b border-grey-lighter">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="w-full flex items-baseline gap-2 px-4 py-1 text-left text-sm"
      >
        <span aria-hidden="true" className="w-3 text-grey">
          {open ? "▾" : "▸"}
        </span>
        <span className="w-36 text-grey">{label}</span>
        <span>{summary}</span>
      </button>
      {open && <div className="pl-9 pr-4 pb-2">{children}</div>}
    </div>
  );
}

/** A description's software, one group per line, each item with its source and checks. */
export function SoftwareRequirements({ items, rubric }: { items: Requirement[]; rubric: Check[] }) {
  const groups = SOFTWARE_GROUPS.map((group) => ({
    group,
    items: items.filter((item) => group.keys.includes(item.key)),
  })).filter(({ items }) => items.length > 0);

  return (
    <>
      {groups.map(({ group, items }) => {
        const head = items.find((item) => HEADS.has(item.key));
        const named = items.filter((item) => !HEADS.has(item.key)).map(itemText);
        const listed = named.slice(0, 3).join(", ") + (named.length > 3 ? `, +${named.length - 3} more` : "");
        const counts = (["inferred", "default"] as const)
          .map((source) => [source, items.filter((item) => item.source === source).length] as const)
          .filter(([, count]) => count > 0)
          .map(([source, count]) => `${count} ${source}`);
        return (
          <Group
            key={group.label}
            label={head ? itemText(head) : group.label}
            summary={[named.length ? `${named.length} · ${listed}` : "", ...counts]
              .filter(Boolean)
              .join(" · ")}
          >
            <ul className="space-y-1">
              {items.map((item, index) => (
                <li key={`${item.key}-${index}`} className="text-sm">
                  <span className="font-mono text-xs">{itemText(item)}</span>
                  <span className="ml-2 text-xs text-grey">{item.source}</span>
                  {checksFor(item, rubric).map((check) => (
                    <div key={check.id} className="font-mono text-xs text-grey">
                      {check.kind} {check.target && `${check.target} `}{check.comparator}{" "}
                      {shown(check.expected)}
                    </div>
                  ))}
                </li>
              ))}
            </ul>
          </Group>
        );
      })}
    </>
  );
}

/** A run's software check results, one group per line, groups with a failure open. */
export function SoftwareResults({ results }: { results: CheckResult[] }) {
  const groups = SOFTWARE_GROUPS.map((group) => ({
    group,
    results: results.filter((result) => group.kinds.includes(result.kind)),
  })).filter(({ results }) => results.length > 0);

  return (
    <>
      {groups.map(({ group, results }) => {
        const passed = results.filter((result) => result.passed).length;
        return (
          <Group
            key={group.label}
            label={group.label}
            summary={`${passed} of ${results.length} passed`}
            initiallyOpen={passed < results.length}
          >
            <ul className="space-y-0.5">
              {results.map((result) => (
                <li key={result.check_id} className="text-sm flex gap-2">
                  <span className={result.passed ? "text-brand-success" : "text-brand-danger"}>
                    {result.passed ? "PASS" : "FAIL"}
                  </span>
                  <span className="text-grey">
                    {result.kind} {result.target}
                  </span>
                  <span className="font-mono text-xs">
                    wanted {String(result.expected)}, got {String(result.actual)}
                  </span>
                </li>
              ))}
            </ul>
          </Group>
        );
      })}
    </>
  );
}
