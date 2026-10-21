import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AdminFeedbackPage } from "../../src/pages/AdminFeedbackPage";

const ROUND = {
  round: 1,
  action: "answer",
  text: "Create a lease first.",
  reasons: [],
  sent: [
    { role: "system", content: "Answer from the documentation." },
    { role: "user", content: "Question: how do I reserve a node?" },
  ],
  model: "llama-70b",
  served_model: "llama-70b-0614",
  temperature: 0,
  bundle_hash: "a784a05c",
};

const DETAIL = {
  feedback_id: "f1111111-1111-1111-1111-111111111111",
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:00:00Z",
  owner_id: "chameleon:bob",
  username: "bob",
  email: null,
  thread_id: "t1",
  thread_title: "a vm",
  project_id: null,
  rating: "down",
  comment: "wrong site",
  message_id: "m1",
  message_kind: "answer",
  message_text: "Create a lease first.",
  message_payload: {},
  conversation_id: "c1",
  prompt: "how do I reserve a node?",
  transcript: [ROUND],
  routing_conversation_id: null,
  routing_transcript: null,
  job_id: null,
  job_error: null,
  description_id: null,
  description_version: null,
  requirements: null,
  rubric: null,
  run_id: null,
  run_state: null,
  run_failure: null,
  build_turns: [],
};

afterEach(() => vi.unstubAllGlobals());

describe("a piece of feedback's admin page", () => {
  it("shows what the model was sent each round, and what it replied", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => DETAIL })),
    );
    render(
      <MemoryRouter initialEntries={[`/admin/feedback/${DETAIL.feedback_id}`]}>
        <QueryClientProvider
          client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
        >
          <Routes>
            <Route path="/admin/feedback/:feedbackId" element={<AdminFeedbackPage />} />
          </Routes>
        </QueryClientProvider>
      </MemoryRouter>,
    );

    const round = (await screen.findByText("Round 1: answer")).closest("li") as HTMLElement;
    expect([...round.querySelectorAll("pre")].map((p) => p.textContent)).toEqual([
      "Answer from the documentation.",
      "Question: how do I reserve a node?",
      "Create a lease first.",
    ]);
  });
});
