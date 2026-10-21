import { useProfile } from "../hooks/useProfile";
import { useSetKeypair } from "../hooks/useThread";
import { Failed } from "./Failed";
import { SaveStatus } from "./SaveStatus";
import { SiteKeypairSelect } from "./SiteKeypairSelect";

/** The keypair this chat's instances boot with, at the site of its latest description. */
export function KeypairPicker({
  threadId,
  keypair,
  siteKeypairs,
  site,
}: {
  threadId: string;
  keypair: string | null;
  siteKeypairs: Record<string, string> | null;
  site: string | null;
}) {
  if (siteKeypairs && !(site && site in siteKeypairs)) {
    return <p className="text-sm text-grey">Keypair: one per site, shown once a site is chosen</p>;
  }
  return siteKeypairs && site ? (
    <AtSite threadId={threadId} siteKeypairs={siteKeypairs} site={site} />
  ) : (
    <Imported threadId={threadId} keypair={keypair} />
  );
}

function Imported({ threadId, keypair }: { threadId: string; keypair: string | null }) {
  const profile = useProfile();
  const chosen = useSetKeypair(threadId);
  const imported = (profile.data?.keys ?? []).map((k) => k.keypair_name);
  const options = keypair && !imported.includes(keypair) ? [keypair, ...imported] : imported;

  return (
    <div className="text-sm text-grey">
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-2">
          Keypair
          <select
            value={keypair ?? ""}
            onChange={(e) => chosen.mutate({ ssh_key_ref: e.target.value })}
            disabled={chosen.isPending}
            className="border border-grey-light rounded-md px-2 py-1 bg-white text-grey-dark"
          >
            {!keypair && (
              <option value="" disabled>
                none
              </option>
            )}
            {options.map((name) => (
              <option key={name} value={name}>
                {name}
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

function AtSite({
  threadId,
  siteKeypairs,
  site,
}: {
  threadId: string;
  siteKeypairs: Record<string, string>;
  site: string;
}) {
  const chosen = useSetKeypair(threadId);

  return (
    <div className="text-sm text-grey">
      <div className="flex items-center gap-2">
        Keypair at {site}
        <SiteKeypairSelect
          site={site}
          value={siteKeypairs[site]}
          onChange={(name) => chosen.mutate({ site_keypairs: { ...siteKeypairs, [site]: name } })}
          label={`Keypair at ${site}`}
          disabled={chosen.isPending}
        />
        <SaveStatus status={chosen.status} />
      </div>
      <Failed error={chosen.error} />
    </div>
  );
}
