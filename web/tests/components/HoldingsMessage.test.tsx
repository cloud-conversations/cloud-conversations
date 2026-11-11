import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { HoldingsMessage } from "../../src/components/HoldingsMessage";
import { ACTIVITY } from "../../src/components/ThreadSidebar";
import type { HoldingKind, HoldingRow, Message } from "../../src/api/types";

const OLD: Message = {
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

const LEASES: HoldingKind = { name: "leases", label: "Leases", holds_hardware: true };
const INSTANCES: HoldingKind = { name: "instances", label: "Instances", holds_hardware: true };
const KEY_PAIRS: HoldingKind = { name: "key_pairs", label: "Key pairs", holds_hardware: false };
const GROUPS: HoldingKind = { name: "security_groups", label: "Security groups", holds_hardware: false };

function row(over: Partial<HoldingRow>): HoldingRow {
  return {
    kind: "instances",
    site: "CHI@UC",
    resource_id: "00000000-aaaa",
    name: "an-instance",
    status: "ACTIVE",
    made_by: "you",
    run_id: null,
    thread_id: null,
    title: null,
    lease_ends: null,
    released_at: null,
    ...over,
  };
}

const BENCH = { made_by: "experiment", run_id: "r1", thread_id: "t1", title: "GPU benchmark", site: "CHI@TACC" } as const;
const BENCH_LEASE = row({ ...BENCH, kind: "leases", resource_id: "lease-1", lease_ends: "2026-11-12T18:00:00Z" });
const BENCH_NODE = row({ ...BENCH, resource_id: "node-1", lease_ends: "2026-11-12T18:00:00Z" });
const MPI = row({
  made_by: "experiment", run_id: "r2", thread_id: "t2", title: "MPI scaling",
  status: "released", released_at: "2026-11-05T18:00:00Z",
});

function listing(text: string, kinds: HoldingKind[], rows: HoldingRow[]): Message {
  return {
    ...OLD,
    text,
    payload: {
      kinds,
      rows,
      left_out: ["There's no site called Mars."],
      unread: { "CHI@NCAR": "Not authorised for leases 'CHI@NCAR'" },
    },
  };
}

function show(message: Message) {
  return render(
    <MemoryRouter>
      <HoldingsMessage message={message} />
    </MemoryRouter>,
  );
}

describe("a holdings report", () => {
  it("shows an old report's tables as they were", () => {
    show(OLD);

    const tables = [...document.querySelectorAll("pre")].map((p) => p.textContent);
    expect(tables).toEqual([
      "# leases\n\n## CHI@TACC\n\nmine  lease-1  ACTIVE  1x",
      "# instances\n\n## CHI@TACC\n\nnothing",
    ]);
  });

  it("shows each experiment once, linked to its chat, with its site and lease end", () => {
    show(listing("1 lease, 1 instance at CHI@TACC.", [LEASES, INSTANCES], [BENCH_LEASE, BENCH_NODE]));

    const [link] = screen.getAllByRole("link");
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(link).toHaveAttribute("href", "/threads/t1#run-r1");
    expect(link.closest("div")).toHaveTextContent(/^Holds hardwareGPU benchmarkCHI@TACC, until Nov 12$/);
  });

  it("puts running experiments and other running leases or instances in separate boxes", () => {
    const outside = row({ name: "bob-mpi-0", made_by: "other", resource_id: "i1" });
    const key = row({ kind: "key_pairs", name: "alice-laptop", resource_id: "k1" });
    show(listing("3 at CHI@UC.", [INSTANCES, KEY_PAIRS], [BENCH_NODE, outside, key]));

    expect(screen.getByText("Your running experiments").closest("section")).toHaveTextContent(
      /^Your running experimentsHolds hardwareGPU benchmarkCHI@TACC, until Nov 12$/,
    );
    expect(screen.getByText("Other running resources").closest("section")).toHaveTextContent(
      /^Other running resourcesHolds hardwarebob-mpi-0CHI@UC, active$/,
    );
    expect(screen.queryByRole("button", { name: "Instances" })).not.toBeInTheDocument();
  });

  it("says no experiments are running when only other resources are", () => {
    const outside = row({ name: "bob-mpi-0", made_by: "other", resource_id: "i1" });
    show(listing("1 instance at CHI@UC.", [INSTANCES, KEY_PAIRS], [outside]));

    expect(screen.getByText("No experiments are running.")).toBeInTheDocument();
  });

  it("says no resources are running when nothing holds hardware", () => {
    const key = row({ kind: "key_pairs", name: "alice-laptop", resource_id: "k1" });
    show(listing("1 key pair at CHI@UC.", [LEASES, KEY_PAIRS], [key]));

    expect(screen.getByText("No resources are running.")).toBeInTheDocument();
    expect(screen.queryByText("Your running experiments")).not.toBeInTheDocument();
  });

  it("marks only an experiment that holds a lease or instance as holding hardware", () => {
    const group = row({ ...BENCH, kind: "security_groups", run_id: "r3", thread_id: "t3", title: "Firewall test" });
    show(listing("2 at CHI@TACC.", [LEASES, INSTANCES], [BENCH_LEASE, group]));

    expect(screen.getAllByText("Holds hardware")).toHaveLength(1);
    const dot = screen.getByText("Holds hardware").parentElement;
    expect(dot).toHaveClass(...ACTIVITY.reserved.dot.split(" "));
    expect(dot?.closest("div")).toHaveTextContent(/^Holds hardwareGPU benchmarkCHI@TACC, until Nov 12$/);
  });

  it("hides released experiments until asked, then links each to its chat with the day", async () => {
    show(listing("1 instance at CHI@UC.", [LEASES, INSTANCES], [MPI]));
    expect(screen.queryByRole("link", { name: "MPI scaling" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Already released (1)" }));

    expect(screen.getByRole("link", { name: "MPI scaling" }).closest("div")).toHaveTextContent(
      /^MPI scalingNov 5$/,
    );
  });

  it("folds everything else into one total, broken out by type, then rows, on click", async () => {
    const rest = [
      row({ kind: "key_pairs", name: "alice-laptop", status: "", resource_id: "k1" }),
      row({ kind: "key_pairs", resource_id: "k2" }),
      row({ kind: "security_groups", name: "default", status: "", resource_id: "g1" }),
    ];
    show(listing("4 at CHI@UC.", [INSTANCES, KEY_PAIRS, GROUPS], [BENCH_NODE, ...rest]));
    expect(screen.queryByRole("button", { name: "Key pairs" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Also in your project (3)" }));
    await userEvent.click(screen.getByRole("button", { name: "Key pairs" }));

    expect(screen.getByRole("button", { name: "Key pairs" }).closest("div")).toHaveTextContent(/^Key pairs2$/);
    expect(screen.getByRole("button", { name: "Security groups" }).closest("div")).toHaveTextContent(/^Security groups1$/);
    expect(screen.getByText("alice-laptop").closest("div")).toHaveTextContent(/^alice-laptopCHI@UC$/);
  });

  it("draws the folded sections and the types they open at the card's own size", async () => {
    const key = row({ kind: "key_pairs", resource_id: "k1" });
    show(listing("1 key pair at CHI@UC.", [LEASES, KEY_PAIRS], [MPI, key]));

    await userEvent.click(screen.getByRole("button", { name: "Also in your project (1)" }));

    for (const name of ["Already released (1)", "Also in your project (1)", "Key pairs"]) {
      expect(screen.getByRole("button", { name }).closest("[class*='text-xs']")).toBeNull();
    }
  });

  it("uses the same boxes for a listing of one type", () => {
    const rows = [
      row({ name: "bob-mpi-0", made_by: "other", resource_id: "i1" }),
      row({ name: "scratch-vm", status: "SHUTOFF", resource_id: "i2" }),
    ];
    show(listing("2 instances at CHI@UC.", [INSTANCES], rows));

    expect(screen.getByText("Other running resources").closest("section")).toHaveTextContent(
      /^Other running resourcesHolds hardwarebob-mpi-0CHI@UC, activeHolds hardwarescratch-vmCHI@UC, shutoff$/,
    );
  });

  it("opens a listing of one type that holds no hardware straight to its rows", () => {
    const keys = [
      row({ kind: "key_pairs", name: "alice-laptop", status: "", resource_id: "k1" }),
      row({ kind: "key_pairs", name: "alice-desktop", status: "", resource_id: "k2" }),
    ];
    show(listing("2 key pairs at CHI@UC.", [KEY_PAIRS], keys));

    expect(screen.getByText("alice-laptop").closest("div")).toHaveTextContent(/^alice-laptopCHI@UC$/);
    expect(screen.getByText("alice-desktop")).toBeInTheDocument();
  });

  it("lists the types, still folded, when several are all there is", () => {
    const rest = [
      row({ kind: "key_pairs", name: "alice-laptop", status: "", resource_id: "k1" }),
      row({ kind: "security_groups", name: "default", status: "", resource_id: "g1" }),
    ];
    show(listing("2 at CHI@UC.", [KEY_PAIRS, GROUPS], rest));

    expect(screen.getByRole("button", { name: "Key pairs" })).toBeInTheDocument();
    expect(screen.queryByText("alice-laptop")).not.toBeInTheDocument();
  });

  it("shows only the summary when nothing matches", () => {
    show(listing("No leases at CHI@UC where ends before Nov 12 09:00.", [LEASES], []));

    expect(screen.getByText("No leases at CHI@UC where ends before Nov 12 09:00.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("names what it left out", () => {
    show(listing("1 lease, 1 instance at CHI@TACC.", [LEASES, INSTANCES], [BENCH_LEASE]));

    expect(screen.getByText("There's no site called Mars.")).toBeInTheDocument();
  });

  it("follows a site it couldn't read with the error in red", () => {
    show(listing("1 lease, 1 instance at CHI@TACC.", [LEASES, INSTANCES], [BENCH_LEASE]));

    const error = screen.getByText("Not authorised for leases 'CHI@NCAR'");
    expect(error).toHaveClass("text-brand-danger");
    expect(error.closest("p")).toHaveTextContent(/^CHI@NCAR wasn't read. Not authorised for leases 'CHI@NCAR'$/);
  });
});
