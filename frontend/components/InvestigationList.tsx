"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";
import { ACTIVE_STATUSES, message, timestamp } from "@/lib/format";
import type { InvestigationList as InvestigationListData } from "@/lib/types";
import { StatusBadge } from "./StatusBadge";

const PAGE_SIZE = 20;

export function InvestigationList() {
  const [offset, setOffset] = useState(0);
  const { data, error, isLoading, isValidating, mutate } = useSWR<InvestigationListData>(
    ["investigations", offset],
    () => api.list(PAGE_SIZE, offset),
    { refreshInterval: (current) => current?.items.some((item) => ACTIVE_STATUSES.has(item.status)) ? 3000 : 0 },
  );

  const pageCount = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE));
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  if (data && offset > 0 && offset >= data.total) {
    setOffset(Math.max(0, (pageCount - 1) * PAGE_SIZE));
  }

  return (
    <main className="page-shell">
      <div className="page-heading">
        <div>
          <div className="eyebrow">WORKSPACE / INVESTIGATIONS</div>
          <h1>Investigations</h1>
          <p>Follow each investigation from first hypothesis through review.</p>
        </div>
        <button className="button button-secondary" onClick={() => void mutate()} disabled={isValidating}>
          <span aria-hidden="true">↻</span> {isValidating ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      <section className="panel list-panel" aria-labelledby="investigations-heading">
        <div className="panel-heading">
          <div><h2 id="investigations-heading">All investigations</h2><p>Most recent first</p></div>
          {data && <span className="count-pill">{data.total} total</span>}
        </div>
        {isLoading && <div className="state-message" role="status">Loading investigations…</div>}
        {error && <div className="state-message state-error" role="alert"><strong>Could not load investigations.</strong><span>{message(error)}</span><button className="text-button" onClick={() => void mutate()}>Try again</button></div>}
        {!isLoading && !error && data?.items.length === 0 && <div className="state-message"><strong>No investigations yet</strong><span>Investigations started through the API will appear here.</span></div>}
        {!error && data && data.items.length > 0 && (
          <>
            <div className="table-scroll"><table className="data-table">
              <thead><tr><th scope="col">Issue / symptom</th><th scope="col">Repository</th><th scope="col">Status</th><th scope="col">Created</th><th scope="col"><span className="sr-only">Open</span></th></tr></thead>
              <tbody>{data.items.map((item) => <tr key={item.id}>
                <td><Link className="issue-link" href={`/investigations/${encodeURIComponent(item.id)}`}>{item.external_issue_id}</Link><span className="table-subtitle">{item.bug_context?.symptom || "No symptom recorded"}</span></td>
                <td><span className="repo-name" title={item.repository}>{item.repository.split("/").filter(Boolean).at(-1) || item.repository}</span><span className="table-subtitle">{item.base_branch}</span></td>
                <td><StatusBadge status={item.status} /></td>
                <td><time dateTime={item.created_at} title={item.created_at}>{timestamp(item.created_at)}</time><span className="table-subtitle">Updated {timestamp(item.updated_at)}</span></td>
                <td><Link className="open-link" href={`/investigations/${encodeURIComponent(item.id)}`} aria-label={`Open ${item.external_issue_id}`}>↗</Link></td>
              </tr>)}</tbody>
            </table></div>
            <div className="pagination"><span>Page {page} of {pageCount}</span><div><button className="button button-secondary" onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0}>Previous</button><button className="button button-secondary" onClick={() => setOffset(offset + PAGE_SIZE)} disabled={offset + PAGE_SIZE >= data.total}>Next</button></div></div>
          </>
        )}
      </section>
    </main>
  );
}
