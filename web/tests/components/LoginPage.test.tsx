import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LoginPage } from "../../src/pages/LoginPage";

function show(local: boolean, entry = "/login?next=%2Fthreads%2Ft1") {
  const sent: [string, string, string | undefined][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      sent.push([init?.method ?? "GET", path, init?.body as string | undefined]);
      if (path === "/api/auth/local") return { ok: true, status: 204, json: async () => ({}) };
      return {
        ok: true,
        status: 200,
        json: async () => ({ local, logins: [{ testbed: "chameleon", label: "Chameleon" }] }),
      };
    }),
  );
  render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <LoginPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return sent;
}

afterEach(() => vi.unstubAllGlobals());

describe("the login page", () => {
  it("sends the user to each testbed's login, to come back where they were", async () => {
    show(false);

    expect(await screen.findByRole("link", { name: "Log in with Chameleon" })).toHaveAttribute(
      "href",
      "/api/auth/login?testbed=chameleon&next=%2Fthreads%2Ft1",
    );
  });

  it("never sends the user back to another site", async () => {
    show(false, "/login?next=%2F%2Fevil.example.org");

    expect(await screen.findByRole("link", { name: "Log in with Chameleon" })).toHaveAttribute(
      "href",
      "/api/auth/login?testbed=chameleon&next=%2F",
    );
  });

  it("offers no password form unless local test users may log in", async () => {
    show(false);

    await screen.findByRole("link", { name: "Log in with Chameleon" });
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });

  it("logs a local test user in and goes where they were", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    const sent = show(true);

    await userEvent.type(await screen.findByLabelText("Name"), "alice");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Log in" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/threads/t1"));
    expect(sent).toContainEqual([
      "POST",
      "/api/auth/local",
      JSON.stringify({ name: "alice", password: "correct horse battery" }),
    ]);
  });

  it("never sends a local test user to another site", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    show(true, "/login?next=%2F%5Cevil.example");

    await userEvent.type(await screen.findByLabelText("Name"), "alice");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Log in" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
  });
});
