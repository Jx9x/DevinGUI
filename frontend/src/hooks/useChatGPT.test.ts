import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, cleanup } from "@testing-library/react";
import { useChatGPT } from "./useChatGPT";
import {
  chatgptAPI,
  type ChatEvent,
  type ChatSession,
  type Controls,
  type EventPage,
  type Receipt,
  type SendInput,
} from "../api/chatgpt";

vi.mock("../api/chatgpt", async () => {
  const actual = await vi.importActual<typeof import("../api/chatgpt")>("../api/chatgpt");
  return {
    ...actual,
    chatgptAPI: {
      status: vi.fn(),
      sessions: vi.fn(),
      models: vi.fn(),
      outbox: vi.fn(),
      events: vi.fn(),
      controls: vi.fn(),
      send: vi.fn(),
      stop: vi.fn(),
      cancel: vi.fn(),
      open: vi.fn(),
      refreshModels: vi.fn(),
    },
  };
});

const KEY = "devin-gui-chatgpt-v1";
const sendMock = vi.mocked(chatgptAPI.send);
const statusMock = vi.mocked(chatgptAPI.status);
const eventsMock = vi.mocked(chatgptAPI.events);
const controlsMock = vi.mocked(chatgptAPI.controls);

const input: SendInput = {
  id: "input-1",
  sessionId: null,
  text: "hello outbox",
  model: "model-1",
  reasoningEffort: null,
  mode: "auto",
  dueAt: 0,
  automation: "off",
};

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (error: unknown) => void = () => undefined;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function pendingOf(raw: string | null) {
  if (!raw) return null;
  const parsed = JSON.parse(raw) as { drafts: { pending?: { input: SendInput; state: string } }[] };
  return parsed.drafts.find((d) => d.pending)?.pending ?? null;
}

const realSetItem = Storage.prototype.setItem;
function failWritesFor(...storages: Storage[]) {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (
    this: Storage,
    key: string,
    value: string
  ) {
    if (storages.includes(this)) throw new Error("storage disabled");
    return realSetItem.call(this, key, value);
  });
}

beforeEach(() => {
  cleanup();
  vi.restoreAllMocks();
  for (const fn of Object.values(chatgptAPI)) {
    (fn as ReturnType<typeof vi.fn>).mockReset();
  }
  statusMock.mockResolvedValue({ available: false });
  controlsMock.mockResolvedValue({ sessionId: "s", activeTurnId: null, stopPending: false });
  window.sessionStorage.clear();
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe("useChatGPT", () => {

  it("persists the pending input identity to both storages before network delivery", async () => {
    const { result } = renderHook(() => useChatGPT(false));
    act(() => {
      result.current.updateDraft({ text: input.text, model: input.model });
    });
    const key = result.current.draft.id;

    const send = deferred<Receipt>();
    const observed: { session: string | null; local: string | null } = { session: null, local: null };
    sendMock.mockImplementation(() => {
      observed.session = window.sessionStorage.getItem(KEY);
      observed.local = window.localStorage.getItem(KEY);
      return send.promise;
    });

    await act(async () => {
      void result.current.send(input, key);
    });

    const sessionPending = pendingOf(observed.session);
    expect(sessionPending?.input.id).toBe("input-1");
    expect(sessionPending?.state).toBe("submitting");
    const localPending = pendingOf(observed.local);
    expect(localPending?.input.id).toBe("input-1");
    expect(localPending?.state).toBe("submitting");

    await act(async () => {
      send.resolve({ id: "input-1", sessionId: null, state: "sent" });
    });
  });

  it("restores the in-flight input as uncertain from localStorage when the tab loses sessionStorage", async () => {
    const first = renderHook(() => useChatGPT(false));
    const key = first.result.current.draft.id;

    const send = deferred<Receipt>();
    const observed: { local: string | null } = { local: null };
    sendMock.mockImplementation(() => {
      observed.local = window.localStorage.getItem(KEY);
      return send.promise;
    });
    await act(async () => {
      void first.result.current.send(input, key);
    });

    first.unmount();
    window.sessionStorage.clear();
    if (observed.local !== null) {
      window.localStorage.setItem(KEY, observed.local);
    } else {
      window.localStorage.removeItem(KEY);
    }

    const second = renderHook(() => useChatGPT(false));
    const pending = second.result.current.draft.pending;
    expect(pending?.state).toBe("uncertain");
    expect(pending?.input).toEqual(input);
    expect(sendMock).toHaveBeenCalledTimes(1);

    second.unmount();
    send.resolve({ id: "input-1", sessionId: null, state: "sent" });
  });

  it("keeps the pending identity durable in localStorage and restorable when sessionStorage writes fail", async () => {
    failWritesFor(window.sessionStorage);
    const first = renderHook(() => useChatGPT(false));
    const key = first.result.current.draft.id;

    const send = deferred<Receipt>();
    const observed: { session: string | null; local: string | null } = { session: null, local: null };
    sendMock.mockImplementation(() => {
      observed.session = window.sessionStorage.getItem(KEY);
      observed.local = window.localStorage.getItem(KEY);
      return send.promise;
    });

    await act(async () => {
      void first.result.current.send(input, key);
    });

    expect(observed.session).toBeNull();
    const localPending = pendingOf(observed.local);
    expect(localPending?.input.id).toBe("input-1");
    expect(localPending?.state).toBe("submitting");

    first.unmount();
    if (observed.local !== null) {
      window.localStorage.setItem(KEY, observed.local);
    } else {
      window.localStorage.removeItem(KEY);
    }
    const second = renderHook(() => useChatGPT(false));
    const pending = second.result.current.draft.pending;
    expect(pending?.state).toBe("uncertain");
    expect(pending?.input).toEqual(input);

    second.unmount();
    send.resolve({ id: "input-1", sessionId: null, state: "sent" });
  });

  it("keeps the pending identity in sessionStorage when localStorage writes fail", async () => {
    failWritesFor(window.localStorage);
    const { result } = renderHook(() => useChatGPT(false));
    const key = result.current.draft.id;

    const send = deferred<Receipt>();
    const observed: { session: string | null; local: string | null } = { session: null, local: null };
    sendMock.mockImplementation(() => {
      observed.session = window.sessionStorage.getItem(KEY);
      observed.local = window.localStorage.getItem(KEY);
      return send.promise;
    });

    await act(async () => {
      void result.current.send(input, key);
    });

    const sessionPending = pendingOf(observed.session);
    expect(sessionPending?.input.id).toBe("input-1");
    expect(sessionPending?.state).toBe("submitting");
    await act(async () => {
      send.resolve({ id: "input-1", sessionId: null, state: "sent" });
    });
  });

  it("retains the in-memory pending input when both storages fail so retry uses the same identity", async () => {
    failWritesFor(window.sessionStorage, window.localStorage);
    const { result } = renderHook(() => useChatGPT(false));
    const key = result.current.draft.id;

    const send = deferred<Receipt>();
    sendMock.mockImplementation(() => send.promise);
    await act(async () => {
      void result.current.send(input, key);
    });
    expect(result.current.draft.pending?.input).toEqual(input);
    expect(result.current.draft.pending?.state).toBe("submitting");

    await act(async () => {
      send.reject(new Error("network down"));
      await send.promise.catch(() => undefined);
    });
    expect(result.current.draft.pending?.input).toEqual(input);
    expect(result.current.draft.pending?.state).toBe("uncertain");

    const retry = deferred<Receipt>();
    sendMock.mockImplementation(() => retry.promise);
    await act(async () => {
      void result.current.send(result.current.draft.pending!.input, key);
      retry.resolve({ id: "input-1", sessionId: "session-1", deliveredSessionId: "session-1", state: "sent" });
    });
    expect(sendMock).toHaveBeenCalledTimes(2);
    expect(sendMock.mock.calls[0][0]).toEqual(input);
    expect(sendMock.mock.calls[1][0]).toEqual(input);
    expect(sendMock.mock.calls[1][0].id).toBe("input-1");
  });
});

describe("useChatGPT session history retention", () => {
  const sessionA: ChatSession = { id: "sess-a", title: "Session A", updatedAt: 1 };
  const sessionB: ChatSession = { id: "sess-b", title: "Session B", updatedAt: 2 };
  const sessionC: ChatSession = { id: "sess-c", title: "Session C", updatedAt: 3 };

  function msg(seq: number, text: string, messageId: string, origin?: number): ChatEvent {
    return { seq, kind: "assistant_message", messageId, message: { text }, origin };
  }

  function page(events: ChatEvent[], nextFrom = 0, id = "s"): EventPage {
    return {
      summary: { id, title: id, updatedAt: 0 },
      events,
      nextFrom,
      total: events.length,
    };
  }

  function texts(view: { events: ChatEvent[] }) {
    return view.events.map((e) => e.message?.text);
  }

  type EventCall = ReturnType<typeof deferred<EventPage>> & { sid: string };
  let recentCalls: EventCall[];
  let olderCalls: EventCall[];
  let controlsCalls: (ReturnType<typeof deferred<Controls>> & { sid: string })[];

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    recentCalls = [];
    olderCalls = [];
    controlsCalls = [];
    eventsMock.mockImplementation((_id, options) => {
      const d = Object.assign(deferred<EventPage>(), { sid: _id });
      (options?.before !== undefined ? olderCalls : recentCalls).push(d);
      return d.promise;
    });
    controlsMock.mockImplementation((_id) => {
      const d = Object.assign(deferred<Controls>(), { sid: _id });
      controlsCalls.push(d);
      return d.promise;
    });
  });

  async function open(session: ChatSession) {
    const hook = renderHook(() => useChatGPT(true));
    await act(async () => {});
    act(() => {
      hook.result.current.openDraft(session);
    });
    await act(async () => {});
    return hook;
  }

  function settle(index: number, events: ChatEvent[]) {
    return async () => {
      recentCalls[index].resolve(page(events, 0, recentCalls[index].sid));
      controlsCalls[index]?.resolve({ sessionId: recentCalls[index].sid, activeTurnId: null, stopPending: false });
    };
  }

  it("keeps loaded history and the older boundary while a refresh re-fetches the recent page", async () => {
    const { result } = await open(sessionA);
    expect(recentCalls).toHaveLength(1);
    await act(settle(0, [msg(100, "current", "m1")]));
    expect(texts(result.current.view)).toEqual(["current"]);
    expect(result.current.view.before).toBe(100);

    await act(async () => {
      const older = result.current.older();
      olderCalls[0].resolve(page([msg(10, "older", "m0")], 0, olderCalls[0].sid));
      await older;
    });
    expect(texts(result.current.view)).toEqual(["older", "current"]);
    expect(result.current.view.before).toBe(10);

    await act(async () => {
      result.current.refresh();
    });
    expect(recentCalls).toHaveLength(2);
    expect(texts(result.current.view)).toEqual(["older", "current"]);
    expect(result.current.view.controls).toBeNull();
    expect(result.current.view.before).toBe(10);

    await act(settle(1, [msg(120, "updated", "m1", 100)]));
    expect(texts(result.current.view)).toEqual(["older", "updated"]);
    expect(result.current.view.before).toBe(10);
    expect(result.current.view.controls).not.toBeNull();
  });

  it("preserves loaded history across pane reactivation and after a failed refresh", async () => {
    const { result, rerender } = renderHook(({ active }) => useChatGPT(active), {
      initialProps: { active: true },
    });
    await act(async () => {});
    act(() => {
      result.current.openDraft(sessionA);
    });
    await act(async () => {});
    await act(settle(0, [msg(100, "current", "m1")]));
    await act(async () => {
      const older = result.current.older();
      olderCalls[0].resolve(page([msg(10, "older", "m0")], 0, olderCalls[0].sid));
      await older;
    });
    expect(texts(result.current.view)).toEqual(["older", "current"]);
    expect(result.current.view.before).toBe(10);

    await act(async () => {
      rerender({ active: false });
    });
    await act(async () => {
      rerender({ active: true });
    });
    expect(recentCalls).toHaveLength(2);
    await act(settle(1, [msg(100, "current", "m1")]));
    expect(texts(result.current.view)).toEqual(["older", "current"]);
    expect(result.current.view.before).toBe(10);

    await act(async () => {
      result.current.refresh();
    });
    expect(recentCalls).toHaveLength(3);
    await act(async () => {
      recentCalls[2].reject(new Error("fetch failed"));
      controlsCalls[2]?.resolve({ sessionId: recentCalls[2].sid, activeTurnId: null, stopPending: false });
    });
    expect(result.current.view.error).toBe("fetch failed");
    expect(texts(result.current.view)).toEqual(["older", "current"]);
    expect(result.current.view.before).toBe(10);
  });

  it("clears events on a session change and ignores a late response for the old session", async () => {
    const { result } = await open(sessionA);
    await act(settle(0, [msg(100, "current", "m1")]));
    expect(result.current.view.id).toBe("sess-a");
    expect(texts(result.current.view)).toEqual(["current"]);

    await act(async () => {
      result.current.refresh();
    });
    expect(recentCalls).toHaveLength(2);
    act(() => {
      result.current.openDraft(sessionB);
    });
    await act(async () => {});
    expect(result.current.view.id).toBe("sess-b");
    expect(result.current.view.events).toEqual([]);
    expect(result.current.view.before).toBeNull();
    expect(recentCalls).toHaveLength(3);

    await act(async () => {
      recentCalls[1].resolve(page([msg(999, "late-a", "mL")], 0, recentCalls[1].sid));
      controlsCalls[1]?.resolve({ sessionId: recentCalls[1].sid, activeTurnId: null, stopPending: false });
    });
    expect(result.current.view.id).toBe("sess-b");
    expect(result.current.view.events).toEqual([]);
    expect(result.current.view.before).toBeNull();

    await act(async () => {
      recentCalls[2].resolve(page([msg(200, "b-event", "b1")], 0, recentCalls[2].sid));
      controlsCalls[2]?.resolve({ sessionId: recentCalls[2].sid, activeTurnId: null, stopPending: false });
    });
    expect(result.current.view.id).toBe("sess-b");
    expect(texts(result.current.view)).toEqual(["b-event"]);
    expect(result.current.view.before).toBe(200);
    expect(result.current.view.controls?.sessionId).toBe("sess-b");
  });

  it("leaves the older boundary null on an empty initial page and initializes it on the first nonempty page", async () => {
    const { result } = await open(sessionC);
    expect(recentCalls).toHaveLength(1);
    await act(async () => {
      recentCalls[0].resolve(page([], 0, recentCalls[0].sid));
      controlsCalls[0]?.resolve({ sessionId: recentCalls[0].sid, activeTurnId: null, stopPending: false });
    });
    expect(result.current.view.events).toEqual([]);
    expect(result.current.view.before).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(recentCalls).toHaveLength(2);
    await act(async () => {
      recentCalls[1].resolve(page([msg(60, "first", "f1")], 61, recentCalls[1].sid));
      controlsCalls[1]?.resolve({ sessionId: recentCalls[1].sid, activeTurnId: null, stopPending: false });
    });
    expect(texts(result.current.view)).toEqual(["first"]);
    expect(result.current.view.before).toBe(60);
  });

  it("keeps the exhausted older boundary and messages after refresh", async () => {
    const { result } = await open(sessionA);
    await act(settle(0, [msg(100, "current", "m1")]));
    await act(async () => {
      const older = result.current.older();
      olderCalls[0].resolve(page([], 0, olderCalls[0].sid));
      await older;
    });
    expect(result.current.view.before).toBeNull();
    expect(texts(result.current.view)).toEqual(["current"]);

    await act(async () => {
      result.current.refresh();
    });
    expect(recentCalls).toHaveLength(2);
    await act(settle(1, [msg(100, "current", "m1")]));
    expect(texts(result.current.view)).toEqual(["current"]);
    expect(result.current.view.before).toBeNull();
  });
});
