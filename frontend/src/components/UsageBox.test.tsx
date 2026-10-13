import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, fireEvent, within } from "@testing-library/react";

afterEach(() => { cleanup(); vi.useRealTimers(); });
import { UsageBox, formatCompactReset, formatResetIn, quotaWindowLabel, usageColorClass } from "./UsageBox";
import type { CodexUsageSnapshot, UsageSnapshot } from "../types";

function snapshot(overrides: Partial<UsageSnapshot> = {}): UsageSnapshot {
  // Add a 30s buffer so a few ms of test latency don't flip "35hr" to "34hr".
  return {
    daily_used_percent: 5,
    weekly_used_percent: 10,
    weekly_reset_unix: Math.floor(Date.now() / 1000) + 35 * 3600 + 30,
    plan_name: "Pro",
    fetched_at: Math.floor(Date.now() / 1000),
    error: null,
    ...overrides,
  };
}

function codexSnapshot(overrides: Partial<CodexUsageSnapshot> = {}): CodexUsageSnapshot {
  return {
    primary: { used_percent: 26, window_minutes: 300, reset_unix: Date.now() / 1000 + 3 * 3600 + 20 * 60 },
    secondary: { used_percent: 20, window_minutes: 10_080, reset_unix: Date.now() / 1000 + 6 * 86400 + 3600 },
    plan_name: "plus", account_email: "person@example.com", fetched_at: Date.now() / 1000,
    error: null, stale: false, ...overrides,
  };
}

describe("usageColorClass", () => {
  it("is green under 70%", () => {
    expect(usageColorClass(5)).toBe("text-green-400");
  });
  it("is yellow between 70 and 90", () => {
    expect(usageColorClass(75)).toBe("text-yellow-300");
  });
  it("is red at/above 90", () => {
    expect(usageColorClass(90)).toBe("text-red-400");
  });
  it("is muted when null", () => {
    expect(usageColorClass(null)).toBe("text-gray-500");
  });
});

describe("formatResetIn", () => {
  it("formats hours when under 72h", () => {
    const now = 1_000_000_000_000;
    const reset = Math.floor(now / 1000) + 35 * 3600;
    expect(formatResetIn(reset, now)).toBe("35hr until reset");
  });
  it("formats days+hours when 72h or more", () => {
    const now = 1_000_000_000_000;
    const reset = Math.floor(now / 1000) + 58 * 3600; // 2d 10h
    expect(formatResetIn(reset, now)).toBe("2d 10h until reset");
  });
  it("returns Expired when reset is in the past", () => {
    expect(formatResetIn(100, Date.now())).toBe("Expired");
  });
  it("returns empty string when unknown", () => {
    expect(formatResetIn(null)).toBe("");
  });
  it("formats minutes when under 1h", () => {
    const now = 1_000_000_000_000;
    const reset = Math.floor(now / 1000) + 12 * 60;
    expect(formatResetIn(reset, now)).toBe("12m until reset");
  });
});

describe("UsageBox", () => {
  it("hides all placeholders when both integrations are disabled", () => {
    const { container } = render(<UsageBox snapshot={null} loading={false} devinEnabled={false} codexEnabled={false} onRefresh={() => {}} />);
    expect(container.childElementCount).toBe(0);
  });

  it.each(["devin", "codex"])("renders only the enabled %s integration", provider => {
    render(<UsageBox snapshot={snapshot()} loading={false} codexSnapshot={codexSnapshot()}
      devinEnabled={provider === "devin"} codexEnabled={provider === "codex"} onRefresh={() => {}} />);
    expect(screen.queryByRole("region", { name: "Devin usage" }) !== null).toBe(provider === "devin");
    expect(screen.queryByRole("region", { name: "Codex / ChatGPT usage" }) !== null).toBe(provider === "codex");
    expect(screen.getByLabelText("Refresh usage")).toBeTruthy();
    expect(screen.queryByText("Usage unavailable")).toBeNull();
  });

  it("renders daily and weekly percents and reset", () => {
    render(<UsageBox snapshot={snapshot()} loading={false} onRefresh={() => {}} />);
    expect(screen.getByText("5%")).toBeTruthy();
    expect(screen.getByText("10%")).toBeTruthy();
    expect(screen.getByText("Daily")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Devin usage" })).getByText("Weekly")).toBeTruthy();
    expect(screen.getByText("35hr until reset")).toBeTruthy();
    expect(screen.getByText("Pro")).toBeTruthy();
  });

  it("shows em-dash when percents are null but no error", () => {
    render(
      <UsageBox
        snapshot={snapshot({ daily_used_percent: null, weekly_used_percent: null, weekly_reset_unix: null })}
        loading={false}
        onRefresh={() => {}}
      />
    );
    expect(screen.getAllByText("—").length).toBe(2);
  });

  it("shows unavailable message with the error as title", () => {
    render(
      <UsageBox
        snapshot={snapshot({ error: "No tokens found" })}
        loading={false}
        onRefresh={() => {}}
      />
    );
    const el = within(screen.getByRole("region", { name: "Devin usage" })).getByText("Usage unavailable");
    expect(el.getAttribute("title")).toBe("No tokens found");
  });

  it("shows loading state when loading with no snapshot", () => {
    render(<UsageBox snapshot={null} loading={true} onRefresh={() => {}} />);
    expect(screen.getByText("Loading…")).toBeTruthy();
  });

  it("calls onRefresh when the refresh button is clicked", () => {
    let called = 0;
    render(<UsageBox snapshot={snapshot()} loading={false} onRefresh={() => (called += 1)} />);
    fireEvent.click(screen.getByLabelText("Refresh usage"));
    expect(called).toBe(1);
  });

  it("renders ChatGPT used percentages and both resets alongside Devin", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-01T14:00:00Z"));
    render(<UsageBox snapshot={snapshot()} codexSnapshot={codexSnapshot()} loading={false} onRefresh={() => {}} />);
    const codex = within(screen.getByRole("region", { name: "Codex / ChatGPT usage" }));
    expect(codex.getByText("26%")).toBeTruthy();
    expect(codex.getByText("20%")).toBeTruthy();
    expect(codex.getByText("5-hour")).toBeTruthy();
    expect(codex.getByText("Weekly")).toBeTruthy();
    expect(codex.getByText("Reset in 3h 20m").title).toContain("Resets ");
    expect(codex.getByText("Reset in 6d 1h")).toBeTruthy();
    expect(codex.getByText("Codex / ChatGPT").title).toContain("person@example.com");
    expect(screen.getByText("5%")).toBeTruthy();
  });

  it("keeps Devin visible when ChatGPT is unavailable", () => {
    render(<UsageBox snapshot={snapshot()} codexSnapshot={codexSnapshot({ primary: null, secondary: null, error: "Run codex login" })}
      loading={false} onRefresh={() => {}} />);
    expect(screen.getByText("5%")).toBeTruthy();
    const codex = within(screen.getByRole("region", { name: "Codex / ChatGPT usage" }));
    expect(codex.getByText("Usage unavailable").title).toBe("Run codex login");
  });

  it("keeps ChatGPT visible when Devin is unavailable", () => {
    render(<UsageBox snapshot={snapshot({ error: "No Devin credentials" })} codexSnapshot={codexSnapshot()}
      loading={false} onRefresh={() => {}} />);
    expect(screen.getByText("26%")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Devin usage" })).getByText("Usage unavailable")).toBeTruthy();
  });

  it("shows missing ChatGPT usage as unknown instead of zero", () => {
    render(<UsageBox snapshot={snapshot()} codexSnapshot={codexSnapshot({
      primary: { used_percent: null, window_minutes: 300, reset_unix: null }, secondary: null,
    })} loading={false} onRefresh={() => {}} />);
    const codex = within(screen.getByRole("region", { name: "Codex / ChatGPT usage" }));
    expect(codex.getAllByText("—")).toHaveLength(2);
    expect(codex.queryByText("0%")).toBeNull();
  });

  it("marks cached ChatGPT quotas as last known after a failed refresh", () => {
    render(<UsageBox snapshot={snapshot()} codexSnapshot={codexSnapshot({ error: "Refresh timed out", stale: true })}
      loading={false} onRefresh={() => {}} />);
    expect(screen.getByText("Last known usage").title).toContain("Refresh timed out");
    expect(screen.getByText("26%").className).toBe("text-gray-500");
  });

  it("disables the shared refresh while either provider is loading", () => {
    render(<UsageBox snapshot={snapshot()} codexSnapshot={null} codexLoading={true} loading={false} onRefresh={() => {}} />);
    expect((screen.getByLabelText("Refresh usage") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("5%")).toBeTruthy();
    expect(screen.getByText("Loading…")).toBeTruthy();
  });

  it("opens ChatGPT sharing settings in a separate tab", () => {
    render(<UsageBox snapshot={snapshot()} loading={false} onRefresh={() => {}} />);
    const link = screen.getByRole("link", { name: "Manage sharing" });
    expect(link.getAttribute("href")).toBe("https://chatgpt.com/settings/usage");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("updates the reset countdown without fetching again", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-01T14:00:00Z"));
    render(<UsageBox snapshot={snapshot()} codexSnapshot={codexSnapshot()} loading={false} onRefresh={() => {}} />);
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(screen.getByText("Reset in 3h 19m")).toBeTruthy();
  });
});

describe("ChatGPT quota formatting", () => {
  it("uses the actual quota duration returned by Codex", () => {
    expect(quotaWindowLabel(300, "Primary")).toBe("5-hour");
    expect(quotaWindowLabel(10_080, "Secondary")).toBe("Weekly");
    expect(quotaWindowLabel(60, "Primary")).toBe("1-hour");
    expect(quotaWindowLabel(15, "Primary")).toBe("15-min");
  });

  it("handles reset boundaries without showing an expired value as fresh", () => {
    expect(formatCompactReset(null, 100_000)).toBe("");
    expect(formatCompactReset(90, 100_000)).toBe("Reset pending");
    expect(formatCompactReset(110, 100_000)).toBe("1m");
  });
});
