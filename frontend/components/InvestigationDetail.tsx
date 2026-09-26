"use client";

import Link from "next/link";
import useSWR from "swr";
import { ApiError, api } from "@/lib/api";
import { ACTIVE_STATUSES, label, message, timestamp } from "@/lib/format";
import type { AgentEvent, Hypothesis, InvestigationDetail as DetailData } from "@/lib/types";
import { DiagnosisCard } from "./DiagnosisCard";
import { HypothesisCard } from "./HypothesisCard";
import { ReviewActions } from "./ReviewActions";
import { StatusBadge } from "./StatusBadge";

function DataSection<T>({ title, data, error, isLoading, retry, render, empty }: { title: string; data?: T[]; error: unknown; isLoading: boolean; retry: () => void; render: (items: T[]) => React.ReactNode; empty: string }) {
  return <section className="section-block" aria-label={title}>
    <div className="section-heading"><h2>{title}</h2>{data && <span className="section-count">{data.length}</span>}</div>
    {isLoading && <div className="inline-state" role="status">Loading {title.toLowerCase()}…</div>}
    {!!error && <div className="inline-state inline-error" role="alert">{message(error)} <button className="text-button" onClick={retry}>Retry</button></div>}
    {!isLoading && !error && data && (data.length ? render(data) : <div className="empty-section">{empty}</div>)}
  </section>;
}

export function InvestigationDetail({ id }: { id: string }) {
  const detail = useSWR<DetailData>(["investigation", id], () => api.detail(id), {
    refreshInterval: (current) => current && ACTIVE_STATUSES.has(current.status) ? 3000 : 0,
  });
  const active = !!detail.data && ACTIVE_STATUSES.has(detail.data.status);
  const hypotheses = useSWR<Hypothesis[]>(["hypotheses", id], () => api.hypotheses(id), { refreshInterval: active ? 3000 : 0 });
  const events = useSWR<AgentEvent[]>(["events", id], () => api.events(id), { refreshInterval: active ? 3000 : 0 });

  const refresh = async () => {
    const [current] = await Promise.all([detail.mutate(), hypotheses.mutate(), events.mutate()]);
    return current;
  };

  if (detail.isLoading) return <main className="page-shell"><div className="state-message" role="status">Loading investigation…</div></main>;
  if (detail.error instanceof ApiError && detail.error.status === 404) return <main className="page-shell"><Link href="/investigations" className="back-link">← All investigations</Link><div className="state-message"><strong>Investigation not found</strong><span>Check the link or return to the investigations list.</span></div></main>;
  if (detail.error || !detail.data) return <main className="page-shell"><Link href="/investigations" className="back-link">← All investigations</Link><div className="state-message state-error" role="alert"><strong>Could not load this investigation</strong><span>{message(detail.error)}</span><button className="text-button" onClick={() => void detail.mutate()}>Try again</button></div></main>;

  const investigation = detail.data;
  const bug = investigation.bug_context;
  const selectedId = investigation.diagnosis?.selected_hypothesis_id;

  return <main className="page-shell detail-page">
    <div className="detail-toolbar"><Link href="/investigations" className="back-link">← All investigations</Link><button className="button button-secondary" disabled={detail.isValidating} onClick={() => void refresh()}>{detail.isValidating ? "Refreshing…" : "↻ Refresh"}</button></div>
    <div className="detail-hero"><div className="eyebrow">INVESTIGATION / {investigation.external_issue_id}</div><div className="detail-title-row"><h1>{investigation.external_issue_id}</h1><StatusBadge status={investigation.status} /></div><p>{bug?.symptom || "No symptom recorded"}</p><div className="hero-meta"><span>Repository <strong>{investigation.repository}</strong></span><span>Base branch <strong>{investigation.base_branch}</strong></span><span>Created <time dateTime={investigation.created_at} title={investigation.created_at}>{timestamp(investigation.created_at)}</time></span></div></div>

    <div className="detail-grid"><div className="detail-main">
      <section className="panel context-panel" aria-labelledby="context-title"><div className="panel-heading"><div><div className="eyebrow">SOURCE TICKET</div><h2 id="context-title">Ticket context</h2></div></div>
        <div className="context-grid"><div><span className="field-label">Expected</span><p>{bug?.expected || "Not recorded"}</p></div><div><span className="field-label">Actual</span><p>{bug?.actual || "Not recorded"}</p></div>{bug?.affected_area && <div><span className="field-label">Affected area</span><p>{bug.affected_area}</p></div>}{bug?.error_type && <div><span className="field-label">Error type</span><p>{label(bug.error_type)}</p></div>}</div>
        {bug?.known_evidence && bug.known_evidence.length > 0 && <div className="context-extra"><span className="field-label">Known evidence</span><ul>{bug.known_evidence.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
        {bug?.stack_trace && <details className="context-extra"><summary>Stack trace</summary><pre className="stack-trace">{bug.stack_trace}</pre></details>}
      </section>

      <DataSection title="Hypotheses" data={hypotheses.data} error={hypotheses.error} isLoading={hypotheses.isLoading} retry={() => void hypotheses.mutate()} empty="No hypotheses recorded yet." render={(items) => <div className="hypotheses-list">{items.map((hypothesis, index) => <HypothesisCard key={hypothesis.id} hypothesis={hypothesis} index={index} active={active} selected={selectedId === hypothesis.id} />)}</div>} />
      <DiagnosisCard diagnosis={investigation.diagnosis} />
      <ReviewActions investigation={investigation} refresh={refresh} />
    </div><aside className="detail-aside">
      <DataSection title="Activity" data={events.data} error={events.error} isLoading={events.isLoading} retry={() => void events.mutate()} empty="No activity recorded yet." render={(items) => <ol className="timeline">{items.map((event) => <li key={event.id}><div className="timeline-dot" /><div className="timeline-body"><div><strong>{label(event.agent)}</strong><time dateTime={event.timestamp} title={event.timestamp}>{timestamp(event.timestamp)}</time></div><p>{label(event.action)}{event.target ? ` · ${event.target}` : ""}</p>{event.payload && Object.keys(event.payload).length > 0 && <details><summary>Details</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>}</div></li>)}</ol>} />
    </aside></div>
  </main>;
}
