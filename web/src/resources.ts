import type { Resource } from "./api/types";

const RESOURCE: Record<string, { made: string; noun: string }> = {
  lease: { made: "reserved", noun: "lease" },
  instance: { made: "booted", noun: "instance" },
  floating_ip: { made: "allocated", noun: "floating IP" },
  security_group: { made: "created", noun: "security group" },
};

export const verb = (kind: string) => RESOURCE[kind]?.made ?? "took";
export const noun = (kind: string) => RESOURCE[kind]?.noun ?? kind;

/** The resources in one line, as in "lease L at CHI@UC, instance I at CHI@UC". */
export function named(resources: Resource[]): string {
  return resources.map((r) => `${noun(r.kind)} ${r.provider_id} at ${r.site}`).join(", ");
}

/** Ask the user to confirm releasing the resources. */
export function askToRelease(resources: Resource[]): boolean {
  return window.confirm(`Release ${named(resources)}? This deletes them at the testbed.`);
}
