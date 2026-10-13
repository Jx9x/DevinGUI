import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, ApiError } from "../api/client";
import { ExportButton } from "./ExportButton";
import type { ExportPreviewResponse } from "../types";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function openExport() {
  vi.spyOn(api, "previewExport").mockResolvedValue({
    markdown: "# Chat",
    counts: {
      base: 2,
      include_reasoning: 4,
      include_tool_calls: 5,
      include_tool_outputs: 7,
      include_summaries: 6,
      db_only: 20,
      total: 2,
    },
  });
  render(<ExportButton sessionId="s-1" defaultDirectory={"C:\\Projects"} title="Chat" />);
  fireEvent.click(screen.getByRole("button", { name: "Export conversation" }));
  await screen.findByText("≈20 tokens total");
}

describe("ExportButton", () => {
  it("creates a new Markdown export without requesting an overwrite", async () => {
    const exportSession = vi.spyOn(api, "exportSession").mockResolvedValue({
      ok: true, path: "C:\\Projects\\Chat.md", message: "Wrote export",
    });
    await openExport();

    fireEvent.click(screen.getByRole("button", { name: /^Export$/ }));

    await waitFor(() => expect(exportSession).toHaveBeenCalledWith("s-1", expect.objectContaining({
      output: "C:\\Projects\\Chat.md", overwrite: false,
    })));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("requires an explicit overwrite click after a destination conflict", async () => {
    const exportSession = vi.spyOn(api, "exportSession")
      .mockRejectedValueOnce(new ApiError("export session failed: 409 Conflict", 409))
      .mockResolvedValueOnce({ ok: true, path: "C:\\Projects\\Chat.md", message: "Wrote export" });
    await openExport();

    fireEvent.click(screen.getByRole("button", { name: /^Export$/ }));
    const overwrite = await screen.findByRole("button", { name: "Overwrite file" });
    expect(screen.getByRole("alert").textContent).toContain("C:\\Projects\\Chat.md");
    expect(exportSession).toHaveBeenCalledTimes(1);
    expect(exportSession).toHaveBeenLastCalledWith("s-1", expect.objectContaining({ overwrite: false }));

    fireEvent.click(overwrite);

    await waitFor(() => expect(exportSession).toHaveBeenCalledTimes(2));
    expect(exportSession).toHaveBeenLastCalledWith("s-1", expect.objectContaining({
      output: "C:\\Projects\\Chat.md", overwrite: true,
    }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("allows cancelling a conflict without replacing the file", async () => {
    const exportSession = vi.spyOn(api, "exportSession")
      .mockRejectedValue(new ApiError("destination exists", 409));
    await openExport();
    fireEvent.click(screen.getByRole("button", { name: /^Export$/ }));
    await screen.findByRole("button", { name: "Overwrite file" });

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(exportSession).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Export conversation" }));
    expect(screen.queryByRole("button", { name: "Overwrite file" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Export$/ }));
    await waitFor(() => expect(exportSession).toHaveBeenCalledTimes(2));
    expect(exportSession).toHaveBeenLastCalledWith("s-1", expect.objectContaining({ overwrite: false }));
  });

  it("clears overwrite confirmation when the destination changes", async () => {
    const exportSession = vi.spyOn(api, "exportSession")
      .mockRejectedValueOnce(new ApiError("destination exists", 409))
      .mockResolvedValueOnce({ ok: true, path: "C:\\Projects\\new.md", message: "Wrote export" });
    await openExport();
    fireEvent.click(screen.getByRole("button", { name: /^Export$/ }));
    await screen.findByRole("button", { name: "Overwrite file" });

    fireEvent.change(screen.getByLabelText("Output file"), { target: { value: "C:\\Projects\\new.md" } });

    expect(screen.queryByRole("button", { name: "Overwrite file" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Export$/ }));
    await waitFor(() => expect(exportSession).toHaveBeenLastCalledWith("s-1", expect.objectContaining({
      output: "C:\\Projects\\new.md", overwrite: false,
    })));
  });

  it("clears overwrite confirmation when the content selection changes", async () => {
    const exportSession = vi.spyOn(api, "exportSession")
      .mockRejectedValueOnce(new ApiError("destination exists", 409))
      .mockResolvedValueOnce({ ok: true, path: "C:\\Projects\\Chat.md", message: "Wrote export" });
    await openExport();
    fireEvent.click(screen.getByRole("button", { name: /^Export$/ }));
    await screen.findByRole("button", { name: "Overwrite file" });

    fireEvent.click(screen.getByRole("checkbox", { name: /Include reasoning/ }));

    expect(screen.queryByRole("button", { name: "Overwrite file" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Export$/ }));
    await waitFor(() => expect(exportSession).toHaveBeenLastCalledWith("s-1", expect.objectContaining({
      include_reasoning: true, overwrite: false,
    })));
  });

  it("does not offer overwrite confirmation for an unrelated export failure", async () => {
    const exportSession = vi.spyOn(api, "exportSession")
      .mockRejectedValue(new ApiError("export failed", 500));
    await openExport();
    fireEvent.click(screen.getByRole("button", { name: /^Export$/ }));

    await screen.findByText("export failed");
    expect(screen.queryByRole("button", { name: "Overwrite file" })).toBeNull();
    expect(exportSession).toHaveBeenCalledTimes(1);
  });

  it.each(["C:\\Projects\\application.py", "C:\\Projects\\saved.txt", "C:\\Projects\\saved.md.exe"])(
    "does not submit a non-Markdown destination: %s", async (output) => {
      const exportSession = vi.spyOn(api, "exportSession");
      await openExport();
      fireEvent.change(screen.getByLabelText("Output file"), { target: { value: output } });

      expect(screen.getByRole("button", { name: /^Export$/ })).toHaveProperty("disabled", true);
      expect(screen.getByText(/local \.md files/)).toBeTruthy();
      expect(exportSession).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: /Copy Markdown/ })).toHaveProperty("disabled", false);
    }
  );

  it("shows option and total counts and copies the selected Markdown without saving a file", async () => {
    const previewExport = vi.spyOn(api, "previewExport").mockImplementation(async (_sessionId, options) => {
      const expanded = options.include_tool_outputs;
      return {
        markdown: expanded ? "# Chat\n\nTool output" : "# Chat",
        counts: {
          base: 2,
          include_reasoning: 4,
          include_tool_calls: expanded ? 3 : 5,
          include_tool_outputs: expanded ? 2 : 7,
          include_summaries: 6,
          db_only: 20,
          total: expanded ? 7 : 2,
        },
      } satisfies ExportPreviewResponse;
    });
    const exportSession = vi.spyOn(api, "exportSession");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(<ExportButton sessionId="s-1" defaultDirectory="C:\\Projects" title="Chat" />);
    fireEvent.click(screen.getByRole("button", { name: "Export conversation" }));
    await waitFor(() => expect(screen.getByText("Selected total").parentElement?.textContent).toContain("≈2 tokens"));
    expect(screen.getByText("≈20 tokens total")).toBeTruthy();

    fireEvent.click(screen.getByRole("checkbox", { name: /Include tool outputs/ }));
    expect(screen.getByRole("checkbox", { name: /Include tool calls/ })).toHaveProperty("checked", true);
    await waitFor(() => expect(screen.getByText("Selected total").parentElement?.textContent).toContain("≈7 tokens"));
    expect(previewExport).toHaveBeenLastCalledWith("s-1", expect.objectContaining({
      include_tool_calls: true,
      include_tool_outputs: true,
    }));

    fireEvent.change(screen.getByLabelText("Output file"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /Copy Markdown/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("# Chat\n\nTool output"));
    expect(exportSession).not.toHaveBeenCalled();
    expect(screen.getByText("Markdown copied to clipboard.")).toBeTruthy();
  });
});
