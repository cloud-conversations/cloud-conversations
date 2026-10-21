import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TrashPage } from "../../src/pages/TrashPage";

const TRASH = [
  {
    thread_id: "t1",
    title: "an A100 for a day",
    deleted_at: "2026-10-01T12:00:00Z",
    restorable_until: "2026-10-31T12:00:00Z",
  },
];

function serve() {
  const calls: [string, string][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      calls.push([init?.method ?? "GET", path]);
      return { ok: true, status: 200, json: async () => (path === "/api/trash" ? TRASH : {}) };
    }),
  );
  render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <TrashPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("the trash", () => {
  it("restores a chat", async () => {
    const calls = serve();

    expect(await screen.findByText("an A100 for a day")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Restore" }));

    await waitFor(() => expect(calls).toContainEqual(["POST", "/api/trash/t1/restore"]));
  });
});
