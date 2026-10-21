import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WaitingBanner } from "../../src/components/WaitingBanner";

function show(waiting: number) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        testbed: "Chameleon",
        username: "alice",
        admin: true,
        waiting_accounts: waiting,
      }),
    })),
  );
  render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <WaitingBanner />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the waiting-accounts banner", () => {
  it("tells an admin how many accounts wait, linked to the admin page", async () => {
    show(2);

    expect(await screen.findByRole("status")).toHaveTextContent(
      "2 accounts are waiting for approval.",
    );
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute("href", "/admin");
  });

  it("shows nothing when no account waits", async () => {
    show(0);

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
