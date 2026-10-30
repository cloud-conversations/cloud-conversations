import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { KeypairPicker } from "../../src/components/KeypairPicker";

const PROFILE = {
  keys: [{ name: "laptop", keypair_name: "cc-laptop-x7k2p9", fingerprint: "SHA256:a" }],
};

function show(
  keypair: string | null,
  siteKeypairs: Record<string, string> | null = null,
  site: string | null = null,
) {
  const calls: [string, string, string | undefined][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      calls.push([init?.method ?? "GET", path, init?.body as string | undefined]);
      const answers: Record<string, unknown> = {
        "/api/profile": PROFILE,
        "/api/profile/keypairs?site=CHI%40UC": [{ name: "uc-key-2" }, { name: "uc-key" }],
      };
      return { ok: true, status: 200, json: async () => answers[path] ?? {} };
    }),
  );
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <KeypairPicker threadId="t1" keypair={keypair} siteKeypairs={siteKeypairs} site={site} />
    </QueryClientProvider>,
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("the keypair picker", () => {
  it("keeps a keypair the user has at a site among the choices", async () => {
    show("my-site-keypair");

    expect(await screen.findByRole("option", { name: "cc-laptop-x7k2p9" })).toBeInTheDocument();
    expect(screen.getByLabelText("Keypair")).toHaveValue("my-site-keypair");
  });

  it("offers no keypair until a chat with one per site has a site", () => {
    show(null, { "CHI@UC": "uc-key", "KVM@TACC": "tacc-key" });

    expect(screen.getByText(/^Keypair:/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("offers the keypairs at the chat's site, and changes that site's alone", async () => {
    const calls = show(null, { "CHI@UC": "uc-key", "KVM@TACC": "tacc-key" }, "CHI@UC");
    await screen.findByRole("option", { name: "uc-key-2" });
    expect(screen.getByLabelText("Keypair at CHI@UC")).toHaveValue("uc-key");

    await userEvent.selectOptions(screen.getByLabelText("Keypair at CHI@UC"), "uc-key-2");

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/threads/t1",
        JSON.stringify({ site_keypairs: { "CHI@UC": "uc-key-2", "KVM@TACC": "tacc-key" } }),
      ]),
    );
  });

  it("asks for a keypair at the chat's site if the chat has none there yet", async () => {
    const calls = show(null, { "KVM@TACC": "tacc-key" }, "CHI@UC");
    await screen.findByRole("option", { name: "uc-key-2" });

    await userEvent.selectOptions(screen.getByLabelText("Keypair at CHI@UC"), "uc-key-2");

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/threads/t1",
        JSON.stringify({ site_keypairs: { "KVM@TACC": "tacc-key", "CHI@UC": "uc-key-2" } }),
      ]),
    );
  });

  it("moves the chat to the imported key picked", async () => {
    const calls = show(null);
    await screen.findByRole("option", { name: "cc-laptop-x7k2p9" });

    await userEvent.selectOptions(screen.getByLabelText("Keypair"), "cc-laptop-x7k2p9");

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/threads/t1",
        JSON.stringify({ ssh_key_ref: "cc-laptop-x7k2p9" }),
      ]),
    );
  });
});
