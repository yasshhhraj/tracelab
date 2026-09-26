import type { Hypothesis } from "@/lib/types";
import { label } from "@/lib/format";
import { EvidencePanel } from "./EvidencePanel";
import { StatusBadge } from "./StatusBadge";

export function HypothesisCard({ hypothesis, index, active, selected }: { hypothesis: Hypothesis; index: number; active: boolean; selected: boolean }) {
  return <article className={`hypothesis-card ${selected ? "hypothesis-selected" : ""}`}>
    <div className="hypothesis-top"><span className="hypothesis-number">H{index + 1}</span><span className="agent-name">{label(hypothesis.agent_type)}</span><StatusBadge status={hypothesis.status} />{selected && <span className="selected-label">Selected diagnosis</span>}</div>
    <h3>{hypothesis.summary}</h3>
    {hypothesis.reasoning_summary && <p className="hypothesis-reasoning">{hypothesis.reasoning_summary}</p>}
    {hypothesis.suspected_files.length > 0 && <div className="file-tags" aria-label="Suspected files">{hypothesis.suspected_files.map((file) => <code key={file}>{file}</code>)}</div>}
    {hypothesis.candidate_fix && <details className="proposed-fix"><summary>Proposed fix</summary><pre>{hypothesis.candidate_fix}</pre></details>}
    <div className="hypothesis-evidence"><h4>Evidence</h4><EvidencePanel hypothesisId={hypothesis.id} active={active} /></div>
  </article>;
}
