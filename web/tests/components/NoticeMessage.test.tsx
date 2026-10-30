import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NoticeMessage } from "../../src/components/NoticeMessage";
import type { Message } from "../../src/api/types";

const NEEDS_KEYPAIR: Message = {
  message_id: "m1",
  role: "assistant",
  kind: "notice",
  text: "Before I can build this I need the key you will log in with.",
  payload: { needs: "ssh_key_ref" },
  description_id: null,
  description_version: null,
  created_at: "2026-09-15T10:00:00Z",
};

function show(message: Message, keypair: string | null = null) {
  const onSuggestion = vi.fn();
  const onSaved = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <NoticeMessage
        message={message}
        threadId="t1"
        keypair={keypair}
        onSuggestion={onSuggestion}
        onSaved={onSaved}
      />
    </QueryClientProvider>,
  );
  return { onSuggestion, onSaved };
}

afterEach(() => vi.unstubAllGlobals());

const PROFILE = {
  username: "alice",
  projects: [],
  default_project_id: null,
  keys: [{ name: "laptop", keypair_name: "cc-laptop-x7k2p9", fingerprint: "SHA256:a" }],
  sites: ["CHI@UC", "KVM@TACC"],
};

/** Answer each call by its path, and record what was sent. */
function serve(answers: Record<string, unknown>) {
  const sent: [string, string, string | undefined][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      sent.push([init?.method ?? "GET", path, init?.body as string | undefined]);
      return { ok: true, status: 200, json: async () => answers[path] ?? {} };
    }),
  );
  return sent;
}

const SET_ON = (keypair: string) => [
  "PATCH",
  "/api/threads/t1",
  JSON.stringify({ ssh_key_ref: keypair }),
];

describe("a notice", () => {
  it("asks for a keypair where that is what is missing", () => {
    serve({ "/api/profile": PROFILE });
    show(NEEDS_KEYPAIR);

    expect(screen.getByLabelText("One of my imported keys")).toBeInTheDocument();
    expect(screen.getByLabelText("A keypair I have at each site")).toBeInTheDocument();
    expect(screen.getByLabelText("Import a public key")).toBeInTheDocument();
  });

  it("names the keypair the chat already has instead of asking for one", () => {
    show(NEEDS_KEYPAIR, "cc-laptop-x7k2p9");

    expect(screen.getByText("cc-laptop-x7k2p9")).toBeInTheDocument();
    expect(screen.queryByLabelText("One of my imported keys")).not.toBeInTheDocument();
  });

  it("passes on the suggestion the user clicks", async () => {
    const { onSuggestion } = show({
      ...NEEDS_KEYPAIR,
      text: "Which site should it run at?",
      payload: { suggestions: ["CHI@UC", "KVM@TACC"] },
    });

    await userEvent.click(screen.getByRole("button", { name: "KVM@TACC" }));

    expect(onSuggestion).toHaveBeenCalledWith("KVM@TACC");
  });

  it("uses an imported key the user picks", async () => {
    const sent = serve({ "/api/profile": PROFILE });
    const { onSaved } = show(NEEDS_KEYPAIR);

    await screen.findByRole("option", { name: "laptop (cc-laptop-x7k2p9)" });
    await userEvent.selectOptions(screen.getByLabelText("Imported key"), "cc-laptop-x7k2p9");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(sent).toContainEqual(SET_ON("cc-laptop-x7k2p9"));
    expect(sent.filter(([method, path]) => method === "PATCH" && path === "/api/profile")).toEqual(
      [],
    );
  });

  it("uses the keypair the user picks at each site", async () => {
    const sent = serve({
      "/api/profile": PROFILE,
      "/api/profile/keypairs?site=CHI%40UC": [{ name: "my-uc-key" }],
      "/api/profile/keypairs?site=KVM%40TACC": [{ name: "my-tacc-key" }],
    });
    const { onSaved } = show(NEEDS_KEYPAIR);

    await userEvent.click(screen.getByLabelText("A keypair I have at each site"));
    await screen.findByRole("option", { name: "my-uc-key" });
    await userEvent.selectOptions(screen.getByLabelText("Keypair at CHI@UC"), "my-uc-key");
    await screen.findByRole("option", { name: "my-tacc-key" });
    await userEvent.selectOptions(screen.getByLabelText("Keypair at KVM@TACC"), "my-tacc-key");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(sent).toContainEqual([
      "PATCH",
      "/api/threads/t1",
      JSON.stringify({ site_keypairs: { "CHI@UC": "my-uc-key", "KVM@TACC": "my-tacc-key" } }),
    ]);
  });

  it("saves the keypairs it could load when a site's keypairs fail to load", async () => {
    const sent: [string, string, string | undefined][] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string, init?: RequestInit) => {
        sent.push([init?.method ?? "GET", path, init?.body as string | undefined]);
        if (path === "/api/profile/keypairs?site=KVM%40TACC") {
          return { ok: false, status: 502, json: async () => ({ detail: "the site is down" }) };
        }
        const answers: Record<string, unknown> = {
          "/api/profile": PROFILE,
          "/api/profile/keypairs?site=CHI%40UC": [{ name: "my-uc-key" }],
        };
        return { ok: true, status: 200, json: async () => answers[path] ?? {} };
      }),
    );
    const { onSaved } = show(NEEDS_KEYPAIR);

    await userEvent.click(screen.getByLabelText("A keypair I have at each site"));
    await screen.findByRole("option", { name: "my-uc-key" });
    await screen.findByText(/Couldn't load your keypairs at KVM@TACC/);
    await userEvent.selectOptions(screen.getByLabelText("Keypair at CHI@UC"), "my-uc-key");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(sent).toContainEqual([
      "PATCH",
      "/api/threads/t1",
      JSON.stringify({ site_keypairs: { "CHI@UC": "my-uc-key" } }),
    ]);
  });

  it("imports a pasted public key and uses the keypair it is given", async () => {
    const sent = serve({
      "/api/profile": PROFILE,
      "/api/keys": { name: "desk", keypair_name: "cc-desk-a1b2c3", fingerprint: "SHA256:b" },
    });
    const { onSaved } = show(NEEDS_KEYPAIR);

    await userEvent.click(screen.getByLabelText("Import a public key"));
    await userEvent.type(screen.getByLabelText("Key name"), "desk");
    await userEvent.type(screen.getByLabelText("Public key"), "ssh-ed25519 AAAA me@desk");
    await userEvent.click(screen.getByRole("button", { name: "Import" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(sent).toContainEqual([
      "POST",
      "/api/keys",
      JSON.stringify({ name: "desk", public_key: "ssh-ed25519 AAAA me@desk" }),
    ]);
    expect(sent).toContainEqual(SET_ON("cc-desk-a1b2c3"));
  });

  it("makes the key it uses the default for new chats when asked", async () => {
    const sent = serve({ "/api/profile": PROFILE });
    const { onSaved } = show(NEEDS_KEYPAIR);

    await screen.findByRole("option", { name: "laptop (cc-laptop-x7k2p9)" });
    await userEvent.selectOptions(screen.getByLabelText("Imported key"), "cc-laptop-x7k2p9");
    await userEvent.click(screen.getByLabelText("Make default for new chats"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(sent).toContainEqual(SET_ON("cc-laptop-x7k2p9"));
    await waitFor(() =>
      expect(sent).toContainEqual([
        "PATCH",
        "/api/profile",
        JSON.stringify({ default_ssh_key_ref: "cc-laptop-x7k2p9" }),
      ]),
    );
  });

  it("does not offer to make a default when there is one", async () => {
    serve({ "/api/profile": { ...PROFILE, default_ssh_key_ref: "cc-laptop-x7k2p9" } });
    show(NEEDS_KEYPAIR);

    await screen.findByRole("option", { name: "laptop (cc-laptop-x7k2p9)" });
    expect(screen.queryByLabelText("Make default for new chats")).not.toBeInTheDocument();
  });
});
