import { useState } from "react";
import type { KeyChoice } from "../api/types";
import { Failed } from "./Failed";
import { useImportKey, useProfile } from "../hooks/useProfile";
import { SiteKeypairSelect } from "./SiteKeypairSelect";

type Source = "imported" | "import" | "site";

interface Saving {
  save: (choice: KeyChoice) => void;
  saving: boolean;
  error: unknown;
}

/** One of the user's imported keys, a key to import, or a keypair they have at each site. */
export function KeyChooser({
  save,
  saving,
  error,
  defaultChoice = null,
}: Saving & { defaultChoice?: KeyChoice | null }) {
  const atSites =
    defaultChoice && "site_keypairs" in defaultChoice ? defaultChoice.site_keypairs : {};
  const imported =
    defaultChoice && "ssh_key_ref" in defaultChoice ? defaultChoice.ssh_key_ref : "";
  const defaultSource: Source | null =
    defaultChoice && ("site_keypairs" in defaultChoice ? "site" : "imported");
  const [picked, setPicked] = useState<Source | null>(null);
  const source = picked ?? defaultSource ?? "imported";
  const choices: [Source, string][] = [
    ["imported", "One of my imported keys"],
    ["import", "Import a public key"],
    ["site", "A keypair I have at each site"],
  ];

  return (
    <div>
      <div className="flex gap-4 text-sm">
        {choices.map(([value, label]) => (
          <label key={value} className="flex items-center gap-1">
            <input
              type="radio"
              checked={source === value}
              onChange={() => setPicked(value)}
            />
            {label}
            {value === defaultSource && (
              <span className="ml-1 text-xs px-1.5 rounded bg-brand-info/10 text-brand-info">
                default
              </span>
            )}
          </label>
        ))}
      </div>
      {source === "imported" && (
        <ImportedForm
          key={imported}
          save={save}
          saving={saving}
          error={error}
          initial={imported}
        />
      )}
      {source === "import" && <ImportForm save={save} saving={saving} error={error} />}
      {source === "site" && (
        <SiteForm
          key={JSON.stringify(atSites)}
          save={save}
          saving={saving}
          error={error}
          initial={atSites}
        />
      )}
    </div>
  );
}

function ImportedForm({ save, saving, error, initial }: Saving & { initial: string }) {
  const profile = useProfile();
  const keys = profile.data?.keys ?? [];
  const [chosen, setChosen] = useState(initial);

  if (profile.data && keys.length === 0) {
    return <p className="mt-2 text-sm text-grey">You have not imported a key yet.</p>;
  }
  return (
    <Choose
      label="Imported key"
      options={keys.map((k) => [k.keypair_name, `${k.name} (${k.keypair_name})`])}
      chosen={chosen}
      onChoose={setChosen}
      onSave={() => save({ ssh_key_ref: chosen })}
      pending={saving}
      error={error ?? profile.error}
    />
  );
}

function SiteForm({
  save,
  saving,
  error,
  initial,
}: Saving & { initial: Record<string, string> }) {
  const profile = useProfile();
  const sites = profile.data?.sites ?? [];
  const [chosen, setChosen] = useState(initial);
  const picked = Object.fromEntries(Object.entries(chosen).filter(([, name]) => name));
  const ready = Object.keys(picked).length > 0;

  return (
    <form
      className="mt-2 flex flex-col gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (ready) save({ site_keypairs: picked });
      }}
    >
      {sites.map((site) => (
        <SiteKeypair
          key={site}
          site={site}
          chosen={chosen[site] ?? ""}
          onChoose={(name) => setChosen((was) => ({ ...was, [site]: name }))}
        />
      ))}
      <button
        type="submit"
        disabled={!ready || saving}
        className="self-end text-sm px-3 py-1 rounded-md bg-brand-info text-white disabled:opacity-40"
      >
        Save
      </button>
      <Failed error={error ?? profile.error} />
    </form>
  );
}

function SiteKeypair({
  site,
  chosen,
  onChoose,
}: {
  site: string;
  chosen: string;
  onChoose: (name: string) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="w-24">{site}</span>
      <SiteKeypairSelect
        site={site}
        value={chosen}
        onChange={onChoose}
        label={`Keypair at ${site}`}
      />
    </label>
  );
}

function Choose({
  label,
  options,
  chosen,
  onChoose,
  onSave,
  pending,
  error,
}: {
  label: string;
  options: [string, string][];
  chosen: string;
  onChoose: (value: string) => void;
  onSave: () => void;
  pending: boolean;
  error: unknown;
}) {
  return (
    <>
      <form
        className="mt-2 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (chosen) onSave();
        }}
      >
        <select
          value={chosen}
          onChange={(e) => onChoose(e.target.value)}
          aria-label={label}
          className="flex-1 text-sm border border-grey-light rounded-md px-2 py-1 bg-white"
        >
          <option value="">choose one</option>
          {options.map(([value, text]) => (
            <option key={value} value={value}>
              {text}
            </option>
          ))}
        </select>
        <button
          type="submit"
          disabled={!chosen || pending}
          className="text-sm px-3 py-1 rounded-md bg-brand-info text-white disabled:opacity-40"
        >
          Save
        </button>
      </form>
      <Failed error={error} />
    </>
  );
}

function ImportForm({ save, saving, error }: Saving) {
  const [name, setName] = useState("");
  const [publicKey, setPublicKey] = useState("");
  const imported = useImportKey();
  const ready = name.trim() && publicKey.trim();

  return (
    <>
      <form
        className="mt-2 flex flex-col gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready)
            imported.mutate(
              { name: name.trim(), publicKey: publicKey.trim() },
              { onSuccess: (key) => save({ ssh_key_ref: key.keypair_name }) },
            );
        }}
      >
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="a name for the key, e.g. laptop"
          aria-label="Key name"
          className="text-sm border border-grey-light rounded-md px-2 py-1 bg-white"
        />
        <textarea
          value={publicKey}
          onChange={(e) => setPublicKey(e.target.value)}
          placeholder="ssh-ed25519 AAAA… you@host"
          aria-label="Public key"
          rows={3}
          className="text-sm font-mono border border-grey-light rounded-md px-2 py-1 bg-white"
        />
        <button
          type="submit"
          disabled={!ready || imported.isPending || saving}
          className="self-end text-sm px-3 py-1 rounded-md bg-brand-info text-white disabled:opacity-40"
        >
          Import
        </button>
      </form>
      <Failed error={imported.error ?? error} />
    </>
  );
}
