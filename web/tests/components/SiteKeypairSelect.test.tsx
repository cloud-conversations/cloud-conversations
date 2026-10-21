import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SiteKeypairSelect } from "../../src/components/SiteKeypairSelect";

function show(answer: () => Promise<unknown>) {
  const asked = vi.fn(answer);
  vi.stubGlobal("fetch", asked);
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <SiteKeypairSelect site="CHI@UC" value="" onChange={() => {}} label="Keypair at CHI@UC" />
    </QueryClientProvider>,
  );
  return asked;
}

afterEach(() => vi.unstubAllGlobals());

describe("a choice of keypairs at a site", () => {
  it("says it is loading them, and waits to be used", async () => {
    show(() => new Promise(() => {}));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Loading your keypairs at CHI@UC…",
    );
    expect(screen.getByLabelText("Keypair at CHI@UC")).toBeDisabled();
  });

  it("says why they did not load, and loads them again when asked", async () => {
    const asked = show(async () => ({
      ok: false,
      status: 502,
      json: async () => ({ detail: "CHI@UC is unreachable" }),
    }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't load your keypairs at CHI@UC: CHI@UC is unreachable",
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(asked).toHaveBeenCalledTimes(2));
  });

  it("says when the user holds none there", async () => {
    show(async () => ({ ok: true, status: 200, json: async () => [] }));

    expect(await screen.findByText("You have no keypairs at CHI@UC.")).toBeInTheDocument();
  });
});
