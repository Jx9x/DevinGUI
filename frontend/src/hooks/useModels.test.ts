import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { useModels } from "./useModels";
import { api } from "../api/client";
import type { ModelInfo, ModelsResponse } from "../types";

vi.mock("../api/client", () => ({
  api: {
    getModels: vi.fn(),
    excludeModel: vi.fn(async () => {}),
    includeModel: vi.fn(async () => {}),
    excludeModels: vi.fn(async () => {}),
    includeModels: vi.fn(async () => {}),
    acknowledgeModel: vi.fn(async () => {}),
  },
  ApiError: class ApiError extends Error {},
}));

const getModelsMock = vi.mocked(api.getModels);
const excludeModelMock = vi.mocked(api.excludeModel);

function mk(id: string, overrides: Partial<ModelInfo> = {}): ModelInfo {
  return {
    id,
    display_name: id,
    vision: false,
    tags: [],
    description: "",
    is_new: false,
    available: true,
    excluded: false,
    reasoning: null,
    is_fast: false,
    fusion: null,
    ...overrides,
  };
}

function modelsResponse(
  models: ModelInfo[],
  overrides: Partial<ModelsResponse> = {}
): ModelsResponse {
  return {
    models,
    selectable: models.filter((m) => m.available && !m.excluded),
    recent: [],
    last_used: null,
    has_new: false,
    ...overrides,
  };
}

async function mount(data: ModelsResponse) {
  getModelsMock.mockResolvedValue(data);
  const hook = renderHook(() => useModels());
  await act(async () => {});
  return hook;
}

describe("useModels selected-model fallback", () => {
  beforeEach(() => {
    cleanup();
    vi.restoreAllMocks();
    getModelsMock.mockReset();
    excludeModelMock.mockReset();
    excludeModelMock.mockResolvedValue(undefined);
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("falls back to a valid recent model when last_used is excluded", async () => {
    const { result } = await mount(
      modelsResponse(
        [mk("excluded-used", { excluded: true }), mk("recent-ok"), mk("first-ok")],
        { recent: [mk("recent-ok")], last_used: "excluded-used" }
      )
    );
    await waitFor(() => expect(result.current.selectedModel).toBe("recent-ok"));
  });

  it("falls back to a valid recent model when last_used is unavailable", async () => {
    const { result } = await mount(
      modelsResponse(
        [mk("gone-used", { available: false }), mk("recent-ok"), mk("first-ok")],
        { recent: [mk("recent-ok")], last_used: "gone-used" }
      )
    );
    await waitFor(() => expect(result.current.selectedModel).toBe("recent-ok"));
  });

  it("falls back to a valid recent model when last_used does not exist in the catalog", async () => {
    const { result } = await mount(
      modelsResponse([mk("recent-ok"), mk("first-ok")], {
        recent: [mk("recent-ok")],
        last_used: "ghost-model",
      })
    );
    await waitFor(() => expect(result.current.selectedModel).toBe("recent-ok"));
  });

  it("falls back to the first selectable model when last_used is invalid and no usable recent exists", async () => {
    const { result } = await mount(
      modelsResponse(
        [mk("excluded-used", { excluded: true }), mk("first-ok"), mk("second-ok")],
        { recent: [mk("excluded-used")], last_used: "excluded-used" }
      )
    );
    await waitFor(() => expect(result.current.selectedModel).toBe("first-ok"));
  });

  it("keeps selectedModel null when last_used is invalid and nothing is selectable", async () => {
    const { result } = await mount(
      modelsResponse(
        [
          mk("excluded-used", { excluded: true }),
          mk("unavailable-1", { available: false }),
        ],
        { recent: [mk("excluded-used")], last_used: "excluded-used" }
      )
    );
    await waitFor(() => expect(getModelsMock).toHaveBeenCalled());
    await act(async () => {});
    expect(result.current.selectedModel).toBeNull();
  });

  it("prefers a valid last_used over recent and first selectable", async () => {
    const { result } = await mount(
      modelsResponse([mk("first-ok"), mk("used-ok"), mk("recent-ok")], {
        recent: [mk("recent-ok")],
        last_used: "used-ok",
      })
    );
    await waitFor(() => expect(result.current.selectedModel).toBe("used-ok"));
  });

  it("retains a manual valid selection across refresh even when last_used differs", async () => {
    let data = modelsResponse([mk("manual-ok"), mk("used-ok")], {
      last_used: "used-ok",
    });
    getModelsMock.mockImplementation(() => Promise.resolve(data));
    const { result } = renderHook(() => useModels());
    await act(async () => {});
    await waitFor(() => expect(result.current.selectedModel).toBe("used-ok"));

    act(() => result.current.setSelectedModel("manual-ok"));
    data = modelsResponse([mk("manual-ok"), mk("used-ok")], {
      last_used: "other-used",
    });
    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.selectedModel).toBe("manual-ok");
  });

  it("re-selects a remaining selectable model after the selected last-used model is excluded", async () => {
    let data = modelsResponse([mk("used-1"), mk("other-1")], {
      last_used: "used-1",
    });
    getModelsMock.mockImplementation(() => Promise.resolve(data));
    const { result } = renderHook(() => useModels());
    await act(async () => {});
    await waitFor(() => expect(result.current.selectedModel).toBe("used-1"));

    data = modelsResponse([mk("used-1", { excluded: true }), mk("other-1")], {
      last_used: "used-1",
    });
    await act(async () => {
      await result.current.excludeModel("used-1");
    });
    expect(excludeModelMock).toHaveBeenCalledWith("used-1");
    await waitFor(() => expect(result.current.selectedModel).toBe("other-1"));
  });

  it("filters unavailable or excluded entries out of the raw recent list", async () => {
    const { result } = await mount(
      modelsResponse([mk("recent-ok"), mk("gone", { available: false }), mk("hidden", { excluded: true })], {
        recent: [mk("gone", { available: false }), mk("hidden", { excluded: true }), mk("recent-ok"), mk("ghost")],
        last_used: "recent-ok",
      })
    );
    await waitFor(() => expect(result.current.selectedModel).toBe("recent-ok"));
    expect(result.current.recentModels.map((m) => m.id)).toEqual(["recent-ok"]);
  });
});
