import { useState } from "react";
import { Link } from "react-router-dom";
import type { KeyChoice, KeyDeleted, Resource } from "../api/types";
import { Failed } from "../components/Failed";
import { KeyChooser } from "../components/KeyChooser";
import { SaveStatus } from "../components/SaveStatus";
import { useDeleteKey, useProfile, useRelease, useSetDefaults } from "../hooks/useProfile";
import { asDefault } from "../keypairs";
import { QUOTA_LABELS } from "../quotas";
import { askToRelease } from "../resources";
import { stamp } from "../time";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-grey-light bg-white p-5 shadow-xs">
      <h2 className="mb-3 font-medium text-cc-navy">{title}</h2>
      {children}
    </section>
  );
}

export function ProfilePage() {
  const profile = useProfile();
  const chosen = useSetDefaults();
  const defaultKey = useSetDefaults();
  const defaultSite = useSetDefaults();
  const deleted = useDeleteKey();
  const released = useRelease();
  const [report, setReport] = useState<KeyDeleted | null>(null);
  const [releasing, setReleasing] = useState<string[]>([]);

  if (!profile.data) return <Failed error={profile.error} />;
  const {
    testbed,
    username,
    projects,
    default_project_id,
    default_ssh_key_ref,
    default_site_keypairs,
    default_vm_site,
    default_baremetal_site,
    vm_sites,
    baremetal_sites,
    keys,
    sites,
    quotas,
    active_experiments,
  } = profile.data;
  const defaultKeypair: KeyChoice | null = default_site_keypairs
    ? { site_keypairs: default_site_keypairs }
    : default_ssh_key_ref
      ? { ssh_key_ref: default_ssh_key_ref }
      : null;
  const sitePickers = [
    { label: "VMs", field: "default_vm_site", value: default_vm_site, sites: vm_sites },
    {
      label: "Bare metal",
      field: "default_baremetal_site",
      value: default_baremetal_site,
      sites: baremetal_sites,
    },
  ] as const;

  function release(runId: string, resources: Resource[]) {
    if (!askToRelease(resources)) return;
    released.mutate(
      { runId, resources },
      { onSuccess: () => setReleasing((ids) => [...ids, runId]) },
    );
  }

  function forget(name: string) {
    if (!window.confirm(`Forget the key ${name}? Its keypairs stay at the sites.`)) return;
    deleted.mutate({ name, atSites: false }, { onSuccess: setReport });
  }

  function deleteEverywhere(name: string, keypairName: string) {
    if (!window.confirm(`Delete the keypair ${keypairName} at ${sites.join(", ")}?`)) return;
    deleted.mutate({ name, atSites: true }, { onSuccess: setReport });
  }

  return (
    <div className="flex-1 overflow-y-auto bg-grey-lighter">
      <div className="max-w-4xl mx-auto p-6 space-y-5">
        <div className="border-b border-grey-light pb-4">
          <h1 className="text-xl font-bold text-cc-navy">{username}</h1>
          <p className="mt-1 text-sm text-grey">Your projects, keys and defaults at {testbed}.</p>
        </div>

        <Section title="Usage">
          <ul className="text-sm space-y-1">
            {quotas.map((q) => (
              <li key={q.name}>
                {QUOTA_LABELS[q.name]}: {q.used} of {q.limit}
              </li>
            ))}
          </ul>
        </Section>

        <Section title="Active experiments">
          {active_experiments.length === 0 ? (
            <p className="text-sm text-grey">None of your experiments holds hardware.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-grey">
                <tr>
                  <th className="pb-1 pr-4 font-medium">Chat</th>
                  <th className="pb-1 pr-4 font-medium">Site</th>
                  <th className="pb-1 pr-4 font-medium">State</th>
                  <th className="pb-1 pr-4 font-medium">Lease ends</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {active_experiments.map((e) => (
                  <tr key={e.run_id} className="border-t border-grey-light">
                    <td className="py-2 pr-4">
                      {e.thread_id ? (
                        <Link to={`/threads/${e.thread_id}`} className="text-link">
                          {e.title}
                        </Link>
                      ) : (
                        <span className="font-mono">{e.run_id}</span>
                      )}
                    </td>
                    <td className="py-2 pr-4">{e.site}</td>
                    <td className="py-2 pr-4">{e.state}</td>
                    <td className="py-2 pr-4 whitespace-nowrap">
                      {e.ends_at ? stamp(e.ends_at) : "unknown"}
                    </td>
                    <td className="py-2 text-right whitespace-nowrap">
                      {releasing.includes(e.run_id) ? (
                        <span className="text-grey">Releasing…</span>
                      ) : (
                        <button
                          onClick={() => release(e.run_id, e.resources)}
                          disabled={released.isPending}
                          className="text-link disabled:opacity-40"
                        >
                          Release
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <Failed error={released.error} />
        </Section>

        <Section title="Default project">
          {projects.length === 0 ? (
            <p className="text-sm text-grey">This account is in no {testbed} project.</p>
          ) : (
            <div className="flex items-center gap-3">
              <select
                value={default_project_id ?? projects[0].id}
                onChange={(e) => chosen.mutate({ default_project_id: e.target.value })}
                aria-label="Default project"
                className="text-sm border border-grey-light rounded-md px-2 py-1 bg-white"
              >
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.nickname ? `${p.nickname} (${p.id})` : p.id}
                  </option>
                ))}
              </select>
              <SaveStatus status={chosen.status} />
            </div>
          )}
          <Failed error={chosen.error} />
        </Section>

        <Section title="Default sites">
          <div className="flex flex-wrap items-center gap-6 text-sm">
            {sitePickers.map((p) => (
              <label key={p.field} className="flex items-center gap-2">
                {p.label}
                <select
                  value={p.value ?? ""}
                  onChange={(e) => defaultSite.mutate({ [p.field]: e.target.value || null })}
                  className="border border-grey-light rounded-md px-2 py-1 bg-white"
                >
                  <option value="">No preference</option>
                  {p.sites.map((site) => (
                    <option key={site} value={site}>
                      {site}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <SaveStatus status={defaultSite.status} />
          </div>
          <Failed error={defaultSite.error} />
        </Section>

        <Section title="Default keypair">
          <p className="flex items-center gap-3 text-sm">
            {defaultKeypair ? (
              <>
                <span>
                  {"site_keypairs" in defaultKeypair ? "One at each site" : "An imported key"}
                </span>
                <button
                  onClick={() =>
                    defaultKey.mutate({ default_ssh_key_ref: null, default_site_keypairs: null })
                  }
                  className="text-link"
                >
                  Clear
                </button>
              </>
            ) : (
              <span className="text-grey">No default set</span>
            )}
            <SaveStatus status={defaultKey.status} />
          </p>
          <div className="mt-4 border-t border-grey-light pt-4">
            <KeyChooser
              save={(choice) => defaultKey.mutate(asDefault(choice))}
              saving={defaultKey.isPending}
              error={defaultKey.error}
              defaultChoice={defaultKeypair}
            />
          </div>
        </Section>

        <Section title="Imported keys">
          {keys.length === 0 ? (
            <p className="text-sm text-grey">None yet.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-grey">
                <tr>
                  <th className="pb-1 pr-4 font-medium">Name</th>
                  <th className="pb-1 pr-4 font-medium">Keypair</th>
                  <th className="pb-1 pr-4 font-medium">Fingerprint</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {keys.map((k) => (
                  <tr key={k.name} className="border-t border-grey-light">
                    <td className="py-2 pr-4">{k.name}</td>
                    <td className="py-2 pr-4 font-mono">{k.keypair_name}</td>
                    <td className="py-2 pr-4 font-mono">{k.fingerprint}</td>
                    <td className="py-2 space-x-3 text-right whitespace-nowrap">
                      <button onClick={() => forget(k.name)} className="text-link">
                        Forget
                      </button>
                      <button
                        onClick={() => deleteEverywhere(k.name, k.keypair_name)}
                        className="text-link"
                      >
                        Delete at every {testbed} site
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="mt-3 text-sm text-grey">
            Instances you build also accept the Cloud Conversations service key. Delete its line
            in <code className="font-mono">~/.ssh/authorized_keys</code> to remove its access.
          </p>
          <Failed error={deleted.error} />
          {report && report.sites.length > 0 && (
            <ul
              aria-label="Deletion at each site"
              className="mt-3 rounded-md bg-grey-lighter p-3 text-sm space-y-1"
            >
              {report.sites.map((s) => (
                <li key={s.site}>
                  {s.site}: {s.outcome}
                  {s.error ? `. Delete ${report.keypair_name} there yourself: ${s.error}` : ""}
                </li>
              ))}
            </ul>
          )}
        </Section>
      </div>
    </div>
  );
}
