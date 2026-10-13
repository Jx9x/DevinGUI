/** Keep small positive inference rates from rounding to zero. */
export function formatTokenRate(rate: number): string {
  if (rate < 0.01) return "<0.01";
  return `~${rate.toFixed(rate < 1 ? 2 : 0)}`;
}
