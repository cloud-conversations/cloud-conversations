import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Layout } from "../../src/components/Layout";
import { browserStorage } from "../../src/storage";

function show(me: object = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => ({
      ok: true,
      status: 200,
      json: async () => (path === "/api/me" ? me : []),
    })),
  );
  render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Layout>
          <p>the thread</p>
        </Layout>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return screen.getByRole("separator", { name: "Experiment list width" });
}

afterEach(() => {
  vi.unstubAllGlobals();
  browserStorage()?.clear();
});

describe("the layout", () => {
  it("puts the chat list back to its default width on a double-click", async () => {
    const handle = show();
    const start = handle.getAttribute("aria-valuenow");
    handle.focus();
    await userEvent.keyboard("{ArrowRight>4/}");
    expect(handle).not.toHaveAttribute("aria-valuenow", start);

    await userEvent.dblClick(handle);

    expect(handle).toHaveAttribute("aria-valuenow", start);
  });

  it("will not drag the chat list past its bounds", async () => {
    const handle = show();

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
});
