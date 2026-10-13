import { Coins } from "lucide-react";
import type { ConversationCostEstimate } from "../types";

interface ConversationCostProps {
  estimate: ConversationCostEstimate | null;
}

type ModelUsage = ConversationCostEstimate["models"][number];

function modelPricing(model: ModelUsage) {
  const unpriced = model.unpriced_steps ?? 0;
  const free = model.free_steps ?? (model.was_free ? 1 : 0);
  const paid = model.paid_steps ?? (!model.was_free && unpriced === 0 ? 1 : 0);
  const labels = [];
  if (free > 0) labels.push(paid > 0 || unpriced > 0 ? `${free} free` : "Free");
  if (paid > 0 && (free > 0 || unpriced > 0)) labels.push(`${paid} paid`);
  if (unpriced > 0) labels.push(`${unpriced} unpriced`);
  return {
    label: labels.join(" · "),
    showBreakdown: free === 0 && unpriced === 0,
    amount:
      unpriced > 0
        ? paid + free > 0
          ? `Partial ${formatPrice(model.amount)}`
          : "Unknown"
        : formatPrice(model.amount),
    showApi:
      model.hypothetical_amount > 0 && Math.abs(model.hypothetical_amount - model.amount) > 1e-9,
  };
}

/** Compact token formatting: 2,423 -> "2.423k", 3,289,210 -> "3.289M". */
export function formatTokenCount(count: number): string {
  if (count < 0) return formatTokenCount(0);
  if (count < 1_000) return String(count);
  if (count < 1_000_000) return `${(count / 1_000).toFixed(3)}k`;
  if (count < 1_000_000_000) return `${(count / 1_000_000).toFixed(3)}M`;
  return `${(count / 1_000_000_000).toFixed(3)}G`;
}

/** Price formatting with exactly three decimal places and a leading "$". */
export function formatPrice(amount: number): string {
  return `$${amount.toFixed(3)}`;
}

/** Render a single model's aggregated usage as one compact row. */
export function renderModelCostRow(model: ModelUsage): string {
  const inTokens = formatTokenCount(model.input_tokens);
  const cacheTokens = formatTokenCount(model.cached_input_tokens);
  const outTokens = formatTokenCount(model.output_tokens);
  const pricing = modelPricing(model);

  if (!pricing.showBreakdown) {
    const apiPart = pricing.showApi
      ? ` (API${model.hypothetical_estimated ? " est." : ""} ${formatPrice(model.hypothetical_amount)})`
      : "";
    return `${model.display_name} · In ${inTokens} · Cache ${cacheTokens} · Out ${outTokens} · ${pricing.label} · ${pricing.amount}${apiPart}`;
  }

  // Paid model: show per-category charged prices and the charged total.
  return (
    `${model.display_name} · ` +
    `In ${inTokens} (${formatPrice(model.input_amount)}) · ` +
    `Cache ${cacheTokens} (${formatPrice(model.cached_input_amount)}) · ` +
    `Out ${outTokens} (${formatPrice(model.output_amount)}) · ` +
    formatPrice(model.amount)
  );
}

export function ConversationCost({ estimate }: ConversationCostProps) {
  if (!estimate) return null;

  const models = estimate.models;
  const hasMultipleModels = models.length > 1;
  // Show the API-equivalent total only when it diverges from the charged total
  // (i.e. at least one free-on-plan model contributed an API estimate).
  const showApiTotal = Math.abs(estimate.hypothetical_amount - estimate.amount) > 1e-9;

  return (
    <section
      title="Local cost estimate from token usage and model prices"
      className="rounded-lg border border-border bg-bg-secondary px-3 py-1.5 text-xs text-gray-500"
    >
      <div className="flex flex-col gap-0.5">
        {models.length === 0 ? (
          // No priced models — fall back to a single combined line so the box
          // still surfaces the conversation-wide totals.
          <div className="flex items-center gap-1.5">
            <Coins className="h-3 w-3 text-amber-400" />
            <span className="font-mono text-amber-300">
              {estimate.priced_steps === 0 && estimate.unpriced_steps > 0
                ? "Unknown"
                : formatPrice(estimate.amount)}
            </span>
            <span>
              · In {formatTokenCount(estimate.input_tokens)} · Cache{" "}
              {formatTokenCount(estimate.cached_input_tokens)} · Out{" "}
              {formatTokenCount(estimate.output_tokens)}
            </span>
          </div>
        ) : (
          models.map((model) => {
            const pricing = modelPricing(model);
            return (
              <div key={model.model_id} className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0">
                <Coins className="hidden h-3 w-3 shrink-0 text-amber-400 sm:inline" />
                <span className="font-medium text-gray-200">{model.display_name}</span>
                <span className="text-gray-600">·</span>
                <span>
                  In <span className="text-gray-400">{formatTokenCount(model.input_tokens)}</span>
                  {pricing.showBreakdown && (
                    <span className="text-gray-600"> ({formatPrice(model.input_amount)})</span>
                  )}
                </span>
                <span className="text-gray-600">·</span>
                <span>
                  Cache{" "}
                  <span className="text-gray-400">
                    {formatTokenCount(model.cached_input_tokens)}
                  </span>
                  {pricing.showBreakdown && (
                    <span className="text-gray-600">
                      {" "}
                      ({formatPrice(model.cached_input_amount)})
                    </span>
                  )}
                </span>
                <span className="text-gray-600">·</span>
                <span>
                  Out <span className="text-gray-400">{formatTokenCount(model.output_tokens)}</span>
                  {pricing.showBreakdown && (
                    <span className="text-gray-600"> ({formatPrice(model.output_amount)})</span>
                  )}
                </span>
                <span className="text-gray-600">·</span>
                {pricing.label && <span className="text-gray-400">{pricing.label}</span>}
                <span className="font-mono font-medium text-amber-300">{pricing.amount}</span>
                {pricing.showApi && (
                  <span
                    className="text-gray-600"
                    title={
                      model.hypothetical_estimated
                        ? "Approximate API comparison using assumed token prices for free usage"
                        : undefined
                    }
                  >
                    (API{model.hypothetical_estimated ? " est." : ""}{" "}
                    <span className="text-gray-400">{formatPrice(model.hypothetical_amount)}</span>)
                  </span>
                )}
              </div>
            );
          })
        )}

        {hasMultipleModels && (
          <div className="mt-0.5 flex flex-wrap items-baseline gap-x-1.5 border-t border-border/60 pt-0.5 text-gray-400">
            <span className="font-medium text-gray-300">Estimated total</span>
            <span className="font-mono font-medium text-amber-300">
              {estimate.priced_steps === 0 && estimate.unpriced_steps > 0
                ? "Unknown"
                : formatPrice(estimate.amount)}
            </span>
            {showApiTotal && (
              <>
                <span className="text-gray-600">·</span>
                <span>{estimate.hypothetical_estimated ? "API estimate" : "API equivalent"}</span>
                <span className="font-mono text-gray-300">
                  {formatPrice(estimate.hypothetical_amount)}
                </span>
              </>
            )}
          </div>
        )}

        {!estimate.complete && (
          <p className="mt-1 text-amber-400">
            Partial estimate: {estimate.unpriced_steps} inference step
            {estimate.unpriced_steps === 1 ? " has" : "s have"} no available model price; excluded
            from dollar totals.
          </p>
        )}
      </div>
    </section>
  );
}
