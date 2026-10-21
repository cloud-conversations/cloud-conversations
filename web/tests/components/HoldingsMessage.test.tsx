import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HoldingsMessage } from "../../src/components/HoldingsMessage";
import type { Message } from "../../src/api/types";

const HOLDINGS: Message = {
  message_id: "m1",
  role: "assistant",
  kind: "holdings",
  text: "One lease at CHI@TACC, and nothing running.",
  payload: {
    tables: [
      "# leases\n\n## CHI@TACC\n\nmine  lease-1  ACTIVE  1x",
      "# instances\n\n## CHI@TACC\n\nnothing",
    ],
  },
  description_id: null,
  description_version: null,
  created_at: "2026-09-15T10:00:00Z",
};

describe("a holdings report", () => {
  it("shows the summary over every table it read", () => {
    render(<HoldingsMessage message={HOLDINGS} />);

    expect(
      screen.getByText("One lease at CHI@TACC, and nothing running."),
    ).toBeInTheDocument();

    const tables = [...document.querySelectorAll("pre")].map((p) => p.textContent);
    expect(tables).toEqual([
      "# leases\n\n## CHI@TACC\n\nmine  lease-1  ACTIVE  1x",
      "# instances\n\n## CHI@TACC\n\nnothing",
    ]);
  });
});
