import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PullLine, PullsDuring } from "../../components/shell/PullsPanel";
import { pullPercent, recordPull, usePulls, type PullProgress } from "../pulls";

const base: PullProgress = { type: "pull", id: "pull-1", hostId: "local", ref: "nginx:1.28", status: "pulling", message: "Downloading",
  current: 10 * 1024 * 1024, total: 40 * 1024 * 1024, layers: 4, layersDone: 1, startedAt: 1 };

function List() {
  return <ul>{usePulls().map((p) => <li key={p.id}>{p.ref} {p.status}</li>)}</ul>;
}

describe("pull progress", () => {
  afterEach(() => vi.useRealTimers());

  it("reports bytes, then layers, then done", () => {
    expect(pullPercent(base)).toBe(25);
    expect(pullPercent({ ...base, total: 0, current: 0 })).toBe(25);
    expect(pullPercent({ ...base, total: 0, layers: 0 })).toBeNull();
    expect(pullPercent({ ...base, status: "done" })).toBe(100);
  });

  it("shows a pull and drops it a moment after it ends", () => {
    vi.useFakeTimers();
    render(<List />);
    act(() => recordPull(base));
    expect(screen.getByText("nginx:1.28 pulling")).toBeInTheDocument();
    act(() => recordPull({ ...base, status: "done" }));
    expect(screen.getByText("nginx:1.28 done")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(7000));
    expect(screen.queryByText(/nginx:1.28/)).toBeNull();
  });

  it("draws the bar with sizes and layers, or the error", () => {
    const { rerender } = render(<PullLine pull={base} host="forge" />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");
    expect(screen.getByText(/25% · 10\.0 MB \/ 40\.0 MB · 1\/4 layers/)).toBeInTheDocument();
    rerender(<PullLine pull={{ ...base, status: "error", message: "manifest unknown" }} />);
    expect(screen.getByText("manifest unknown")).toBeInTheDocument();
  });

  it("shows only the pulls an action started, on its hosts, while it runs", () => {
    act(() => recordPull({ ...base, id: "old", ref: "before:1" }));
    const { rerender } = render(<PullsDuring hostIds={["local"]} active />);
    act(() => recordPull({ ...base, id: "new", ref: "app:2" }));
    act(() => recordPull({ ...base, id: "elsewhere", hostId: "h2", ref: "other:1" }));
    expect(screen.getByLabelText("Pull of app:2")).toBeInTheDocument();
    expect(screen.queryByLabelText("Pull of before:1")).toBeNull();
    expect(screen.queryByLabelText("Pull of other:1")).toBeNull();
    rerender(<PullsDuring hostIds={["local"]} active={false} />);
    expect(screen.queryByLabelText("Pull of app:2")).toBeNull();
  });
});
