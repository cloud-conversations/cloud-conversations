import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThreadSidebar } from "../../src/components/ThreadSidebar";

const THREADS = [
  { thread_id: "t1", title: "an A100 for a day", ssh_key_ref: null, created_at: "", updated_at: "" },
];

function Where() {
  return <p data-testid="at">{useLocation().pathname}</p>;
}

function serve(status = 204, detail = "", at = "/") {
  const calls: [string, string][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      calls.push([init?.method ?? "GET", path]);
      if (init?.method === "DELETE") {
        return { ok: status === 204, status, json: async () => ({ detail }) };
      }
      return { ok: true, status: 200, json: async () => THREADS };
    }),
  );
  const sidebar = (
    <>
      <ThreadSidebar width={256} />
      <Where />
    </>
  );
  render(
    <MemoryRouter initialEntries={[at]}>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Routes>
          <Route path="*" element={sidebar} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the chat sidebar", () => {
  it("marks a chat running a build and one still reserving hardware, and no other", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => [
          { ...THREADS[0], thread_id: "t1", title: "building", activity: "running" },
          { ...THREADS[0], thread_id: "t2", title: "reserving", activity: "reserved" },
          { ...THREADS[0], thread_id: "t3", title: "idle", activity: null },
        ],
      })),
    );
    render(
      <MemoryRouter>
        <QueryClientProvider
          client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
        >
          <ThreadSidebar width={256} />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    const dot = async (title: string) =>
      (await screen.findByRole("link", { name: title })).closest("div")!.querySelector("span[title]");
    expect((await dot("building"))?.getAttribute("title")).toMatch(/running/);
    expect((await dot("reserving"))?.getAttribute("title")).toMatch(/reserved/);
    expect(await dot("idle")).toBeNull();
  });

  it("shades the open chat grey", async () => {
    serve(204, "", "/threads/t1");

    const open = await screen.findByRole("link", { name: "an A100 for a day" });

    expect(open).toHaveAttribute("aria-current", "page");
    expect(open.parentElement).toHaveClass("bg-grey-light");
  });

  it("deletes a chat once the confirmation is accepted", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = serve();

    await userEvent.click(
      await screen.findByRole("button", { name: "Delete an A100 for a day" }),
    );

    expect(calls).toContainEqual(["DELETE", "/api/threads/t1"]);
  });

  it("goes home after deleting the open chat", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    serve(204, "", "/threads/t1");

    await userEvent.click(
      await screen.findByRole("button", { name: "Delete an A100 for a day" }),
    );

    await waitFor(() => expect(screen.getByTestId("at")).toHaveTextContent(/^\/$/));
  });

  it("deletes nothing when the confirmation is refused", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const calls = serve();

    await userEvent.click(
      await screen.findByRole("button", { name: "Delete an A100 for a day" }),
    );

    expect(calls.some(([method]) => method === "DELETE")).toBe(false);
  });

  it("says why a chat could not be deleted", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    serve(409, "Thread t1 is running a exchange");

    await userEvent.click(
      await screen.findByRole("button", { name: "Delete an A100 for a day" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That did not work: Thread t1 is running a exchange",
    );
  });
});
