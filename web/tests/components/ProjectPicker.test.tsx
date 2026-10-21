import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectPicker } from "../../src/components/ProjectPicker";

const ME = {
  username: "alice",
  projects: [{ id: "CHI-000001", nickname: "mine" }, { id: "CHI-000002" }],
  admin: false,
};

function show() {
  const calls: [string, string, string | undefined][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      calls.push([init?.method ?? "GET", path, init?.body as string | undefined]);
      return { ok: true, status: 200, json: async () => (path === "/api/me" ? ME : {}) };
    }),
  );
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ProjectPicker threadId="t1" project="CHI-000001" />
    </QueryClientProvider>,
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("the project picker", () => {
  it("offers each of the user's projects, showing the chat's", async () => {
    show();

    expect(await screen.findByRole("option", { name: "mine (CHI-000001)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "CHI-000002" })).toBeInTheDocument();
    expect(screen.getByLabelText("Project")).toHaveValue("CHI-000001");
  });

  it("moves the chat to the project picked", async () => {
    const calls = show();
    await screen.findByRole("option", { name: "CHI-000002" });

    await userEvent.selectOptions(screen.getByLabelText("Project"), "CHI-000002");

    await waitFor(() =>
      expect(calls).toContainEqual([
        "PATCH",
        "/api/threads/t1",
        JSON.stringify({ project_id: "CHI-000002" }),
      ]),
    );
  });
});
