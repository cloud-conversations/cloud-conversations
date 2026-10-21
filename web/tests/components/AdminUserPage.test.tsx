import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AdminUserPage } from "../../src/pages/AdminUserPage";
import type { Quota } from "../../src/api/types";

const BOB = {
  user_id: "chameleon:bob",
  testbed: "Chameleon",
  username: "bob",
  projects: [{ id: "CHI-000001" }],
  local: false,
  first_login_at: null,
  last_login_at: null,
  checked_at: null,
  disabled_at: null,
  disabled_reason: null,
  open_sessions: 2,
  quotas: [
    { name: "max_builds_per_user", used: 1, limit: 2, default: 2, override: null },
  ] as Quota[],
};

function show(user = BOB) {
  const calls: [string, string, string | undefined][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      calls.push([init?.method ?? "GET", path, init?.body as string | undefined]);
      return { ok: true, status: 200, json: async () => user };
    }),
  );
  render(
    <MemoryRouter initialEntries={["/admin/users/chameleon%3Abob"]}>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Routes>
          <Route path="/admin/users/:userId" element={<AdminUserPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("a user's admin panel", () => {
  it("disables the user with the reason given", async () => {
    const calls = show();

    await userEvent.type(await screen.findByLabelText("Reason"), "a runaway script");
    await userEvent.click(screen.getByRole("button", { name: "Disable" }));

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/admin/users/chameleon%3Abob",
        JSON.stringify({ disabled: true, reason: "a runaway script" }),
      ]),
    );
  });

  it("overrides a limit", async () => {
    const calls = show();

    await userEvent.type(await screen.findByLabelText("Builds running override"), "5");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/admin/users/chameleon%3Abob",
        JSON.stringify({ max_builds_per_user: 5 }),
      ]),
    );
  });

  it("clears an override left empty", async () => {
    const calls = show({ ...BOB, quotas: [{ ...BOB.quotas[0], override: 5 }] });

    const override = await screen.findByLabelText("Builds running override");
    expect(override).toHaveValue(5);
    await userEvent.clear(override);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/admin/users/chameleon%3Abob",
        JSON.stringify({ max_builds_per_user: null }),
      ]),
    );
  });

  it("ends every session", async () => {
    const calls = show();

    await userEvent.click(await screen.findByRole("button", { name: "End all" }));

    await waitFor(() =>
      expect(calls).toContainEqual([
        "DELETE",
        "/api/admin/users/chameleon%3Abob/sessions",
        undefined,
      ]),
    );
  });
});
