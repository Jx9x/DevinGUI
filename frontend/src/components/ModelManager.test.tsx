import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ModelInfo } from "../types";
import { ModelManager } from "./ModelManager";

afterEach(cleanup);

const lightning: ModelInfo = {
  id: "swe-2-high-lightning",
  display_name: "SWE-2 High Lightning",
  family: "swe-2",
  family_name: "SWE-2",
  vision: false,
  tags: [],
  description: "",
  is_new: false,
  available: true,
  excluded: false,
  reasoning: "high",
  is_fast: true,
  input_price: 1,
  cached_input_price: 0.1,
  output_price: 5,
};

describe("ModelManager", () => {
  it("shows Lightning and preserves its include/exclude controls while hiding other Fast models", () => {
    const fast = {
      ...lightning,
      id: "claude-fable-5-1-high-fast",
      display_name: "Claude Fable 5.1 High Fast",
      family: "claude-fable-5-1",
    };
    const props = {
      models: [lightning, fast],
      hasNewModels: false,
      onExclude: vi.fn(),
      onInclude: vi.fn(),
      onExcludeMany: vi.fn(),
      onIncludeMany: vi.fn(),
      onAcknowledge: vi.fn(),
      onClose: vi.fn(),
    };
    const { rerender } = render(<ModelManager {...props} />);
    expect(screen.getByText(lightning.display_name)).toBeTruthy();
    expect(screen.queryByText(fast.display_name)).toBeNull();
    fireEvent.click(screen.getByTitle("Exclude from model selector"));
    expect(props.onExclude).toHaveBeenCalledWith(lightning.id);

    rerender(<ModelManager {...props} models={[{ ...lightning, excluded: true }, fast]} />);
    expect(screen.getByText("Excluded Models (1)")).toBeTruthy();
    fireEvent.click(screen.getByTitle("Include in model selector"));
    expect(props.onInclude).toHaveBeenCalledWith(lightning.id);
    fireEvent.change(screen.getByPlaceholderText("Search models..."), {
      target: { value: "lightning" },
    });
    expect(within(screen.getByRole("dialog")).getByText(lightning.display_name)).toBeTruthy();
  });
});
