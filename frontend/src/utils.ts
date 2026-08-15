export function formatPercent(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return `${Math.round(value * 1000) / 10}%`;
}

export function formatNumber(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}
