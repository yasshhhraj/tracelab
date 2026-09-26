"use client";

import { useState } from "react";
import { useSWRConfig } from "swr";
import { api } from "@/lib/api";
import { message } from "@/lib/format";
import type { InvestigationDetail } from "@/lib/types";

export function ReviewActions({ investigation, refresh }: { investigation: InvestigationDetail; refresh: () => Promise<InvestigationDetail | undefined> }) {
  const { mutate } = useSWRConfig();
  const [pending, setPending] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [settled, setSettled] = useState(false);

  async function checkStatus() {
    try {
      const current = await refresh();
      if (current?.status === "WAITING_FOR_REVIEW") setSettled(false);
    } catch { /* Keep actions hidden until the current status is known. */ }
  }

  if (investigation.status !== "WAITING_FOR_REVIEW") {
    if (investigation.status === "APPROVED") return <section className="panel review-panel"><div><div className="eyebrow">HUMAN REVIEW</div><h2>Approval recorded</h2><p>Draft PR creation will be available after the GitHub integration is complete.</p></div></section>;
    if (investigation.status === "REJECTED") return <section className="panel review-panel"><div><div className="eyebrow">HUMAN REVIEW</div><h2>Diagnosis rejected</h2></div></section>;
    return null;
  }

  async function act(action: "approve" | "reject") {
    if (pending || settled) return;
    setPending(action);
    setError(null);
    try {
      if (action === "approve") await api.approve(investigation.id);
      else await api.reject(investigation.id);
      setSettled(true);
      await refresh();
      await mutate((key) => Array.isArray(key) && key[0] === "investigations");
    } catch (cause) {
      setError(message(cause));
      setSettled(true);
      await checkStatus();
    } finally {
      setPending(null);
    }
  }

  return <section className="panel review-panel" aria-labelledby="review-title">
    <div><div className="eyebrow">HUMAN REVIEW</div><h2 id="review-title">Review this diagnosis</h2><p>Read the recorded proof and conclusion before choosing an action.</p></div>
    {error && <div className="review-error" role="alert">{error} <button className="text-button" onClick={() => void checkStatus()}>Refresh status</button></div>}
    {settled ? <p className="muted" role="status">Review submitted. Refreshing current status…</p> : <div className="review-buttons">
      <button className="button button-secondary" disabled={!!pending} onClick={() => void act("reject")}>{pending === "reject" ? "Rejecting…" : "Reject diagnosis"}</button>
      <button className="button button-primary" disabled={!!pending} onClick={() => void act("approve")}>{pending === "approve" ? "Approving…" : "Approve diagnosis"}</button>
    </div>}
  </section>;
}
