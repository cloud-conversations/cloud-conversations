import type { Check, CheckResult, Requirement } from "./api/types";

/** A group of a description's software. */
export interface SoftwareGroup {
  label: string;
  keys: string[];
  kinds: string[];
}

/** In install order. */
export const SOFTWARE_GROUPS: SoftwareGroup[] = [
  { label: "System packages", keys: ["system_package"], kinds: ["package_installed"] },
  { label: "Repositories", keys: ["repository"], kinds: ["git_head"] },
  {
    label: "Python",
    keys: ["python_version", "pip_package"],
    kinds: ["python_version", "pip_package"],
  },
  { label: "Node", keys: ["node_version", "npm_package"], kinds: ["node_version", "npm_package"] },
  {
    label: "Docker",
    keys: ["docker", "container"],
    kinds: ["docker_running", "docker_gpu", "container_running", "port_listening"],
  },
];

const SOFTWARE_KEYS = new Set(SOFTWARE_GROUPS.flatMap((group) => group.keys));
const SOFTWARE_KINDS = new Set(SOFTWARE_GROUPS.flatMap((group) => group.kinds));

export function isSoftware(item: Requirement): boolean {
  return SOFTWARE_KEYS.has(item.key);
}

export function isSoftwareCheck(check: Check | CheckResult): boolean {
  return SOFTWARE_KINDS.has(check.kind);
}

/** A requirement's value or a check's expected value, as text. */
export function shown(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function field(value: unknown, name: string): string {
  const found = (value as Record<string, unknown> | null)?.[name];
  return found === undefined || found === null ? "" : String(found);
}

/** How an item reads in its group. */
export function itemText(item: Requirement): string {
  switch (item.key) {
    case "python_version":
      return `Python ${String(item.value)}`;
    case "node_version":
      return `Node ${String(item.value)}`;
    case "docker":
      return field(item.value, "gpu") === "true" ? "Docker with GPU access" : "Docker";
    case "container": {
      const ports = (item.value as { ports?: number[] }).ports ?? [];
      return [field(item.value, "image"), ...ports.map((port) => `port ${port}`)].join(" · ");
    }
    case "repository":
      return `${field(item.value, "url").replace(/^https:\/\//, "")} @ ${field(item.value, "ref").slice(0, 7)} in ~/${field(item.value, "path")}`;
    default:
      return String(item.value);
  }
}

/** The targets of the checks on an item. Null stands for a check with no target. */
function itemTargets(item: Requirement): (string | null)[] {
  switch (item.key) {
    case "pip_package":
      return [String(item.value).match(/^[A-Za-z0-9][A-Za-z0-9._-]*/)?.[0] ?? null];
    case "npm_package":
      return [String(item.value).match(/^(@[a-z0-9][a-z0-9._~-]*\/)?[a-z0-9][a-z0-9._~-]*/)?.[0] ?? null];
    case "container": {
      const ports = (item.value as { ports?: number[] }).ports ?? [];
      return [field(item.value, "image"), ...ports.map(String)];
    }
    case "repository":
      return [field(item.value, "path")];
    case "system_package":
      return [String(item.value)];
    default:
      return [null];
  }
}

export function checksFor(item: Requirement, rubric: Check[]): Check[] {
  const targets = itemTargets(item);
  return rubric.filter(
    (check) => check.from_requirement === item.key && targets.includes(check.target ?? null),
  );
}
