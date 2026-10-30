import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DescriptionCard } from "../../src/components/DescriptionCard";
import type { Version } from "../../src/api/types";

const VERSION: Version = {
  description_id: "d1",
  version: 1,
  requirements: {
    site: "CHI@UC",
    start_at: null,
    start_source: "default",
    duration_hours: 4,
    duration_source: "stated",
    items: [
      { key: "node_type", value: "compute_skylake", source: "stated" },
      { key: "os_release", value: "Ubuntu 24.04", source: "inferred" },
    ],
  },
  rubric: [{ id: "ssh", kind: "ssh_reachable", expected: true, comparator: "eq" }],
  unsupported: ["two nodes at once"],
  open_questions: ["assumed Ubuntu 24.04"],
};

function withImage(name: string, supported: boolean): Version {
  return {
    ...VERSION,
    requirements: {
      ...VERSION.requirements,
      image: { image_id: "img-1", name, supported, testbed: "Chameleon" },
    },
  };
}

function show(over: Partial<Parameters<typeof DescriptionCard>[0]> = {}) {
  const onApprove = vi.fn();
  const onAnswer = vi.fn();
  render(
    <DescriptionCard
      version={VERSION}
      latest={1}
      experiment={null}
      buildInFlight={false}
      onApprove={onApprove}
      onAnswer={onAnswer}
      {...over}
    />,
  );
  return { onApprove, onAnswer };
}

describe("a description card", () => {
  it("folds the software into one line per group, and opens a group to show each check", async () => {
    const warned = vi.spyOn(console, "error").mockImplementation(() => {});
    show({
      version: {
        ...VERSION,
        requirements: {
          ...VERSION.requirements,
          items: [
            ...VERSION.requirements.items,
            { key: "system_package", value: "jq", source: "stated" },
            { key: "system_package", value: "git", source: "inferred" },
            { key: "python_version", value: "3.12", source: "default" },
            { key: "pip_package", value: "numpy", source: "stated" },
          ],
        },
        rubric: [
          ...VERSION.rubric,
          {
            id: "np", kind: "pip_package", target: "numpy", expected: "1",
            comparator: "version_gte", from_requirement: "pip_package",
          },
        ],
      },
    });

    expect(screen.getByRole("button", { name: "System packages 2 · jq, git · 1 inferred" }))
      .toBeInTheDocument();
    expect(screen.queryByText("pip_package numpy version_gte 1")).not.toBeInTheDocument();
    expect(screen.getByText(/and 1 software check, listed with/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Python 3.12 1 · numpy · 1 default" }));

    expect(screen.getByText("pip_package numpy version_gte 1")).toBeInTheDocument();
    expect(warned).not.toHaveBeenCalled();
    warned.mockRestore();
  });

  it("shows each requirement with its source, each check, and what the testbed cannot do", () => {
    show();

    expect(screen.getByRole("row", { name: /node_type compute_skylake stated/ }))
      .toBeInTheDocument();
    expect(screen.getByText("ssh_reachable eq true")).toBeInTheDocument();
    expect(screen.getByText("two nodes at once")).toBeInTheDocument();
  });

  it("says what it assumed, and offers to be told otherwise", async () => {
    const { onAnswer } = show();

    await userEvent.click(screen.getByRole("button", { name: "Change" }));

    expect(onAnswer).toHaveBeenCalledWith("assumed Ubuntu 24.04");
  });

  it("approves the version it is showing", async () => {
    const { onApprove } = show({ version: { ...VERSION, version: 2 }, latest: 3 });

    await userEvent.click(screen.getByRole("button", { name: /Approve and build/ }));

    expect(onApprove).toHaveBeenCalledWith("d1", 2);
  });

  it("shows the lease length and start as requirements, with their source", () => {
    show();

    const rows = [...document.querySelectorAll("tbody tr")].map((r) =>
      [...r.children].map((c) => c.textContent),
    );
    expect(rows[0]).toEqual(["lease length", "4h", "stated"]);
    expect(rows[1]).toEqual([
      "lease start",
      "as soon as you approve the build",
      "default",
    ]);
  });

  it("will not approve a second build while one is running", () => {
    show({ buildInFlight: true });

    expect(screen.getByRole("button", { name: /Approve and build/ })).toBeDisabled();
    expect(screen.getByText(/already running on this experiment/)).toBeInTheDocument();
  });

  it("marks a version a later one has replaced", () => {
    show({ latest: 3 });

    expect(screen.getByText("superseded by v3")).toBeInTheDocument();
  });

  it("says when the testbed does not support the image it chose", () => {
    show({ version: withImage("my-image", false) });

    expect(screen.getByRole("row", { name: "image my-image, not supported by Chameleon" }))
      .toBeInTheDocument();
  });

  it("says on the image_name line whether the testbed supports the image", () => {
    const version = withImage("CC-Ubuntu26.04", true);
    version.requirements.items = [
      ...version.requirements.items,
      { key: "image_name", value: "CC-Ubuntu26.04", source: "inferred" },
    ];
    show({ version });

    expect(
      screen.getByRole("row", { name: "image_name CC-Ubuntu26.04, supported by Chameleon inferred" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /^image CC-Ubuntu26.04/ })).toBeNull();
  });
});
