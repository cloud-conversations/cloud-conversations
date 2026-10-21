import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProfilePage } from "../../src/pages/ProfilePage";

const PROFILE = {
  testbed: "Chameleon",
  username: "alice",
  projects: [{ id: "CHI-000001", nickname: "" }, { id: "CHI-000002", nickname: "two" }],
  default_project_id: null,
  keys: [{ name: "laptop", keypair_name: "cc-laptop-x7k2p9", fingerprint: "SHA256:a" }],
  sites: ["CHI@UC", "KVM@TACC"],
  default_vm_site: null,
  default_baremetal_site: null,
  vm_sites: ["KVM@TACC"],
  baremetal_sites: ["CHI@UC"],
  quotas: [
    { name: "max_messages_per_day", used: 37, limit: 500, default: 200, override: 500 },
  ],
  active_experiments: [
    {
      run_id: "r1",
      state: "passed",
      site: "CHI@UC",
      ends_at: null,
      thread_id: "t1",
      title: "an A100 for a day",
      resources: [{ kind: "lease", provider_id: "lease-1", site: "CHI@UC" }],
    },
  ],
};

const DELETED_EVERYWHERE = {
  name: "laptop",
  keypair_name: "cc-laptop-x7k2p9",
  sites: [
    { site: "CHI@UC", outcome: "deleted", error: null },
    { site: "KVM@TACC", outcome: "failed", error: "KVM@TACC is unreachable" },
  ],
};

function show(profile: object = PROFILE) {
  const sent: [string, string, string | undefined][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      sent.push([method, path, init?.body as string | undefined]);
      const body = method === "DELETE" ? DELETED_EVERYWHERE : profile;
      return { ok: true, status: 200, json: async () => body };
    }),
  );
  render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <ProfilePage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return sent;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the profile page", () => {
  it("shows each limit against its use", async () => {
    show();

    expect(await screen.findByText("Messages today: 37 of 500")).toBeInTheDocument();
  });

  it("sets the site new bare-metal requests prefer", async () => {
    const sent = show();

    await userEvent.selectOptions(await screen.findByLabelText("Bare metal"), "CHI@UC");

    await waitFor(() =>
      expect(sent).toContainEqual([
        "PATCH",
        "/api/profile",
        JSON.stringify({ default_baremetal_site: "CHI@UC" }),
      ]),
    );
  });

  it("releases an active experiment once confirmed", async () => {
    const asked = vi.spyOn(window, "confirm").mockReturnValue(true);
    const sent = show();

    await userEvent.click(await screen.findByRole("button", { name: "Release" }));

    expect(asked).toHaveBeenCalledWith(
      "Release lease lease-1 at CHI@UC? This deletes them at the testbed.",
    );
    await waitFor(() =>
      expect(sent).toContainEqual([
        "POST",
        "/api/runs/r1/teardown",
        JSON.stringify({ resources: PROFILE.active_experiments[0].resources }),
      ]),
    );
    expect(await screen.findByText("Releasing…")).toBeInTheDocument();
  });

  it("releases nothing when the confirmation is refused", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const sent = show();

    await userEvent.click(await screen.findByRole("button", { name: "Release" }));

    expect(sent.filter(([method]) => method !== "GET")).toEqual([]);
  });

  it("says a change is saving until the server has it, then saved", async () => {
    show();
    const picker = await screen.findByLabelText("Default project");
    let answer = () => {};
    const answered = new Promise<void>((resolve) => (answer = resolve));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        await answered;
        return { ok: true, status: 200, json: async () => PROFILE };
      }),
    );

    await userEvent.selectOptions(picker, "CHI-000002");

    expect(await screen.findByRole("status")).toHaveTextContent("Saving…");
    answer();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Saved"));
  });

  it("names the testbed its settings belong to", async () => {
    show({ ...PROFILE, testbed: "Other" });

    const name = await screen.findByRole("heading", { name: "alice" });
    expect(name.nextElementSibling).toHaveTextContent("Other");
  });

  it("sets the project new chats build in", async () => {
    const sent = show();

    await userEvent.selectOptions(await screen.findByLabelText("Default project"), "CHI-000002");

    await waitFor(() =>
      expect(sent).toContainEqual([
        "PATCH",
        "/api/profile",
        JSON.stringify({ default_project_id: "CHI-000002" }),
      ]),
    );
  });

  it("forgets a key without touching the sites once confirmed", async () => {
    const asked = vi.spyOn(window, "confirm").mockReturnValue(true);
    const sent = show();

    await userEvent.click(await screen.findByRole("button", { name: "Forget" }));

    expect(asked).toHaveBeenCalledWith("Forget the key laptop? Its keypairs stay at the sites.");
    await waitFor(() =>
      expect(sent).toContainEqual(["DELETE", "/api/keys/laptop?at_sites=false", undefined]),
    );
  });

  it("deletes nothing at the sites when the confirmation is refused", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const sent = show();

    await userEvent.click(await screen.findByRole("button", { name: "Delete at every Chameleon site" }));

    expect(sent.filter(([method]) => method !== "GET")).toEqual([]);
  });

  it("names each site a key could not be deleted at", async () => {
    const asked = vi.spyOn(window, "confirm").mockReturnValue(true);
    show();

    await userEvent.click(await screen.findByRole("button", { name: "Delete at every Chameleon site" }));

    expect(asked).toHaveBeenCalledWith("Delete the keypair cc-laptop-x7k2p9 at CHI@UC, KVM@TACC?");
    const report = await screen.findByRole("list", { name: "Deletion at each site" });
    expect(report).toHaveTextContent("CHI@UC: deleted");
    expect(report).toHaveTextContent(
      "KVM@TACC: failed. Delete cc-laptop-x7k2p9 there yourself: KVM@TACC is unreachable",
    );
  });

  it("sets the keypair new chats start with", async () => {
    const sent = show();

    await screen.findByRole("option", { name: "laptop (cc-laptop-x7k2p9)" });
    await userEvent.selectOptions(screen.getByLabelText("Imported key"), "cc-laptop-x7k2p9");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(sent).toContainEqual([
        "PATCH",
        "/api/profile",
        JSON.stringify({ default_ssh_key_ref: "cc-laptop-x7k2p9" }),
      ]),
    );
  });

  it("shows the default keypair's kind, chosen and marked, once a fresh profile names it", async () => {
    const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queries.setQueryData(["profile"], PROFILE);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string) => ({
        ok: true,
        status: 200,
        json: async () =>
          path.startsWith("/api/profile/keypairs")
            ? [{ name: "mac-key" }]
            : { ...PROFILE, default_site_keypairs: { "CHI@UC": "mac-key", "KVM@TACC": "mac-key" } },
      })),
    );
    render(
      <MemoryRouter>
        <QueryClientProvider client={queries}>
          <ProfilePage />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    const atSites = screen.getByLabelText(/A keypair I have at each site/);
    await waitFor(() => expect(atSites).toBeChecked());
    expect(within(atSites.closest("label")!).getByText("default")).toBeInTheDocument();
    expect(screen.getByText("One at each site")).toBeInTheDocument();
    expect(screen.getByLabelText("Keypair at CHI@UC")).toHaveValue("mac-key");
  });

  it("starts on the first option when there is no default keypair", async () => {
    show();

    expect(await screen.findByLabelText(/One of my imported keys/)).toBeChecked();
  });

  it("clears the keypair new chats start with", async () => {
    const sent = show({ ...PROFILE, default_ssh_key_ref: "cc-laptop-x7k2p9" });

    await userEvent.click(await screen.findByRole("button", { name: "Clear" }));

    await waitFor(() =>
      expect(sent).toContainEqual([
        "PATCH",
        "/api/profile",
        JSON.stringify({ default_ssh_key_ref: null, default_site_keypairs: null }),
      ]),
    );
  });
});
