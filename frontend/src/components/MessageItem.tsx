import { memo, useState, useEffect } from "react";
import { Bot, Wrench, CheckCircle2, Loader2, Clock, ChevronDown, Copy, Check } from "lucide-react";
import type { UIMessage, DisplayImageAttachment, UIToolCall, ToolCallContent } from "../types";
import { isToolCallImage } from "../types";
import { formatTokenRate } from "../formatTokenRate";
import { externalImageUrl, safeImageSource } from "../imageSources";
import { ImageAttachments } from "./ImageAttachments";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

interface MessageItemProps {
  message: UIMessage;
  // True only for the trailing live block of the active turn, so the
  // "Streaming" indicator renders once rather than on every live block.
  isActiveStream?: boolean;
}

type CompactionMessageKind = "source" | "instruction" | "summary" | null;

type MarkdownNode = {
  type: string;
  value?: string;
  children?: MarkdownNode[];
  data?: { hProperties?: Record<string, unknown> };
};

function referenceAttributes(value: string): Record<string, string> {
  const attributes: Record<string, string> = {};
  const pattern = /([a-zA-Z]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(value))) {
    attributes[match[1].toLowerCase()] = match[2] ?? match[3] ?? "";
  }
  return attributes;
}

function remarkFileReferences() {
  return (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (!node.children) return;
      const children: MarkdownNode[] = [];
      for (const child of node.children) {
        if (child.type !== "text" || !child.value) {
          visit(child);
          children.push(child);
          continue;
        }

        const pattern = /<ref_(file|snippet)\b[^>]*?\/?\s*>/gi;
        let cursor = 0;
        let match: RegExpExecArray | null;
        while ((match = pattern.exec(child.value))) {
          const attributes = referenceAttributes(match[0]);
          const filePath = attributes.file;
          if (!filePath) continue;
          if (match.index > cursor) {
            children.push({ type: "text", value: child.value.slice(cursor, match.index) });
          }
          const filename = filePath.split(/[\\/]/).pop() || filePath;
          children.push({
            type: "inlineCode",
            value:
              match[1].toLowerCase() === "snippet" && attributes.lines
                ? `${filename}:${attributes.lines}`
                : filename,
            data: {
              hProperties: {
                className: "file-reference",
                title: filePath,
              },
            },
          });
          cursor = pattern.lastIndex;
        }
        children.push(cursor ? { type: "text", value: child.value.slice(cursor) } : child);
      }
      node.children = children;
    };
    visit(tree);
  };
}

function remarkSoftBreaks() {
  return (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (!node.children) return;
      const children: MarkdownNode[] = [];
      for (const child of node.children) {
        if (child.type === "text" && child.value?.includes("\n")) {
          child.value.split("\n").forEach((line, index) => {
            if (index) children.push({ type: "break" });
            if (line) children.push({ type: "text", value: line });
          });
        } else {
          visit(child);
          children.push(child);
        }
      }
      node.children = children;
    };
    visit(tree);
  };
}

export function classifyCompactionMessage(content: string): CompactionMessageKind {
  const trimmed = content.trim();
  if (/^Conversation to summarize:/i.test(trimmed)) return "source";
  if (/^Now summarize the conversation above as per the format given\./i.test(trimmed)) {
    return "instruction";
  }
  if (/^<summary>[\s\S]*<\/summary>$/i.test(trimmed)) return "summary";
  return null;
}

function formatTimestamp(timestamp: string | null): string {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      });
}

function markdownUrlTransform(url: string, key: string): string {
  if (key !== "src") return defaultUrlTransform(url);
  return safeImageSource(url) !== null || externalImageUrl(url) ? url : "";
}

function MarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  const safe = safeImageSource(src);
  if (safe !== null) return <img src={safe} alt={alt ?? ""} />;
  if (externalImageUrl(src)) {
    return (
      <a href={src} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">
        Open external image
      </a>
    );
  }
  return <>{alt}</>;
}

function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="message-markdown max-w-none break-words text-sm leading-relaxed text-gray-200">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkFileReferences, remarkSoftBreaks]}
        urlTransform={markdownUrlTransform}
        components={{ img: MarkdownImage }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(id);
  }, [copied]);

  const handleClick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // ignore
    }
  };

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={!text}
      aria-label={copied ? "Copied" : "Copy response"}
      title={copied ? "Copied" : "Copy response"}
      className="ml-auto flex items-center gap-1 rounded-sm p-1 text-gray-600 hover:bg-bg-hover hover:text-gray-300 disabled:opacity-40"
    >
      {copied ? <Check className="h-3 w-3 text-green-500" /> : <Copy className="h-3 w-3" />}
    </button>
  );
}

function ToolCallContentItem({ content }: { content: ToolCallContent }) {
  if (isToolCallImage(content)) return <ImageAttachments attachments={[content]} label="Viewed image" />;
  if (typeof content === "string") return content;
  if ("error" in content && typeof content.error === "string") return content.error;
  if ("text" in content && typeof content.text === "string") return content.text;
  return <pre className="whitespace-pre-wrap break-words">{JSON.stringify(content, null, 2)}</pre>;
}

function ToolCallCardImpl({ tc }: { tc: UIToolCall }) {
  const [expanded, setExpanded] = useState(false);
  const hasContent = Boolean(tc.content?.length);
  const images = tc.content?.filter(isToolCallImage) ?? [];
  const textContent = tc.content?.filter((item) => !isToolCallImage(item)) ?? [];
  const statusLabel = tc.status.replace(/_/g, " ");

  const statusIcon =
    tc.status === "completed" ? (
      <CheckCircle2 className="h-3.5 w-3.5 text-green-500/80" aria-label={statusLabel} />
    ) : tc.status === "in_progress" ? (
      <Loader2 className="h-3.5 w-3.5 animate-spin text-yellow-500/80" aria-label={statusLabel} />
    ) : (
      <Clock className="h-3.5 w-3.5 text-gray-600" aria-label={statusLabel} />
    );

  return (
    <div className="rounded-md bg-bg-tertiary px-3 py-2 text-gray-500">
      {hasContent ? (
        <button
          type="button"
          onClick={() => setExpanded((current) => !current)}
          aria-expanded={expanded}
          className="flex w-full select-none items-center gap-2 bg-transparent text-left text-xs"
        >
          <ChevronDown
            className={`h-3 w-3 text-gray-600 transition-transform ${expanded ? "rotate-0" : "-rotate-90"}`}
          />
          <Wrench className="h-3.5 w-3.5 text-gray-500" />
          <span className="min-w-0 flex-1 truncate font-medium text-gray-400">{tc.title}</span>
          {statusIcon}
        </button>
      ) : (
        <div className="flex items-center gap-2 text-xs">
          <Wrench className="h-3.5 w-3.5 text-gray-500" />
          <span className="min-w-0 flex-1 truncate font-medium text-gray-400">{tc.title}</span>
          {statusIcon}
        </div>
      )}
      {images.length > 0 && <ImageAttachments attachments={images} label="Viewed image" />}
      {expanded && textContent.length > 0 && (
        <div className="mt-2 max-h-72 overflow-y-auto pt-2 text-xs leading-relaxed text-gray-500">
          {textContent.map((content, index) => (
            <div key={index} className="whitespace-pre-wrap break-words">
              <ToolCallContentItem content={content} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export const ToolCallCard = memo(ToolCallCardImpl);

function ToolOutputCollapsible({ content, attachments }: { content: string; attachments: DisplayImageAttachment[] }) {
  const [expanded, setExpanded] = useState(false);
  const preview = content.slice(0, 180).replace(/\s+/g, " ").trim();

  return (
    <div className="rounded-md bg-bg-tertiary px-3 py-2 text-gray-500">
      <button
        type="button"
        onClick={() => setExpanded((current) => !current)}
        aria-expanded={expanded}
        className="flex w-full select-none items-center gap-2 bg-transparent text-left text-xs"
      >
        <ChevronDown
          className={`h-3 w-3 text-gray-600 transition-transform ${expanded ? "rotate-0" : "-rotate-90"}`}
        />
        <Wrench className="h-3.5 w-3.5 text-gray-500" />
        <span className="min-w-0 flex-1 truncate text-gray-500">
          {preview || (attachments.length ? "Viewed images" : "Details")}
          {!expanded && content.length > 180 ? "…" : ""}
        </span>
      </button>
      {attachments.length > 0 && <ImageAttachments attachments={attachments} label="Viewed image" />}
      {expanded && (
        <div className="mt-2 max-h-96 overflow-y-auto pt-2">
          <pre className="tool-output m-0 whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-gray-400">
            {content}
          </pre>
        </div>
      )}
    </div>
  );
}

function CompactionCollapsible({
  content,
  kind,
}: {
  content: string;
  kind: Exclude<CompactionMessageKind, null>;
}) {
  const [expanded, setExpanded] = useState(false);
  const labels: Record<Exclude<CompactionMessageKind, null>, string> = {
    source: "Conversation compaction source",
    instruction: "Conversation compaction instruction",
    summary: "Conversation compaction summary",
  };
  const displayContent =
    kind === "summary"
      ? content
          .trim()
          .replace(/^<summary>\s*/i, "")
          .replace(/\s*<\/summary>$/i, "")
      : content;

  return (
    <div className="rounded-lg bg-black/10">
      <button
        type="button"
        onClick={() => setExpanded((current) => !current)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <ChevronDown
          className={`h-3.5 w-3.5 text-gray-500 transition-transform ${expanded ? "rotate-0" : "-rotate-90"}`}
        />
        <span className="text-xs font-medium text-gray-400">{labels[kind]}</span>
        <span className="ml-auto text-[11px] text-gray-600">
          {content.length.toLocaleString()} chars
        </span>
      </button>
      {expanded && (
        <div className="max-h-[34rem] overflow-y-auto px-3 py-3">
          <MarkdownContent content={displayContent} />
        </div>
      )}
    </div>
  );
}

function MessageItemImpl({ message, isActiveStream }: MessageItemProps) {
  const isUser = message.role === "user";
  const isAssistant = message.role === "assistant";
  const isTool = message.role === "tool";
  const time = formatTimestamp(message.timestamp);
  const compactionKind = message.content ? classifyCompactionMessage(message.content) : null;
  const showAssistantIcon = isAssistant && Boolean(message.content.trim());

  const attachments = message.attachments || [];
  const messageBody =
    (message.content || attachments.length) && !isTool ? (
      compactionKind ? (
        <CompactionCollapsible content={message.content} kind={compactionKind} />
      ) : (
        <MarkdownContent content={message.content} />
      )
    ) : null;

  return (
    <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-3">
      <div className="flex min-h-7 flex-col items-end gap-1 pt-1 text-[11px] text-gray-700">
        {time && <time dateTime={message.timestamp ?? undefined}>{time}</time>}
        {showAssistantIcon && (
          <div className="flex h-6 w-6 items-center justify-center rounded-sm bg-purple-600/80">
            <Bot className="h-3.5 w-3.5 text-white" aria-label="Devin" />
          </div>
        )}
      </div>

      <div className="min-w-0">
        {message.isStreaming && isActiveStream && (
          <div className="mb-1 flex items-center gap-1 text-xs text-accent">
            <Loader2 className="h-3 w-3 animate-spin" />
            <span>Streaming</span>
          </div>
        )}

        {messageBody &&
          (isUser ? (
            <div className="rounded-xl bg-blue-500/10 px-4 py-3">
              {messageBody}
              {attachments.length > 0 && <ImageAttachments attachments={attachments} />}
            </div>
          ) : (
            <>{messageBody}{attachments.length > 0 && <ImageAttachments attachments={attachments} />}</>
          ))}

        {isTool && (message.content || attachments.length > 0) && <ToolOutputCollapsible content={message.content} attachments={attachments} />}

        {message.toolCalls && message.toolCalls.length > 0 && (
          <div className={`${messageBody ? "mt-2" : ""} space-y-1.5`}>
            {message.toolCalls.map((toolCall) => (
              <ToolCallCard key={toolCall.id} tc={toolCall} />
            ))}
          </div>
        )}

        {message.metrics && (message.metrics.input_tokens || message.metrics.output_tokens) && (
          <div className="mt-1.5 flex items-start justify-between text-xs text-gray-700">
            <div className="flex flex-wrap gap-x-3 gap-y-1">
              {message.metrics.input_tokens && (
                <span>in: {message.metrics.input_tokens.toLocaleString()}</span>
              )}
              {message.metrics.output_tokens && (
                <span>out: {message.metrics.output_tokens.toLocaleString()}</span>
              )}
              {message.metrics.tokens_per_sec && (
                <span title="Output tokens / full inference time, including time to first token">
                  {formatTokenRate(message.metrics.tokens_per_sec)} tok/s
                </span>
              )}
              {message.metrics.ttft_ms && <span>ttft: {message.metrics.ttft_ms}ms</span>}
            </div>
            <CopyButton text={message.content} />
          </div>
        )}
      </div>
    </div>
  );
}

export const MessageItem = memo(MessageItemImpl);
