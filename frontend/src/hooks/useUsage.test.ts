import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { CodexUsageSnapshot, UsageSnapshot } from "../types";
import { useUsage } from "./useUsage";

vi.mock("../api/client", () => ({ api: { getUsage: vi.fn(), getCodexUsage: vi.fn() } }));

const devin: UsageSnapshot = { daily_used_percent: 5, weekly_used_percent: 17, weekly_reset_unix: null,
  plan_name: "Pro", fetched_at: 100, error: null };
const codex: CodexUsageSnapshot = {
  primary: { used_percent: 26, window_minutes: 300, reset_unix: null }, secondary: null,
  plan_name: "plus", account_email: null, fetched_at: 100, error: null, stale: false,
};

beforeEach(() => {
  vi.mocked(api.getUsage).mockReset().mockResolvedValue(devin);
  vi.mocked(api.getCodexUsage).mockReset().mockResolvedValue(codex);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("useUsage", () => {
  it("never fetches or polls disabled integrations, including manual refresh", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useUsage());
    await act(async () => { await result.current.refresh(); vi.advanceTimersByTime(120_000); });
    expect(api.getUsage).not.toHaveBeenCalled();
    expect(api.getCodexUsage).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(false);
    expect(result.current.codexLoading).toBe(false);
  });

  it("enables and disables each provider independently", async () => {
    const { result, rerender } = renderHook(({ devinUsage, codexUsage }) => useUsage({ devinUsage, codexUsage }),
      { initialProps: { devinUsage: false, codexUsage: true } });
    await waitFor(() => expect(result.current.codexSnapshot).toEqual(codex));
    expect(api.getUsage).not.toHaveBeenCalled();
    rerender({ devinUsage: true, codexUsage: false });
    await waitFor(() => expect(result.current.snapshot).toEqual(devin));
    expect(result.current.codexSnapshot).toBeNull();
    await act(async () => { await result.current.refresh(); });
    expect(api.getCodexUsage).toHaveBeenCalledTimes(1);
  });

  it("ignores an in-flight response after disabling a provider", async () => {
    let finish!: (value: UsageSnapshot) => void;
    vi.mocked(api.getUsage).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const { result, rerender } = renderHook(({ enabled }) => useUsage({ devinUsage: enabled, codexUsage: false }),
      { initialProps: { enabled: true } });
    rerender({ enabled: false });
    await act(async () => { finish(devin); });
    expect(result.current.snapshot).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("displays Devin immediately while ChatGPT is still loading", async () => {
    let finish!: (value: CodexUsageSnapshot) => void;
    vi.mocked(api.getCodexUsage).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    const { result } = renderHook(() => useUsage({ devinUsage: true, codexUsage: true }));
    await waitFor(() => expect(result.current.snapshot).toEqual(devin));
    expect(result.current.loading).toBe(false);
    expect(result.current.codexLoading).toBe(true);
    await act(async () => { finish(codex); });
    expect(result.current.codexSnapshot).toEqual(codex);
    expect(result.current.codexLoading).toBe(false);
  });

  it("fetches ChatGPT even when the Devin endpoint fails", async () => {
    vi.mocked(api.getUsage).mockRejectedValueOnce(new Error("Offline"));
    const { result } = renderHook(() => useUsage({ devinUsage: true, codexUsage: true }));
    await waitFor(() => expect(result.current.codexSnapshot).toEqual(codex));
    expect(result.current.snapshot?.error).toContain("Devin usage");
  });

  it("forces both providers to refresh after a turn or a manual refresh", async () => {
    const { result } = renderHook(() => useUsage({ devinUsage: true, codexUsage: true }));
    await waitFor(() => expect(result.current.codexLoading).toBe(false));
    await act(async () => { await result.current.refresh(); });
    expect(api.getUsage).toHaveBeenCalledTimes(2);
    expect(api.getCodexUsage).toHaveBeenLastCalledWith(true);
  });

  it("marks the last ChatGPT quota stale on a transport failure", async () => {
    const { result } = renderHook(() => useUsage({ devinUsage: true, codexUsage: true }));
    await waitFor(() => expect(result.current.codexLoading).toBe(false));
    vi.mocked(api.getCodexUsage).mockRejectedValueOnce(new Error("Offline"));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.codexSnapshot?.primary).toEqual(codex.primary);
    expect(result.current.codexSnapshot?.fetched_at).toBe(100);
    expect(result.current.codexSnapshot?.stale).toBe(true);
    expect(result.current.codexSnapshot?.error).toContain("ChatGPT usage");
  });

  it("preserves the existing Devin snapshot when its transport fails", async () => {
    const { result } = renderHook(() => useUsage({ devinUsage: true, codexUsage: true }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    vi.mocked(api.getUsage).mockRejectedValueOnce(new Error("Offline"));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.snapshot).toEqual(devin);
    expect(result.current.codexSnapshot).toEqual(codex);
  });

  it("polls while visible and clears polling when unmounted", async () => {
    vi.useFakeTimers();
    vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    const { unmount } = renderHook(() => useUsage({ devinUsage: true, codexUsage: true }));
    await act(async () => {});
    await act(async () => { vi.advanceTimersByTime(60_000); });
    expect(api.getCodexUsage).toHaveBeenCalledTimes(2);
    expect(api.getCodexUsage).toHaveBeenLastCalledWith(false);
    unmount();
    await act(async () => { vi.advanceTimersByTime(60_000); });
    expect(api.getCodexUsage).toHaveBeenCalledTimes(2);
  });
});
