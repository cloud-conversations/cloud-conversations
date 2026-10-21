import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunCard } from "../../src/components/RunCard";
import { RUN_POLL_MS } from "../../src/hooks/useRun";
import type { Event, Run } from "../../src/api/types";

const PASSED: Run = {
  description_version: 2,
  state: "passed",
  resume_count: 0,
  results: [
    { check_id: "ssh", kind: "ssh_reachable", expected: true, actual: true, passed: true },
    { check_id: "cpus", kind: "cpu_count", expected: 24, actual: 12, passed: false },
  ],
  connection: { host: "129.0.0.9", user: "cc", port: 22, expires_at: "2026-09-16T10:00:00Z" },
  failure: null,
  live_resources: [
    { kind: "lease", provider_id: "lease-1", site: "CHI@UC" },
  ],
};

const EVENTS: Event[] = [
  { seq: 1, at: "2026-09-15T10:00:01Z", kind: "run_started", payload: {} },
  {
    seq: 2,
    at: "2026-09-15T10:02:03Z",
    kind: "step_succeeded",
    payload: { step_id: "step-0001", action: "reserve" },
  },
  {
    seq: 3,
    at: "2026-09-15T10:05:00Z",
    kind: "model_turn",
    payload: { round: 1, action: "retry" },
  },
];

function serve(
  run: Run, events: Event[] = EVENTS, releasing = false, building = false, waiting = false,
) {
  const posted: [string, unknown][] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") posted.push([path, init.body]);
      const body = path.endsWith("/events") ? events : run;
      return { ok: true, status: 200, json: async () => body };
    }),
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RunCard
        runId="r1" threadId="t1" releasing={releasing} waiting={waiting} building={building}
      />
    </QueryClientProvider>,
  );
  return posted;
}

function reads() {
  return (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.filter(
    (c) => c[0] === "/api/runs/r1",
  ).length;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("a run card", () => {
  it("shows the endpoint to log in with", async () => {
    serve(PASSED);

    expect(await screen.findByText("ssh cc@129.0.0.9")).toBeInTheDocument();
    expect(
      screen.getByText("the lease ends Sep 16, 05:00 AM CDT"),
    ).toBeInTheDocument();
  });

  it("shows every rubric check with what was measured", async () => {
    serve(PASSED);

    expect(await screen.findByText("PASS")).toBeInTheDocument();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(screen.getByText("wanted 24, got 12")).toBeInTheDocument();
  });

  it("reads the event stream back as a timeline", async () => {
    serve(PASSED);

    expect(await screen.findByText("started")).toBeInTheDocument();
    expect(screen.getByText("reserve done")).toBeInTheDocument();
    expect(
      screen.getByText("the experiment agent chose to retry"),
    ).toBeInTheDocument();
  });

  it("names each resource made and released", async () => {
    serve(PASSED, [
      { seq: 1, at: "2026-09-15T10:00:01Z", kind: "resource_created",
        payload: { kind: "lease", provider_id: "lease-1" } },
      { seq: 2, at: "2026-09-15T10:09:00Z", kind: "resource_released",
        payload: { kind: "floating_ip", provider_id: "129.0.0.9" } },
    ]);

    expect(await screen.findByText("reserved lease lease-1")).toBeInTheDocument();
    expect(screen.getByText("released floating IP 129.0.0.9")).toBeInTheDocument();
  });

  it("stamps each event with the reader's own clock", async () => {
    serve(PASSED);

    expect(await screen.findByText("05:00:01 AM")).toBeInTheDocument();
    expect(screen.queryByText("10:00:01 AM")).toBeNull();
  });

  it("names what a settled run still holds, and releases it once confirmed", async () => {
    const asked = vi.spyOn(window, "confirm").mockReturnValue(true);
    const posted = serve(PASSED);

    const release = await screen.findByRole("button", { name: "Release resources" });
    expect(screen.getByText("lease lease-1 at CHI@UC")).toBeInTheDocument();
    await userEvent.click(release);

    expect(asked).toHaveBeenCalledWith(
      "Release lease lease-1 at CHI@UC? This deletes them at the testbed.",
    );
    expect(posted).toEqual([
      ["/api/runs/r1/teardown", JSON.stringify({ resources: PASSED.live_resources })],
    ]);
  });

  it("releases nothing when the confirmation is refused", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const posted = serve(PASSED);

    await userEvent.click(await screen.findByRole("button", { name: "Release resources" }));

    expect(posted).toEqual([]);
  });

  it("says so when releasing does not go through", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          return {
            ok: false,
            status: 409,
            json: async () => ({ detail: "a teardown is already running" }),
          };
        }
        return {
          ok: true,
          status: 200,
          json: async () => (path.endsWith("/events") ? EVENTS : PASSED),
        };
      }),
    );
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <RunCard runId="r1" threadId="t1" releasing={false} waiting={false} building={false} />
      </QueryClientProvider>,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Release resources" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That did not work: a teardown is already running",
    );
  });

  it("says it is releasing while a teardown is running", async () => {
    serve(PASSED, EVENTS, true);

    const button = await screen.findByRole("button", { name: "Releasing…" });
    expect(button).toBeDisabled();
    expect(screen.getByText("Releasing resources…")).toBeInTheDocument();
  });

  it("says a queued release is waiting for another build", async () => {
    serve(PASSED, EVENTS, true, false, true);

    expect(await screen.findByText("Waiting for another build to finish…")).toBeInTheDocument();
  });

  it("keeps reading the run while a teardown is running", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serve(PASSED, EVENTS, true);
    await screen.findByRole("button", { name: "Releasing…" });
    const before = reads();

    await vi.advanceTimersByTimeAsync(RUN_POLL_MS);

    expect(reads()).toBeGreaterThan(before);
  });

  it("keeps reading a failed run while its build is still running", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serve({ ...PASSED, state: "failed" }, EVENTS, false, true);
    await waitFor(() => expect(reads()).toBeGreaterThan(0));
    const before = reads();

    await vi.advanceTimersByTimeAsync(RUN_POLL_MS);

    expect(reads()).toBeGreaterThan(before);
  });

  it("re-reads the run once the worker finishes the teardown", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string) => ({
        ok: true,
        status: 200,
        json: async () => (path.endsWith("/events") ? EVENTS : PASSED),
      })),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const card = (releasing: boolean) => (
      <QueryClientProvider client={client}>
        <RunCard runId="r1" threadId="t1" releasing={releasing} waiting={false} building={false} />
      </QueryClientProvider>
    );
    const { rerender } = render(card(true));
    await screen.findByRole("button", { name: "Releasing…" });
    const before = reads();

    rerender(card(false));

    await waitFor(() => expect(reads()).toBeGreaterThan(before));
  });

  it("says a settled run with nothing left needs no releasing", async () => {
    serve({ ...PASSED, live_resources: [] });

    expect(await screen.findByText("Resources released.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Release resources" })).toBeNull();
  });

  it("reports why a failed run failed", async () => {
    serve({
      ...PASSED,
      state: "failed",
      results: null,
      connection: null,
      failure: { kind: "plan", message: "no hosts free", step_id: "step-0001" },
    });

    expect(await screen.findByText(/plan: no hosts free \(at step-0001\)/)).toBeInTheDocument();
  });

  it("says it is still working while the run has not settled", async () => {
    serve({ ...PASSED, state: "provisioning" });

    expect(await screen.findByText("working…")).toBeInTheDocument();
  });
});
