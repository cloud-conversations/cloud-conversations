import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AdminPage } from "../../src/pages/AdminPage";

const KEY = {
  fingerprint: "SHA256:current",
  created_at: "2026-10-01T10:00:00Z",
  retired_at: null,
  authorized_key: "ssh-rsa AAAA cloud-conversations-system",
};

const USER = {
  user_id: "chameleon:bob",
  testbed: "Chameleon",
  username: "bob",
  first_login_at: null,
  last_login_at: null,
  open_sessions: 2,
  chats: 3,
  latest_chat_at: null,
  disabled_at: "2026-10-01T10:00:00Z",
  overridden: true,
  admin: false,
  approval: "approved",
};

const ALICE = {
  ...USER,
  user_id: "chameleon:alice",
  username: "alice",
  disabled_at: null,
  overridden: false,
  admin: true,
};

const CAROL = {
  ...USER,
  user_id: "chameleon:carol",
  username: "carol",
  disabled_at: null,
  overridden: false,
  approval: "waiting",
};

const FEEDBACK = {
  feedback_id: "f1111111-1111-1111-1111-111111111111",
  updated_at: "2026-10-01T10:00:00Z",
  username: "bob",
  email: "bob@example.org",
  thread_title: "a vm",
  rating: "down",
  comment: "wrong site",
  message_kind: "answer",
  message_text: "Use a lease.",
  run_state: null,
  run_failure: null,
};

const ANSWERS: Record<string, unknown> = {
  "/api/admin/feedback?page=1": { feedback: [FEEDBACK], more: true, size: 5, sizes: [5, 25] },
  "/api/admin/feedback?page=2": { feedback: [], more: false, size: 5, sizes: [5, 25] },
  "/api/admin/feedback?page=1&size=25": { feedback: [], more: false, size: 25, sizes: [5, 25] },
  "/api/admin/users?page=1": {
    users: [CAROL, ALICE, USER],
    more: true,
    size: 25,
    sizes: [25, 100],
  },
  "/api/admin/users?page=2": { users: [], more: false, size: 25, sizes: [25, 100] },
  "/api/admin/settings": {
    require_approval: true,
    limits: [{ name: "max_builds_per_user", default: 2, override: 5 }],
  },
  "/api/admin/system-keys": [KEY],
  "/api/announcement": null,
};

function show() {
  const calls: [string, string, string | undefined][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      calls.push([init?.method ?? "GET", path, init?.body as string | undefined]);
      return {
        ok: true,
        status: 200,
        json: async () => (init?.method === "POST" ? KEY : init?.method ? {} : ANSWERS[path]),
      };
    }),
  );
  render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <AdminPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("the admin page", () => {
  it("lists each system key with its authorized_keys line", async () => {
    show();

    expect(await screen.findByText("SHA256:current")).toBeInTheDocument();
    expect(screen.getByText("current")).toBeInTheDocument();
    expect(screen.getByText("ssh-rsa AAAA cloud-conversations-system")).toBeInTheDocument();
  });

  it("rotates the key when asked", async () => {
    const calls = show();
    await screen.findByText("SHA256:current");

    await userEvent.click(screen.getByRole("button", { name: "Rotate" }));

    await waitFor(() =>
      expect(calls).toContainEqual(["POST", "/api/admin/system-keys/rotate", "{}"]),
    );
  });

  it("posts an announcement", async () => {
    const calls = show();

    await userEvent.type(await screen.findByLabelText("Announcement"), "maintenance at noon");
    await userEvent.click(screen.getByRole("button", { name: "Post" }));

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PUT",
        "/api/admin/announcement",
        JSON.stringify({ text: "maintenance at noon" }),
      ]),
    );
  });

  it("lists each user's status, logins and chat count, linked to their panel", async () => {
    show();

    const name = await screen.findByRole("link", { name: "bob" });
    expect(name).toHaveAttribute("href", "/admin/users/chameleon%3Abob");
    const row = name.closest("tr") as HTMLElement;
    expect([...row.querySelectorAll("td")].map((cell) => cell.textContent)).toEqual([
      "Chameleon",
      "bob",
      "disabled, limits overridden",
      "never",
      "never",
      "2",
      "3",
      "none",
      "",
    ]);
  });

  it("marks an admin in their status", async () => {
    show();

    const row = (await screen.findByRole("link", { name: "alice" })).closest("tr") as HTMLElement;
    expect(row.querySelectorAll("td")[2]).toHaveTextContent(/^enabled, admin$/);
  });

  it("approves a waiting user from their row", async () => {
    const calls = show();

    const row = (await screen.findByRole("link", { name: "carol" })).closest("tr") as HTMLElement;
    expect(row.querySelectorAll("td")[2]).toHaveTextContent("waiting");
    await userEvent.click(within(row).getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/admin/users/chameleon%3Acarol",
        JSON.stringify({ approval: "approved" }),
      ]),
    );
  });

  it("offers no approval to a user who is already approved", async () => {
    show();

    const row = (await screen.findByRole("link", { name: "bob" })).closest("tr") as HTMLElement;
    expect(within(row).queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("turns approval of new accounts off", async () => {
    const calls = show();

    const box = await screen.findByLabelText("Require approval for new accounts");
    await waitFor(() => expect(box).toBeChecked());
    await userEvent.click(box);

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/admin/settings",
        JSON.stringify({ require_approval: false }),
      ]),
    );
  });

  it("sets a limit for everyone", async () => {
    const calls = show();

    const box = await screen.findByLabelText("Builds running for everyone");
    await waitFor(() => expect(box).toHaveValue(5));
    const row = box.closest("tr") as HTMLElement;
    expect(row.querySelectorAll("td")[1]).toHaveTextContent(/^2$/);
    await userEvent.clear(box);
    await userEvent.type(box, "7");
    await userEvent.click(within(row).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/admin/settings",
        JSON.stringify({ max_builds_per_user: 7 }),
      ]),
    );
  });

  it("clears a limit for everyone left empty", async () => {
    const calls = show();

    const box = await screen.findByLabelText("Builds running for everyone");
    await waitFor(() => expect(box).toHaveValue(5));
    await userEvent.clear(box);
    const row = box.closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/admin/settings",
        JSON.stringify({ max_builds_per_user: null }),
      ]),
    );
  });

  it("asks for the next page of users", async () => {
    const calls = show();

    await screen.findByRole("link", { name: "carol" });
    await userEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() =>
      expect(calls).toContainEqual(["GET", "/api/admin/users?page=2", undefined]),
    );
  });

  it("lists each piece of feedback with who sent it and what it was about", async () => {
    show();

    const row = (await screen.findByText("wrong site")).closest("tr") as HTMLElement;
    expect([...row.querySelectorAll("td")].slice(1).map((cell) => cell.textContent)).toEqual([
      "bob (bob@example.org)",
      "👎",
      "wrong site",
      "a vm — answer: Use a lease.",
      FEEDBACK.feedback_id,
    ]);
    expect(screen.getByRole("link", { name: FEEDBACK.feedback_id })).toHaveAttribute(
      "href",
      `/admin/feedback/${FEEDBACK.feedback_id}`,
    );
  });

  it("asks for the next page of older feedback", async () => {
    const calls = show();

    await screen.findByText("wrong site");
    await userEvent.click(screen.getByRole("button", { name: "Older" }));

    await screen.findByText("Page 2");
    expect(calls).toContainEqual(["GET", "/api/admin/feedback?page=2", undefined]);
    expect(screen.getByRole("button", { name: "Older" })).toBeDisabled();
  });

  it("asks for the number per page chosen", async () => {
    const calls = show();

    await screen.findByText("wrong site");
    await userEvent.selectOptions(screen.getByLabelText("Per page"), "25");

    await waitFor(() =>
      expect(calls).toContainEqual(["GET", "/api/admin/feedback?page=1&size=25", undefined]),
    );
  });
});
