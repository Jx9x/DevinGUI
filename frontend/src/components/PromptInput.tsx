import { useState, useRef, useEffect, type ReactNode } from "react";
import { Send, Square, X, Image as ImageIcon } from "lucide-react";
import type { ImageAttachment } from "../types";
import { readPromptDraft, savePromptDraft } from "../promptPersistence";

interface PromptInputProps {
  draftSessionId: string | null;
  onSend: (text: string, images?: ImageAttachment[]) => boolean;
  onCancel: () => void;
  isStreaming: boolean;
  isQueued?: boolean;
  disabled: boolean;
  placeholder?: string;
  onDraftChange?: () => void;
  /** Optional compact element rendered to the right of the textarea (e.g. UsageBox). */
  aside?: ReactNode;
  /** Optional element rendered to the left of the textarea. */
  left?: ReactNode;
}

function fileToAttachment(file: File): Promise<ImageAttachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result as string;
      const comma = dataUrl.indexOf(",");
      const header = dataUrl.slice(0, comma);
      const data = dataUrl.slice(comma + 1);
      const match = header.match(/^data:([^;]+)/);
      resolve({ mimeType: match ? match[1] : file.type, data });
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

export function PromptInput({
  draftSessionId,
  onSend,
  onCancel,
  isStreaming,
  isQueued = false,
  disabled,
  placeholder,
  onDraftChange,
  aside,
  left,
}: PromptInputProps) {
  const [text, setText] = useState(() => readPromptDraft(draftSessionId));
  const [draftSaveFailed, setDraftSaveFailed] = useState(false);
  const [attachments, setAttachments] = useState<ImageAttachment[]>([]);
  const hasSavedDraftRef = useRef(Boolean(readPromptDraft(draftSessionId)));
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const savedText = readPromptDraft(draftSessionId);
    setText(savedText);
    hasSavedDraftRef.current = Boolean(savedText);
    setAttachments([]);
    setDraftSaveFailed(false);
  }, [draftSessionId]);

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [text]);

  const addAttachment = async (file: File) => {
    try {
      const attachment = await fileToAttachment(file);
      setAttachments((prev) => [...prev, attachment]);
    } catch {
      // ignore read errors
    }
  };

  const removeAttachment = (index: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  };

  const handleSend = () => {
    const hasInput = text.trim() || attachments.length;
    if (!hasInput || isStreaming || isQueued || disabled) return;
    const sent = onSend(text, attachments);
    if (sent) {
      setText("");
      if (draftSessionId) {
        const saved = savePromptDraft(draftSessionId, "");
        setDraftSaveFailed(!saved);
        if (saved && hasSavedDraftRef.current) {
          hasSavedDraftRef.current = false;
          onDraftChange?.();
        }
      }
      setAttachments([]);
    }
  };

  const handleTextChange = (value: string) => {
    setText(value);
    if (draftSessionId) {
      const saved = savePromptDraft(draftSessionId, value);
      setDraftSaveFailed(!saved);
      if (saved && hasSavedDraftRef.current !== Boolean(value)) {
        hasSavedDraftRef.current = Boolean(value);
        onDraftChange?.();
      }
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey && !e.altKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    for (const file of files) {
      if (file.type.startsWith("image/")) {
        await addAttachment(file);
      }
    }
    e.target.value = "";
  };

  const handlePaste = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = Array.from(e.clipboardData.items).filter(
      (item) => item.kind === "file" && item.type.startsWith("image/")
    );
    const fallbackFiles =
      items.length === 0
        ? Array.from(e.clipboardData.files).filter((file) =>
            file.type.startsWith("image/")
          )
        : [];

    if (items.length === 0 && fallbackFiles.length === 0) return;

    e.preventDefault();

    for (const item of items) {
      const file = item.getAsFile();
      if (file) await addAttachment(file);
    }
    for (const file of fallbackFiles) {
      await addAttachment(file);
    }
  };

  const hasInput = text.trim() || attachments.length;

  return (
    <div className="border-t border-border bg-bg-secondary px-4 py-3">
      <div className="mx-auto flex max-w-4xl flex-wrap items-end gap-3">
        {left}
        <div className="relative min-w-[180px] flex-1 rounded-lg border border-border bg-bg-tertiary focus-within:border-accent">
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-2 border-b border-border px-4 py-2">
              {attachments.map((a, i) => (
                <div key={i} className="relative">
                  <img
                    src={`data:${a.mimeType};base64,${a.data}`}
                    alt=""
                    className="h-16 w-auto rounded-md object-cover"
                  />
                  <button
                    type="button"
                    onClick={() => removeAttachment(i)}
                    aria-label="Remove image"
                    className="absolute -right-1 -top-1 flex h-4 w-4 items-center justify-center rounded-full bg-gray-700 text-white hover:bg-gray-600"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => handleTextChange(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            disabled={disabled}
            placeholder={
              placeholder
                ? placeholder
                : disabled
                  ? "Create or select a session to start chatting..."
                  : "Write your prompt here... (Enter to send, Shift+Enter for newline, Ctrl+V to paste image)"
            }
            rows={1}
            className="block w-full resize-none bg-transparent px-4 py-3 text-sm text-gray-100 placeholder-gray-600 focus:outline-hidden disabled:cursor-not-allowed"
          />
          {draftSaveFailed && (
            <p role="status" className="px-4 text-xs text-amber-300">
              Draft could not be saved in this browser.
            </p>
          )}
          <div className="flex items-center justify-between px-3 py-2">
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={disabled}
                aria-label="Attach image"
                className="flex items-center gap-1 rounded-md p-1.5 text-xs text-gray-500 hover:bg-bg-hover hover:text-gray-300 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <ImageIcon className="h-3.5 w-3.5" /> Attach
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                multiple
                onChange={handleFileChange}
                className="hidden"
              />
              {text.length > 0 && (
                <span className="text-xs text-gray-700">{text.length} chars</span>
              )}
            </div>
            <div className="flex gap-2">
              {isStreaming ? (
                <button
                  onClick={onCancel}
                  aria-label="Cancel streaming"
                  className="flex items-center gap-1.5 rounded-md bg-red-600/20 px-3 py-1.5 text-xs font-medium text-red-400 hover:bg-red-600/30"
                >
                  <Square className="h-3 w-3 fill-current" /> Cancel
                </button>
              ) : isQueued ? (
                <button
                  onClick={onCancel}
                  aria-label="Cancel queued prompt"
                  className="flex items-center gap-1.5 rounded-md bg-amber-600/20 px-3 py-1.5 text-xs font-medium text-amber-300 hover:bg-amber-600/30"
                >
                  <X className="h-3 w-3" /> Cancel
                </button>
              ) : (
                <button
                  onClick={handleSend}
                  aria-label="Send prompt"
                  disabled={!hasInput || disabled}
                  className="flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Send className="h-3 w-3" /> Send
                </button>
              )}
            </div>
          </div>
        </div>
        {aside}
      </div>
    </div>
  );
}
