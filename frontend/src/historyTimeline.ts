import type { ReasoningStep, UIMessage, UIToolCall } from "./types";

export type ChatTimelineItem =
  | { kind: "message"; key: string; message: UIMessage; timestamp: number; sequence: number }
  | {
      kind: "reasoning";
      key: string;
      step: ReasoningStep;
      toolCalls: UIToolCall[];
      timestamp: number;
      sequence: number;
    };

export interface BuildChatTimelineOptions {
  /**
   * True while a Devin turn is in flight. Transcript `step.message` values are
   * persistence fallbacks, not a second live-output channel, so they must not
   * be synthesized into the timeline while ACP/DB streaming is active.
   */
  liveTurn?: boolean;
}

function parseTimestamp(value: string | null): number {
  if (!value) return Number.POSITIVE_INFINITY;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY;
}

function normalizeContent(value: string): string {
  return value
    .normalize("NFKC")
    .replace(/[\u200B-\u200D\uFEFF]/g, "")
    .trim()
    .replace(/\s+/g, " ");
}

/**
 * A presentation-insensitive comparison key for correlating the same assistant
 * output after it has travelled through ACP, SQLite and the ATIF transcript.
 * Those paths can differ in line wrapping and Markdown decoration even when
 * they represent the same text.
 */
function comparisonKey(value: string): string {
  return normalizeContent(value)
    .toLowerCase()
    // Markdown presentation characters frequently differ between the DB and
    // transcript forms. Treat them as decoration for dedup/correlation.
    .replace(/[`*_~#>|\\]/g, "")
    // Ignore punctuation/line-wrapping differences while preserving words and
    // numbers. Unicode property escapes are supported by the ES2020 target.
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim()
    .replace(/\s+/g, " ");
}

function contentMatches(left: string, right: string, allowPrefix: boolean): boolean {
  const normalizedLeft = normalizeContent(left);
  const normalizedRight = normalizeContent(right);
  if (!normalizedLeft || !normalizedRight) return false;
  if (normalizedLeft === normalizedRight) return true;

  const leftKey = comparisonKey(normalizedLeft);
  const rightKey = comparisonKey(normalizedRight);
  if (!leftKey || !rightKey) return false;
  if (leftKey === rightKey) return true;

  if (!allowPrefix) return false;
  const shorterLength = Math.min(leftKey.length, rightKey.length);
  return (
    shorterLength >= 24 &&
    (leftKey.startsWith(rightKey) || rightKey.startsWith(leftKey))
  );
}

function toolCallsForStep(
  step: ReasoningStep,
  stepIndex: number,
  existingToolCallIds: ReadonlySet<string>
): UIToolCall[] {
  return step.tool_calls
    .map(
      (call, callIndex): UIToolCall => ({
        id: call.tool_call_id || `history-step-${step.step_id ?? stepIndex}-tool-${callIndex}`,
        title: call.function_name || "Tool call",
        kind: "tool",
        status: "completed",
        content:
          call.arguments && Object.keys(call.arguments).length > 0
            ? [{ text: JSON.stringify(call.arguments, null, 2) }]
            : undefined,
      })
    )
    .filter((call) => !existingToolCallIds.has(call.id));
}

interface AssistantCandidate {
  messageIndex: number;
  message: UIMessage;
  timestamp: number;
  sequence: number;
}

function closestCandidate(
  candidates: AssistantCandidate[],
  stepTimestamp: number
): AssistantCandidate | undefined {
  if (candidates.length === 0) return undefined;
  if (!Number.isFinite(stepTimestamp)) return candidates[candidates.length - 1];
  return candidates.reduce((best, candidate) => {
    const bestDistance = Math.abs(best.timestamp - stepTimestamp);
    const candidateDistance = Math.abs(candidate.timestamp - stepTimestamp);
    return candidateDistance < bestDistance ? candidate : best;
  });
}

function findMatchingAssistantMessage(
  finalMessage: string,
  stepTimestamp: number,
  candidates: AssistantCandidate[],
  usedMessageIndices: ReadonlySet<number>
): AssistantCandidate | undefined {
  const available = candidates.filter(
    (candidate) => !usedMessageIndices.has(candidate.messageIndex)
  );

  // Prefer a full persisted/live equality match. Repeated short responses such
  // as "Done." can occur in multiple turns, so choose the timestamp-nearest
  // matching assistant row rather than blindly taking the latest one.
  const exact = available.filter((candidate) =>
    contentMatches(candidate.message.content, finalMessage, false)
  );
  const exactMatch = closestCandidate(exact, stepTimestamp);
  if (exactMatch) return exactMatch;

  // During streaming the browser may only have a prefix of the transcript/DB
  // message. Prefix correlation is safe for ordering/dedup as long as the
  // shared normalized text is substantial.
  const prefix = available.filter((candidate) =>
    contentMatches(candidate.message.content, finalMessage, true)
  );
  return closestCandidate(prefix, stepTimestamp);
}

/**
 * Merge persisted chat messages with transcript reasoning by timestamp.
 * Transcript tool invocations are emitted as sibling tool-call cards rather
 * than being nested inside the purple reasoning panel. Missing final agent
 * messages may be supplemented from `step.message` only after a turn is idle.
 *
 * A transcript step that contains `step.message` is causally tied to that
 * assistant output. When the corresponding UI message exists, anchor the
 * reasoning immediately before it instead of sorting the two independently by
 * timestamps from different persistence paths. This prevents the last
 * reasoning block from appearing below the final summary.
 */
export function buildChatTimeline(
  messages: UIMessage[],
  reasoningSteps: ReasoningStep[],
  options: BuildChatTimelineOptions = {}
): ChatTimelineItem[] {
  const messageItems: Extract<ChatTimelineItem, { kind: "message" }>[] = messages.map(
    (message, index) => ({
      kind: "message",
      key: `message-${message.id}`,
      message,
      timestamp: parseTimestamp(message.timestamp),
      // Leave room directly before every message for a paired reasoning step.
      sequence: (message.streamOrder ?? index) * 100 +
        (message.role === "user" ? 20 : message.role === "tool" ? 60 : 80),
    })
  );
  const items: ChatTimelineItem[] = [...messageItems];

  const assistantCandidates: AssistantCandidate[] = messageItems
    .map((item, messageIndex) => ({ item, messageIndex }))
    .filter(({ item }) => item.message.role === "assistant" && item.message.content.trim())
    .map(({ item, messageIndex }) => ({
      messageIndex,
      message: item.message,
      timestamp: item.timestamp,
      sequence: item.sequence,
    }));
  const usedAssistantMessageIndices = new Set<number>();
  const existingAssistantContent = assistantCandidates.map((candidate) => candidate.message.content);
  const existingToolCallIds = new Set(
    messages.flatMap((message) => (message.toolCalls ?? []).map((toolCall) => toolCall.id))
  );

  const liveTurnActive =
    options.liveTurn ??
    (messages.some((message) => message.isStreaming) || reasoningSteps.some((step) => step.isLive));

  // Correlate transcript steps to assistant messages first, before emitting
  // any timeline items. This gives earlier reasoning/tool steps access to the
  // next causally-related assistant answer when clocks from SQLite and the
  // transcript disagree.
  const stepMatches: (AssistantCandidate | undefined)[] = [];
  reasoningSteps.forEach((step) => {
    const finalMessage = step.message.trim();
    const matched = finalMessage
      ? findMatchingAssistantMessage(
          finalMessage,
          parseTimestamp(step.timestamp),
          assistantCandidates,
          usedAssistantMessageIndices
        )
      : undefined;
    if (matched) usedAssistantMessageIndices.add(matched.messageIndex);
    stepMatches.push(matched);
  });

  // For an unmatched reasoning/tool step, remember the next later transcript
  // step that *is* paired to an assistant message. Normally the step's own
  // timestamp is earlier and no adjustment is needed. If its completion clock
  // sorts it after that answer, clamp it immediately before the answer instead
  // of rendering reasoning/tools below the final summary.
  const nextMatchedAnchors: ({ candidate: AssistantCandidate; stepIndex: number } | undefined)[] =
    new Array(reasoningSteps.length);
  let nextMatched: { candidate: AssistantCandidate; stepIndex: number } | undefined;
  for (let index = reasoningSteps.length - 1; index >= 0; index -= 1) {
    nextMatchedAnchors[index] = nextMatched;
    const matched = stepMatches[index];
    if (matched) nextMatched = { candidate: matched, stepIndex: index };
  }

  reasoningSteps.forEach((step, index) => {
    const finalMessage = step.message.trim();
    const stepTimestamp = parseTimestamp(step.timestamp);
    const matchedAssistant = stepMatches[index];
    const futureAnchor = nextMatchedAnchors[index];

    let timestamp = matchedAssistant ? matchedAssistant.timestamp : stepTimestamp;
    let sequence = matchedAssistant
      ? matchedAssistant.sequence - 0.5
      : (step.streamOrder ?? index) * 100 + 40;

    if (
      !matchedAssistant &&
      futureAnchor &&
      Number.isFinite(futureAnchor.candidate.timestamp) &&
      (!Number.isFinite(stepTimestamp) || stepTimestamp >= futureAnchor.candidate.timestamp)
    ) {
      timestamp = futureAnchor.candidate.timestamp;
      // Keep all skewed precursor steps in transcript order, immediately
      // before the matched final reasoning step and its assistant message.
      sequence =
        futureAnchor.candidate.sequence -
        1 -
        (futureAnchor.stepIndex - index) * 0.001;
    }

    items.push({
      kind: "reasoning",
      key: `reasoning-${step.step_id ?? "unknown"}-${index}`,
      step,
      toolCalls: toolCallsForStep(step, index, existingToolCallIds),
      timestamp,
      sequence,
    });

    // `step.message` is a history/persistence fallback only. During ANY live
    // turn, ACP and SQLite are the authoritative visible-output channels. Do
    // not guess whether a transcript step is "old" or "current" using DB/user
    // timestamps: Devin can persist internal user-role nodes mid-turn, which
    // is exactly what the supplied debug trace showed. Once the turn is idle,
    // synthesize only genuinely missing assistant output.
    const finalAlreadyPresent =
      Boolean(matchedAssistant) ||
      existingAssistantContent.some((content) => contentMatches(content, finalMessage, true));
    if (finalMessage && !liveTurnActive && !finalAlreadyPresent) {
      existingAssistantContent.push(finalMessage);
      items.push({
        kind: "message",
        key: `transcript-message-${step.step_id ?? "unknown"}-${index}`,
        message: {
          id: `transcript-message-${step.step_id ?? "unknown"}-${index}`,
          role: "assistant",
          content: finalMessage,
          timestamp: step.timestamp,
          isStreaming: false,
        },
        timestamp: parseTimestamp(step.timestamp),
        sequence: sequence + 0.5,
      });
    }
  });

  return items.sort((left, right) => {
    if (left.timestamp !== right.timestamp) return left.timestamp - right.timestamp;
    return left.sequence - right.sequence;
  });
}
