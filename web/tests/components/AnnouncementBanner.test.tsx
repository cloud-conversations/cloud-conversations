import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnnouncementBanner } from "../../src/components/AnnouncementBanner";

function show() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ text: "maintenance at noon", posted_at: "2026-10-01T10:00:00Z" }),
    })),
  );
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <AnnouncementBanner />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the announcement banner", () => {
  it("shows the announcement until it is dismissed", async () => {
    show();

    expect(await screen.findByRole("status")).toHaveTextContent("maintenance at noon");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
