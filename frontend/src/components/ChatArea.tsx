import { type RefObject, useMemo, useRef } from "react";
import type { ConversationCostEstimate, UIMessage, ReasoningStep } from "../types";
import type { SessionStatus } from "../hooks/useChatSession";
import { buildChatTimeline } from "../historyTimeline";
import { formatTokenRate } from "../formatTokenRate";
import { MessageItem, ToolCallCard, classifyCompactionMessage } from "./MessageItem";
import { ReasoningPanel } from "./ReasoningPanel";
import { ConversationCost } from "./ConversationCost";
import { Brain, Loader2, Clock, X } from "lucide-react";

interface ChatAreaProps {
  messages: UIMessage[];
  reasoningSteps: ReasoningStep[];
  status: SessionStatus;
  messagesEndRef: RefObject<HTMLDivElement>;
  scrollContainerRef?: RefObject<HTMLDivElement>;
  onCancel?: () => void;
  costEstimate: ConversationCostEstimate | null;
}

function formatTimestamp(timestamp: string | null): string {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

type TurnEstimate = ConversationCostEstimate & { turn_index: number };

interface TurnBoxMatch {
  estimate: TurnEstimate;
  promptNumber: number;
}

/**
 * Index the backend's per-turn cost breakdown by `turn_index` so a turn's
 * estimate can be looked up in O(1) when rendering the box that precedes
 * the next user prompt.
 */
function buildTurnCostMap(
  turns: TurnEstimate[] | undefined
): Map<number, TurnEstimate> {
  const map = new Map<number, TurnEstimate>();
  if (!turns) return map;
  for (const turn of turns) {
    if (!map.has(turn.turn_index)) map.set(turn.turn_index, turn);
  }
  return map;
}

/**
 * For each real user prompt in the timeline, determine which per-turn cost
 * box (if any) should be rendered *before* it.
 *
 * Persisted prompts carry the backend's main-chain turn index, so branch
 * users and compactor prompts cannot shift the cost boxes. Live drafts and
 * older history responses fall back to counting real prompts locally.
 * A box always reflects the previous real prompt's turn; the first prompt
 * has no prior cost box. All inferences still contribute to the footer.
 */
function buildTurnBoxMap(
  timeline: ReturnType<typeof buildChatTimeline>,
  turnCostMap: Map<number, TurnEstimate>
): Map<string, TurnBoxMatch> {
  const result = new Map<string, TurnBoxMatch>();
  let turnIndex = 0;
  let realPromptCount = 0;
  let lastReal: { turnIndex: number; promptNumber: number } | null = null;
  for (const item of timeline) {
    if (item.kind !== "message" || item.message.role !== "user") continue;
    const isCompactor = classifyCompactionMessage(item.message.content ?? "") !== null;
    if (isCompactor || item.message.turnIndex === 0) continue;
    const nextTurn = item.message.turnIndex ?? turnIndex + 1;
    // A replayed copy of a main prompt must not advance the display twice.
    if (nextTurn <= turnIndex) continue;
    turnIndex = nextTurn;
    realPromptCount += 1;
    if (lastReal) {
      const estimate = turnCostMap.get(lastReal.turnIndex);
      if (estimate) {
        result.set(item.key, { estimate, promptNumber: lastReal.promptNumber });
      }
    }
    lastReal = { turnIndex, promptNumber: realPromptCount };
  }
  return result;
}

export function ChatArea({
  messages,
  reasoningSteps,
  status,
  messagesEndRef,
  scrollContainerRef,
  onCancel,
  costEstimate,
}: ChatAreaProps) {
  const localScrollRef = useRef<HTMLDivElement>(null);
  const scrollRef = scrollContainerRef ?? localScrollRef;
  const isStreaming = status === "streaming" || status === "cancelling";
  const isQueued = status === "queued";
  const isLoading = status === "loading_history" || status === "resuming";
  const isReadOnly = status === "paused_readonly";
  const timeline = useMemo(
    () => buildChatTimeline(messages, reasoningSteps, { liveTurn: isStreaming }),
    [messages, reasoningSteps, isStreaming]
  );
  // The live turn is split into per-event blocks (one per tool call or text
  // block), all flagged isStreaming until the turn ends. Only the trailing
  // one shows the "Streaming" indicator.
  const activeStreamKey = useMemo(() => {
    for (let index = timeline.length - 1; index >= 0; index -= 1) {
      const item = timeline[index];
      if (item.kind === "message" && item.message.isStreaming) return item.key;
    }
    return null;
  }, [timeline]);
  const turnCostMap = useMemo(
    () => buildTurnCostMap(costEstimate?.turns),
    [costEstimate]
  );
  const turnBoxMap = useMemo(
    () => buildTurnBoxMap(timeline, turnCostMap),
    [timeline, turnCostMap]
  );
  const reasoningThroughput = reasoningSteps.find(
    (step) => step.is_throughput_sample && !step.isLive
  )?.metrics.tokens_per_sec;

  return (
    <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4">
      <div className="mx-auto max-w-4xl space-y-3">
        {timeline.length === 0 && !isStreaming && !isLoading && !isReadOnly && (
          <div className="flex flex-col items-center justify-center py-20 text-center text-gray-600">
            <p className="text-sm">Send a prompt to start a conversation</p>
            <p className="mt-1 text-xs text-gray-700">
              Or select an existing session from the sidebar
            </p>
          </div>
        )}

        {timeline.length === 0 && isLoading && (
          <div className="flex items-center justify-center gap-2 py-20 text-sm text-gray-500">
            <Loader2 className="h-4 w-4 animate-spin" />
            <span>{status === "loading_history" ? "Loading history…" : "Resuming session…"}</span>
          </div>
        )}

        {timeline.length === 0 && isReadOnly && (
          <div className="py-20 text-center text-sm text-amber-400">
            This session is read-only — it could not be resumed.
          </div>
        )}

        {timeline.map((item) =>
          item.kind === "message" ? (
            <div key={item.key} className="space-y-3">
              {item.message.role === "user" && (() => {
                const match = turnBoxMap.get(item.key);
                if (!match) return null;
                return (
                  <ConversationCost estimate={match.estimate} />
                );
              })()}
              <MessageItem message={item.message} isActiveStream={item.key === activeStreamKey} />
            </div>
          ) : (
            <div key={item.key} className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-3">
              <div className="pt-1 text-right text-[11px] text-gray-700">
                {formatTimestamp(item.step.timestamp)}
              </div>
              <div className="min-w-0 space-y-2">
                <ReasoningPanel steps={[item.step]} />
                {item.toolCalls.length > 0 && (
                  <div className="space-y-1.5">
                    {item.toolCalls.map((toolCall) => (
                      <ToolCallCard key={toolCall.id} tc={toolCall} />
                    ))}
                  </div>
                )}
              </div>
            </div>
          )
        )}

        {isStreaming && (
          <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-3">
            <div />
            <div className="flex items-center gap-2 text-sm text-gray-500">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              <span>{status === "cancelling" ? "Stopping…" : "Devin is working…"}</span>
            </div>
          </div>
        )}

        {isQueued && (
          <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-3">
            <div />
            <div className="flex items-center justify-between gap-2 text-sm text-amber-400">
              <div className="flex items-center gap-2">
                <Clock className="h-3.5 w-3.5" />
                <span>Waiting for connection.</span>
              </div>
              {onCancel && (
                <button
                  onClick={onCancel}
                  aria-label="Cancel queued prompt"
                  className="flex items-center gap-1 rounded-md bg-amber-600/20 px-2 py-1 text-xs font-medium text-amber-300 hover:bg-amber-600/30"
                >
                  <X className="h-3 w-3" /> Cancel
                </button>
              )}
            </div>
          </div>
        )}

        {!isStreaming && !isQueued && !isLoading && reasoningThroughput != null &&
          Number.isFinite(reasoningThroughput) && reasoningThroughput > 0 && (
            <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-3">
              <div />
              <div
                className="flex items-center gap-1.5 text-xs text-purple-400"
                title="Largest measured reasoning block in the latest turn: output tokens / full inference time, including time to first token"
              >
                <Brain className="h-3 w-3" />
                <span>Reasoning · {formatTokenRate(reasoningThroughput)} tok/s</span>
              </div>
            </div>
          )}
        {!isStreaming && !isQueued && timeline.length > 0 && <ConversationCost estimate={costEstimate} />}
        <div ref={messagesEndRef} />
      </div>
    </div>
  );
}
