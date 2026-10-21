import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { logInLocally, loginUrl } from "../api/client";
import { Failed } from "../components/Failed";
import { useAuthMethods } from "../hooks/useProfile";

/** next as a path on this site. */
function localPath(next: string | null): string {
  try {
    const url = new URL(next ?? "/", location.origin);
    return url.origin === location.origin ? url.pathname + url.search + url.hash : "/";
  } catch {
    return "/";
  }
}

export function LoginPage() {
  const [params] = useSearchParams();
  const next = localPath(params.get("next"));
  const methods = useAuthMethods();

  return (
    <div className="min-h-screen flex items-center justify-center bg-grey-lighter">
      <div className="bg-white rounded-lg shadow-sm p-8 w-96 space-y-6">
        <h1 className="text-2xl font-bold text-cc-navy">Cloud Conversations</h1>
        {methods.data?.refused && (
          <p role="alert" className="text-sm text-brand-danger">
            You are not logged in: {methods.data.refused}
          </p>
        )}
        {methods.data?.logins.map((login) => (
          <a
            key={login.testbed}
            href={loginUrl(login.testbed, next)}
            className="block text-center px-4 py-2 rounded-md bg-brand-info text-white"
          >
            Log in with {login.label}
          </a>
        ))}
        {methods.data?.local && <LocalForm next={next} />}
      </div>
    </div>
  );
}

function LocalForm({ next }: { next: string }) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);

  return (
    <form
      className="space-y-2 border-t border-grey-light pt-4"
      onSubmit={(e) => {
        e.preventDefault();
        logInLocally(name, password).then(() => window.location.assign(next), setError);
      }}
    >
      <p className="text-sm text-grey">Local test user</p>
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        aria-label="Name"
        className="w-full text-sm border border-grey-light rounded-md px-2 py-1"
      />
      <input
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        aria-label="Password"
        className="w-full text-sm border border-grey-light rounded-md px-2 py-1"
      />
      <button
        type="submit"
        disabled={!name || !password}
        className="w-full text-sm px-3 py-1 rounded-md border border-grey-light disabled:opacity-40"
      >
        Log in
      </button>
      <Failed error={error} />
    </form>
  );
}
