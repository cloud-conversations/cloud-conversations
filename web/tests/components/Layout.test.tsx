import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Layout } from "../../src/components/Layout";
import { browserStorage } from "../../src/storage";

type Answer = { ok: boolean; status: number; json: () => Promise<unknown> };

function serve(me: () => Promise<Answer>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) =>
      path === "/api/me" ? me() : { ok: true, status: 200, json: async () => [] },
    ),
  );
  return render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Layout>
          <p>the thread</p>
        </Layout>
      </QueryClientProvider>
    </MemoryRouter>,
  ).container;
}

function show(me: object = {}) {
  serve(async () => ({ ok: true, status: 200, json: async () => me }));
  return screen.findByRole("separator", { name: "Experiment list width" });
}

afterEach(() => {
  vi.unstubAllGlobals();
  browserStorage()?.clear();
});

describe("the layout", () => {
  it("puts the chat list back to its default width on a double-click", async () => {
    const handle = await show();
    const start = handle.getAttribute("aria-valuenow");
    handle.focus();
    await userEvent.keyboard("{ArrowRight>4/}");
    expect(handle).not.toHaveAttribute("aria-valuenow", start);

    await userEvent.dblClick(handle);

    expect(handle).toHaveAttribute("aria-valuenow", start);
  });

  it("will not drag the chat list past its bounds", async () => {
    const handle = await show();

    handle.focus();
    await userEvent.keyboard("{ArrowLeft>10/}");

    expect(handle).toHaveAttribute("aria-valuenow", handle.getAttribute("aria-valuemin"));
  });

  it("names who is logged in and their testbed, and links an admin to the admin page", async () => {
    show({ testbed: "Other", username: "alice", admin: true });

    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("Testbed:")).toHaveTextContent("Testbed: Other");
    expect(screen.getByRole("link", { name: "Admin" })).toHaveAttribute("href", "/admin");
    const logout = screen.getByRole("button", { name: "Log out" }).closest("form");
    expect(logout).toHaveAttribute("method", "post");
    expect(logout).toHaveAttribute("action", "/api/auth/logout");
  });

  it("shows someone who is not an admin no admin link", async () => {
    show({ testbed: "Chameleon", username: "bob", admin: false });

    expect(await screen.findByText("bob")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
  });

  it("draws nothing until it knows who is logged in", async () => {
    let answer = () => {};
    const page = serve(
      () =>
        new Promise((resolve) => {
          answer = () => resolve({ ok: true, status: 200, json: async () => ({}) });
        }),
    );

    expect(page).toBeEmptyDOMElement();
    answer();
    expect(await screen.findByText("the thread")).toBeInTheDocument();
  });

  it("draws nothing while it sends a logged-out browser to log in", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { pathname: "/", search: "", assign });
    const page = serve(async () => ({
      ok: false,
      status: 401,
      json: async () => ({ detail: "log in first" }),
    }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2F"));
    await expect(screen.findByRole("alert", {}, { timeout: 100 })).rejects.toThrow();
    expect(page).toBeEmptyDOMElement();
  });

  it("says why it could not find out who is logged in", async () => {
    serve(async () => ({ ok: false, status: 502, json: async () => ({ detail: "the api is down" }) }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /^That did not work: the api is down$/,
    );
    expect(screen.queryByText("the thread")).not.toBeInTheDocument();
  });
});
