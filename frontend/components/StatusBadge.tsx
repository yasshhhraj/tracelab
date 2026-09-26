import { label } from "@/lib/format";

export function StatusBadge({ status }: { status: string }) {
  const tone = status === "VERIFIED" || status === "APPROVED" ? "success"
    : status === "REJECTED" || status === "FAILED" ? "danger"
    : status === "BLOCKED" ? "warning"
    : status === "WAITING_FOR_REVIEW" ? "review" : "progress";
  return <span className={`status-badge status-${tone}`}><span className="status-dot" />{label(status)}</span>;
}
