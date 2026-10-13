import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PromptInput } from "./PromptInput";
import { readPromptDraft } from "../promptPersistence";

beforeEach(() => window.localStorage.clear());
afterEach(cleanup);

describe("prompt draft recovery", () => {
  it("saves each conversation's text immediately and restores it after remount", () => {
    const onSend = vi.fn(() => false);
    const props = { onSend, onCancel: vi.fn(), isStreaming: false, disabled: false };
    const { rerender, unmount } = render(<PromptInput key="session-a" draftSessionId="session-a" {...props} />);

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Unfinished prompt for A" } });
    expect(readPromptDraft("session-a")).toBe("Unfinished prompt for A");

    rerender(<PromptInput key="session-b" draftSessionId="session-b" {...props} />);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Draft for B" } });

    unmount();
    render(<PromptInput draftSessionId="session-a" {...props} />);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Unfinished prompt for A");
    expect(readPromptDraft("session-b")).toBe("Draft for B");
  });

  it("keeps failed sends and clears a draft only after an accepted send", () => {
    const onSend = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<PromptInput draftSessionId="session-a" onSend={onSend} onCancel={vi.fn()} isStreaming={false} disabled={false} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Send me" } });

    fireEvent.click(screen.getByRole("button", { name: "Send prompt" }));
    expect(readPromptDraft("session-a")).toBe("Send me");
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Send me");

    fireEvent.click(screen.getByRole("button", { name: "Send prompt" }));
    expect(readPromptDraft("session-a")).toBe("");
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
  });
});

describe("Enter-to-send behavior", () => {
  const baseProps = {
    onCancel: vi.fn(),
    isStreaming: false,
    disabled: false,
  };

  it("ignores an Enter keydown that is part of IME composition", () => {
    const onSend = vi.fn(() => true);
    render(<PromptInput draftSessionId="session-a" onSend={onSend} {...baseProps} />);
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "途中のテキスト" } });

    const notPrevented = fireEvent.keyDown(textarea, { key: "Enter", isComposing: true });
    expect(notPrevented).toBe(true);
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea.value).toBe("途中のテキスト");
    expect(readPromptDraft("session-a")).toBe("途中のテキスト");
  });

  it("sends the exact text and images on a non-composing Enter and clears on an accepted send", async () => {
    const onSend = vi.fn(() => true);
    render(<PromptInput draftSessionId="session-a" onSend={onSend} {...baseProps} />);
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "with image" } });
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(["x"], "a.png", { type: "image/png" })] },
    });
    await waitFor(() => expect(document.querySelectorAll("img")).toHaveLength(1));

    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("with image", [
      expect.objectContaining({ mimeType: "image/png" }),
    ]);
    expect(textarea.value).toBe("");
    expect(readPromptDraft("session-a")).toBe("");
  });

  it("does not send on Shift+Enter or Alt+Enter", () => {
    const onSend = vi.fn(() => true);
    render(<PromptInput draftSessionId="session-a" onSend={onSend} {...baseProps} />);
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "draft text" } });

    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(textarea, { key: "Enter", altKey: true });
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea.value).toBe("draft text");
    expect(readPromptDraft("session-a")).toBe("draft text");
  });

  it("retains the text and persisted draft when a plain Enter send is not accepted", () => {
    const onSend = vi.fn(() => false);
    render(<PromptInput draftSessionId="session-a" onSend={onSend} {...baseProps} />);
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "keep me" } });

    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("keep me", []);
    expect(textarea.value).toBe("keep me");
    expect(readPromptDraft("session-a")).toBe("keep me");
  });
});
