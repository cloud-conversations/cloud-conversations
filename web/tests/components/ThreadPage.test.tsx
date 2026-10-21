import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThreadPage } from "../../src/pages/ThreadPage";
import type { Job, Message, Run, RunSummary, ThreadDetail, Version } from "../../src/api/types";

const FIRST = "d1111111-1111-1111-1111-111111111111";
const SECOND = "d2222222-2222-2222-2222-222222222222";

function version(descriptionId: string, flavor: string, site = "KVM@TACC"): Version {
  return {
    description_id: descriptionId,
    version: 1,
    requirements: {
      site,
      start_at: null,
      start_source: "default",
      duration_hours: 4,
      duration_source: "default",
      items: [{ key: "flavor", value: flavor, source: "default" }],
    },
    rubric: [{ id: "ssh", kind: "ssh_reachable", expected: true, comparator: "eq" }],
    unsupported: [],
    open_questions: [],
  };
}

function described(id: string, descriptionId: string, at: string): Message {
  return {
    message_id: id,
    role: "assistant",
    kind: "description",
    text: "a VM at KVM@TACC",
    payload: {},
    description_id: descriptionId,
    description_version: 1,
    created_at: at,
  };
}

const RUN: Run = {
  description_version: 1,
  state: "passed",
  resume_count: 0,
  results: null,
  connection: null,
  failure: null,
  live_resources: [{ kind: "lease", provider_id: "lease-1", site: "KVM@TACC" }],
};

function detail(over: Partial<ThreadDetail> = {}): ThreadDetail {
  return {
    thread: { thread_id: "t1",
      title: "a vm",
      ssh_key_ref: "cc-keypair",
      site_keypairs: null,
      project_id: null,
    },
    messages: [],
    versions: [],
    jobs: [],
    runs: [],
    decisions: {},
    feedback: [],
    ...over,
  };
}

function serve(thread: ThreadDetail, answers: Record<string, unknown> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      const body =
        answers[path] ??
        (path === "/api/threads/t1"
          ? thread
          : path.endsWith("/events") || path.startsWith("/api/profile/keypairs")
            ? []
            : path.startsWith("/api/runs/")
              ? RUN
              : {});
      return { ok: true, status: 200, json: async () => body };
    }),
  );
  return render(
    <MemoryRouter initialEntries={["/threads/t1"]}>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Routes>
          <Route path="/threads/:threadId" element={<ThreadPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("a chat", () => {
  it("gives every experiment on it its own card", async () => {
    serve(
      detail({
        messages: [
          described("m1", FIRST, "2026-09-20T10:00:00Z"),
          described("m2", SECOND, "2026-09-20T11:00:00Z"),
        ],
        versions: [version(FIRST, "m1.large"), version(SECOND, "m1.medium")],
      }),
    );

    expect(
      await screen.findByText("Experiment 1, version", { exact: false }),
    ).toBeInTheDocument();
    expect(screen.getByText("Experiment 2, version", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("m1.large")).toBeInTheDocument();
    expect(screen.getByText("m1.medium")).toBeInTheDocument();
  });

  it("offers the keypair at the site of the latest description", async () => {
    serve(
      detail({
        thread: {
          thread_id: "t1",
          title: "a vm",
          ssh_key_ref: null,
          site_keypairs: { "KVM@TACC": "kvm-key", "CHI@UC": "uc-key" },
          project_id: null,
        },
        messages: [
          described("m1", FIRST, "2026-09-20T10:00:00Z"),
          described("m2", SECOND, "2026-09-20T11:00:00Z"),
        ],
        versions: [version(FIRST, "m1.large"), version(SECOND, "m1.medium", "CHI@UC")],
      }),
    );

    expect(await screen.findByLabelText("Keypair at CHI@UC")).toHaveValue("uc-key");
  });

  it("says a version is being released only on the run it is releasing", async () => {
    const runs: RunSummary[] = [
      { run_id: "r1", created_at: "2026-09-20T10:00:00Z" },
      { run_id: "r2", created_at: "2026-09-20T11:00:00Z" },
    ];
    const jobs: Job[] = [
      { job_id: "j1", kind: "teardown", route: null, run_id: "r2", waiting: false },
    ];
    serve(detail({ runs, jobs }));

    expect(
      await screen.findByRole("button", { name: "Release resources" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Releasing…" })).toHaveLength(1);
  });

  const PROPOSED: Message = {
    ...described("m2", FIRST, "2026-09-20T10:01:00Z"),
    kind: "release",
    text: "The experiment agent wants to release lease lease-1 at KVM@TACC. It says: nothing fits",
    payload: { request_id: "q1", version: null },
  };

  it("releases what the agent proposed only once the user says so", async () => {
    serve(detail({ messages: [PROPOSED] }));

    expect(await screen.findByText(PROPOSED.text)).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalledWith("/api/releases/q1/confirm", expect.anything());
    await userEvent.click(screen.getByRole("button", { name: "Release" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/releases/q1/confirm",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("keeps what the agent proposed releasing", async () => {
    serve(detail({ messages: [PROPOSED] }));

    await userEvent.click(await screen.findByRole("button", { name: "Keep" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/releases/q1/decline",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(fetch).not.toHaveBeenCalledWith("/api/releases/q1/confirm", expect.anything());
  });

  it("names the version a proposed release goes on to build", async () => {
    serve(detail({ messages: [{ ...PROPOSED, payload: { request_id: "q1", version: 2 } }] }));

    expect(
      await screen.findByRole("button", { name: "Release and build version 2" }),
    ).toBeInTheDocument();
  });

  it("shows the user's answer in place of the buttons", async () => {
    serve(detail({ messages: [PROPOSED], decisions: { q1: "declined" } }));

    expect(await screen.findByText("You kept them.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Release" })).toBeNull();
  });

  it("says a queued build is waiting for another build", async () => {
    const jobs: Job[] = [{ job_id: "j1", kind: "build", route: null, run_id: null, waiting: true }];
    serve(detail({ jobs }));

    expect(await screen.findByText("Waiting for another build to finish…")).toBeInTheDocument();
  });

  it("approves the version again once a keypair is chosen for it", async () => {
    const asked: Message = {
      ...described("m2", FIRST, "2026-09-20T10:01:00Z"),
      kind: "notice",
      text: "Before I can build this I need the key you will log in with at KVM@TACC.",
      payload: { needs: "ssh_key_ref" },
    };
    serve(
      detail({
        thread: { thread_id: "t1", title: "a vm", ssh_key_ref: null, site_keypairs: null, project_id: null },
        messages: [described("m1", FIRST, "2026-09-20T10:00:00Z"), asked],
        versions: [version(FIRST, "m1.large")],
      }),
      {
        "/api/profile": {
          username: "alice",
          projects: [],
          default_project_id: null,
          keys: [{ name: "laptop", keypair_name: "cc-laptop-x7k2p9", fingerprint: "SHA256:a" }],
          sites: ["KVM@TACC"],
        },
      },
    );

    await screen.findByRole("option", { name: "laptop (cc-laptop-x7k2p9)" });
    await userEvent.selectOptions(screen.getByLabelText("Imported key"), "cc-laptop-x7k2p9");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/threads/t1/builds",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ description_id: FIRST, version: 1 }),
        }),
      ),
    );
  });

  it("resends the last message once a keypair is chosen for a notice with no description", async () => {
    const asked: Message = {
      ...described("m1", FIRST, "2026-09-20T10:00:00Z"),
      role: "user",
      kind: "user",
      text: "what do I have running?",
      description_id: null,
      description_version: null,
    };
    const notice: Message = {
      ...asked,
      message_id: "m2",
      role: "assistant",
      kind: "notice",
      text: "I need the key you will log in with.",
      payload: { needs: "ssh_key_ref" },
      created_at: "2026-09-20T10:01:00Z",
    };
    serve(
      detail({
        thread: { thread_id: "t1", title: "a vm", ssh_key_ref: null, site_keypairs: null, project_id: null },
        messages: [asked, notice],
      }),
      { "/api/profile": { keys: [{ name: "laptop", keypair_name: "cc-laptop-x7k2p9", fingerprint: "SHA256:a" }] } },
    );

    await screen.findByRole("option", { name: "laptop (cc-laptop-x7k2p9)" });
    await userEvent.selectOptions(screen.getByLabelText("Imported key"), "cc-laptop-x7k2p9");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/threads/t1/messages",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ text: "what do I have running?" }),
        }),
      ),
    );
  });

  it("sends a comment with the rating it follows", async () => {
    const answered: Message = {
      ...described("m1", FIRST, "2026-09-20T10:00:00Z"),
      kind: "answer",
      text: "Use a lease.",
      description_id: null,
      description_version: null,
    };
    serve(detail({ messages: [answered] }));

    await userEvent.click(await screen.findByRole("button", { name: "Not helpful" }));
    await userEvent.type(screen.getByLabelText("What could be better?"), "wrong site{Enter}");

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/threads/t1/feedback",
        expect.objectContaining({
          body: JSON.stringify({ message_id: "m1", rating: "down", comment: "wrong site" }),
        }),
      ),
    );
  });

  it("scrolls to the newest message when the chat is read", async () => {
    vi.spyOn(Element.prototype, "scrollHeight", "get").mockReturnValue(900);
    const top = vi.spyOn(Element.prototype, "scrollTop", "set");
    serve(detail({ messages: [described("m1", FIRST, "2026-09-20T10:00:00Z")], versions: [version(FIRST, "m1.large")] }));

    await screen.findByText("m1.large");

    expect(top).toHaveBeenCalledWith(900);
  });

  it("keeps the newest message in view as the chat grows", async () => {
    let grew: () => void = () => {};
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(report: ResizeObserverCallback) {
          grew = () => report([], this as unknown as ResizeObserver);
        }
        observe() {}
        disconnect() {}
      },
    );
    const height = vi.spyOn(Element.prototype, "scrollHeight", "get").mockReturnValue(900);
    const top = vi.spyOn(Element.prototype, "scrollTop", "set");
    serve(detail({ messages: [described("m1", FIRST, "2026-09-20T10:00:00Z")], versions: [version(FIRST, "m1.large")] }));
    await screen.findByText("m1.large");

    height.mockReturnValue(1400);
    grew();

    expect(top).toHaveBeenLastCalledWith(1400);
  });
});
