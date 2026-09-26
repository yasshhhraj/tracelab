export const ACTIVE_STATUSES = new Set([
  "CREATED", "CONTEXT_LOADING", "INVESTIGATING", "VERIFYING", "ARBITRATING",
]);

export function label(value: string): string {
  return value.replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function timestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function message(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}
