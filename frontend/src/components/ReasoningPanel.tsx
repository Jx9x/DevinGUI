import { memo, useEffect, useRef, useState } from "react";
import { Brain, ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import type { ReasoningStep } from "../types";
import { formatTokenRate } from "../formatTokenRate";

interface ReasoningPanelProps {
  steps: ReasoningStep[];
}

const PREVIEW_CHARS = 150;

function ReasoningPanelImpl({ steps }: ReasoningPanelProps) {
  // A step is "live" while its tokens are still streaming in from ACP.
  const isLive = steps.some((step) => step.isLive);
  // Live reasoning opens automatically so a long thinking phase is visible
  // as it happens; an explicit toggle by the user always takes precedence.
  const [override, setOverride] = useState<boolean | null>(null);
  const expanded = override ?? isLive;
  const totalChars = steps.reduce((sum, step) => sum + step.reasoning_content.length, 0);
  const firstStep = steps[0];
  const throughput = !isLive && steps.length === 1 ? firstStep?.metrics.tokens_per_sec : undefined;
  const label = isLive
    ? "Thinking…"
    : steps.length === 1 && firstStep?.step_id != null
      ? `Reasoning · Step ${firstStep.step_id}`
      : `Reasoning (${steps.length} step${steps.length !== 1 ? "s" : ""})`;
  // While streaming, preview the most recent tokens rather than the first
  // ones — a fixed head slice never changes and looks frozen. When there are
  // multiple blocks, the live one is the last one still receiving chunks.
  const previewStep = isLive
    ? [...steps].reverse().find((s) => s.isLive) ?? firstStep
    : firstStep;
  const content = previewStep?.reasoning_content ?? "";
  const preview = isLive ? content.slice(-PREVIEW_CHARS) : content.slice(0, PREVIEW_CHARS);
  const truncated = content.length > preview.length;

  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (expanded && isLive && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [expanded, isLive, totalChars]);

  return (
    <div className="rounded-lg bg-purple-950/20">
      <button
        type="button"
        onClick={() => setOverride(!expanded)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 text-purple-400" />
        ) : (
          <ChevronRight className="h-4 w-4 text-purple-400" />
        )}
        {isLive ? (
          <Loader2 className="h-4 w-4 animate-spin text-purple-400" />
        ) : (
          <Brain className="h-4 w-4 text-purple-400" />
        )}
        <span className="text-sm font-medium text-purple-300">{label}</span>
        <span className="text-xs text-purple-700">
          ·{" "}
          {totalChars.toLocaleString()} chars
        </span>
        {throughput != null && Number.isFinite(throughput) && throughput > 0 && (
          <span
            className="text-xs text-purple-400"
            title="Output tokens / full inference time for this block, including time to first token"
          >
            · {formatTokenRate(throughput)} tok/s
          </span>
        )}
      </button>

      {!expanded && preview && (
        <div className="px-3 pb-2 pl-9 text-xs text-gray-600">
          {isLive && truncated ? "…" : ""}
          {preview}
          {!isLive && truncated ? "…" : ""}
        </div>
      )}

      {expanded && (
        <div
          ref={bodyRef}
          className={`space-y-3 px-3 py-2 ${isLive ? "max-h-80 overflow-y-auto" : ""}`}
        >
          {steps.map((step, index) => (
            <div key={`${step.step_id ?? "unknown"}-${index}`}>
              {steps.length > 1 && (
                <div className="mb-1 font-mono text-xs text-purple-400">
                  Step {step.step_id ?? index + 1}
                </div>
              )}
              <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-gray-400">
                {step.reasoning_content}
              </pre>
              {step.metrics && Object.keys(step.metrics).length > 0 && (
                <div className="mt-2 flex gap-3 text-xs text-gray-700">
                  {step.metrics.prompt_tokens != null && (
                    <span>prompt: {step.metrics.prompt_tokens.toLocaleString()}</span>
                  )}
                  {step.metrics.completion_tokens != null && (
                    <span title="Provider-reported output tokens for the whole inference. Reasoning tokens may be excluded.">
                      output: {step.metrics.completion_tokens.toLocaleString()}
                    </span>
                  )}
                  {step.metrics.cached_tokens != null && (
                    <span>cached: {step.metrics.cached_tokens.toLocaleString()}</span>
                  )}
                  {steps.length > 1 && !step.isLive && step.metrics.tokens_per_sec != null &&
                    Number.isFinite(step.metrics.tokens_per_sec) && step.metrics.tokens_per_sec > 0 && (
                    <span>{formatTokenRate(step.metrics.tokens_per_sec)} tok/s</span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export const ReasoningPanel = memo(
  ReasoningPanelImpl,
  (previous, next) =>
    previous.steps.length === next.steps.length &&
    previous.steps.every((step, index) => step === next.steps[index])
);
