import { said } from "./Failed";
import { useSiteKeypairs } from "../hooks/useProfile";

export function SiteKeypairSelect({
  site,
  value,
  onChange,
  label,
  disabled = false,
}: {
  site: string;
  value: string;
  onChange: (name: string) => void;
  label: string;
  disabled?: boolean;
}) {
  const keypairs = useSiteKeypairs(site);
  const names = (keypairs.data ?? []).map((k) => k.name);
  const options = value && !names.includes(value) ? [value, ...names] : names;

  return (
    <>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled || !keypairs.isSuccess}
        aria-label={label}
        className="flex-1 border border-grey-light rounded-md px-2 py-1 bg-white text-grey-dark disabled:bg-grey-lighter"
      >
        {!value && <option value="">choose one</option>}
        {options.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
      {keypairs.isPending && (
        <span role="status" className="text-xs text-grey">
          Loading your keypairs at {site}…
        </span>
      )}
      {keypairs.isSuccess && names.length === 0 && (
        <span className="text-xs text-grey">You have no keypairs at {site}.</span>
      )}
      {keypairs.error && (
        <span role="alert" className="text-xs text-brand-danger">
          Couldn't load your keypairs at {site}: {said(keypairs.error)}{" "}
          <button type="button" onClick={() => keypairs.refetch()} className="text-link">
            Retry
          </button>
        </span>
      )}
    </>
  );
}
