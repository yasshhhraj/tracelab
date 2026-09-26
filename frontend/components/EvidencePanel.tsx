"use client";

import useSWR from "swr";
import { api } from "@/lib/api";
import { message, timestamp } from "@/lib/format";
import type { HypothesisEvidence, TestEvidence } from "@/lib/types";
import { DiffViewer } from "./DiffViewer";

function Result({ value }: { value: string | null }) {
  const tone = value === "PASS" ? "proof-pass" : value === "FAIL" ? "proof-fail" : "proof-unknown";
  return <span className={`proof-result ${tone}`}>{value || "Not recorded"}</span>;
}

function TestProof({ evidence }: { evidence: TestEvidence }) {
  return <div className="test-proof">
    {evidence.test_path && <div className="proof-path" title={evidence.test_path}>{evidence.test_path}</div>}
    <div className="proof-grid">
      <div><span className="proof-label">Before fix</span><Result value={evidence.pre_fix_result} /></div>
      <div><span className="proof-label">After fix</span><Result value={evidence.post_fix_result} /></div>
      <div><span className="proof-label">Existing suite</span><Result value={evidence.existing_suite_result} /></div>
    </div>
    {evidence.runs > 0 && <p className="muted proof-meta">{evidence.runs} recorded run{evidence.runs === 1 ? "" : "s"} · failures before {evidence.failures_before} · failures after {evidence.failures_after}</p>}
  </div>;
}

export function EvidencePanel({ hypothesisId, active }: { hypothesisId: string; active: boolean }) {
  const { data, error, isLoading, mutate } = useSWR<HypothesisEvidence>(
    ["evidence", hypothesisId], () => api.evidence(hypothesisId),
    { refreshInterval: active ? 3000 : 0 },
  );
  if (isLoading) return <div className="inline-state" role="status">Loading evidence…</div>;
  if (error) return <div className="inline-state inline-error" role="alert">Evidence unavailable: {message(error)} <button className="text-button" onClick={() => void mutate()}>Retry</button></div>;
  if (!data) return null;

  const reproduced = data.experiments.some((experiment) =>
    (experiment.command.startsWith("reproduce:") || experiment.command === "reproduce_full_suite")
    && experiment.exit_code !== null && experiment.exit_code !== 0 && !experiment.timed_out,
  );

  return <div className="evidence-content">
    <div className="reproduction-line"><span className="proof-label">Reproduction</span><span className={reproduced ? "proof-result proof-fail" : "proof-result proof-unknown"}>{reproduced ? "Observed failure" : "Not recorded"}</span></div>
    <div className="subsection"><h4>Test proof <span className="section-count">{data.test_evidence.length}</span></h4>
      {data.test_evidence.length ? data.test_evidence.map((item) => <TestProof key={item.id} evidence={item} />) : <p className="muted">No test results recorded.</p>}
    </div>
    <div className="subsection"><h4>Experiments <span className="section-count">{data.experiments.length}</span></h4>
      {data.experiments.length ? <div className="experiment-list">{data.experiments.map((item) => <details key={item.id} className="experiment">
        <summary><span className="experiment-command">{item.command}</span><span className="experiment-meta">{item.timed_out ? "Timed out" : `Exit ${item.exit_code ?? "—"}`} · {item.duration_ms} ms</span></summary>
        <div className="experiment-body"><p className="muted">{timestamp(item.created_at)}</p>{item.stdout && <><span className="proof-label">stdout</span><pre>{item.stdout}</pre></>}{item.stderr && <><span className="proof-label">stderr</span><pre>{item.stderr}</pre></>}{!item.stdout && !item.stderr && <p className="muted">No output recorded.</p>}</div>
      </details>)}</div> : <p className="muted">No experiments recorded.</p>}
    </div>
    <div className="subsection"><h4>Recorded patch <span className="section-count">{data.patches.length}</span></h4>
      {data.patches.length ? data.patches.map((patch) => <div key={patch.id} className="patch-record">
        <div className="patch-meta"><span>{patch.branch}</span>{patch.files_changed.length > 0 && <span>{patch.files_changed.length} changed file{patch.files_changed.length === 1 ? "" : "s"}</span>}</div>
        <DiffViewer diff={patch.diff} />
      </div>) : <p className="muted">No recorded patch diff.</p>}
    </div>
  </div>;
}
