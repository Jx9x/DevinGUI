import { Activity, Zap, Database, Coins, BadgeCheck } from "lucide-react";
import type { ConversationCostEstimate, MessageMetrics } from "../types";

interface StatsBarProps {
  usage: { used?: number; size?: number; cost?: { amount: number; currency: string } };
  metrics: MessageMetrics;
  costEstimate: ConversationCostEstimate | null;
}

function formatTokens(n: number | undefined): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toString();
}

export function StatsBar({ usage, metrics, costEstimate }: StatsBarProps) {
  const hasStats = usage.used || metrics.input_tokens || metrics.output_tokens || costEstimate;

  if (!hasStats) {
    return <div className="border-b border-border bg-bg-secondary px-4 py-1.5" />;
  }

  return (
    <div className="flex items-center gap-4 border-b border-border bg-bg-secondary px-4 py-1.5 text-xs text-gray-500">
      {/* Context window usage */}
      {usage.used && usage.size && (
        <div className="flex items-center gap-1.5">
          <Activity className="h-3 w-3 text-blue-400" />
          <span>
            Context: {formatTokens(usage.used)} / {formatTokens(usage.size)}
          </span>
          <div className="ml-1 h-1.5 w-20 overflow-hidden rounded-full bg-bg-tertiary">
            <div
              className="h-full rounded-full bg-blue-500"
              style={{ width: `${Math.min((usage.used / usage.size) * 100, 100)}%` }}
            />
          </div>
        </div>
      )}

      {/* Input tokens */}
      {metrics.input_tokens && (
        <div className="flex items-center gap-1.5">
          <Database className="h-3 w-3 text-green-400" />
          <span>In: {formatTokens(metrics.input_tokens)}</span>
        </div>
      )}

      {/* Output tokens */}
      {metrics.output_tokens && (
        <div className="flex items-center gap-1.5">
          <Zap className="h-3 w-3 text-yellow-400" />
          <span>Out: {formatTokens(metrics.output_tokens)}</span>
        </div>
      )}

      {/* Cache read */}
      {metrics.cache_read_tokens && (
        <div className="flex items-center gap-1.5">
          <span className="text-gray-700">Cache: {formatTokens(metrics.cache_read_tokens)}</span>
        </div>
      )}

      {costEstimate && (
        <div className="ml-auto flex items-center gap-1.5 text-amber-300" title="Cost estimated locally from token counts × model pricing">
          <Coins className="h-3 w-3" />
          <span>Est. ${costEstimate.amount.toFixed(4)}</span>
        </div>
      )}

      {/* Cost reported by the agent via ACP usage_update — the official source */}
      {usage.cost && (
        <div
          className="ml-auto flex items-center gap-1.5"
          title="Cost reported by the Devin CLI via ACP usage_update"
        >
          <Coins className="h-3 w-3 text-amber-400" />
          <span>
            ${usage.cost.amount.toFixed(4)} {usage.cost.currency}
          </span>
          <BadgeCheck className="h-3 w-3 text-green-400" aria-label="official" />
        </div>
      )}
    </div>
  );
}
