import type { KeyChoice } from "./api/types";

export function keypairLabel(
  sshKeyRef: string | null,
  siteKeypairs: Record<string, string> | null,
): string | null {
  if (siteKeypairs) {
    return Object.entries(siteKeypairs)
      .map(([site, name]) => `${name} at ${site}`)
      .join(", ");
  }
  return sshKeyRef;
}

export function asDefault(choice: KeyChoice) {
  return "site_keypairs" in choice
    ? { default_site_keypairs: choice.site_keypairs }
    : { default_ssh_key_ref: choice.ssh_key_ref };
}
