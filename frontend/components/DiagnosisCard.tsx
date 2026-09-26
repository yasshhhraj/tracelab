import type { Diagnosis } from "@/lib/types";
import { label } from "@/lib/format";

export function DiagnosisCard({ diagnosis }: { diagnosis: Diagnosis | null }) {
  if (!diagnosis) return <section className="panel diagnosis-panel"><div className="panel-heading"><div><div className="eyebrow">CONCLUSION</div><h2>Diagnosis</h2></div></div><div className="empty-section">Diagnosis pending. The arbiter has not recorded a conclusion yet.</div></section>;

  return <section className="panel diagnosis-panel" aria-labelledby="diagnosis-title">
    <div className="panel-heading"><div><div className="eyebrow">CONCLUSION</div><h2 id="diagnosis-title">Diagnosis</h2></div><span className={`risk-badge risk-${diagnosis.risk.toLowerCase()}`}>{label(diagnosis.risk)} risk</span></div>
    <div className="diagnosis-body">
      <p className="diagnosis-summary">{diagnosis.summary}</p>
      <div className="diagnosis-facts"><div><span className="field-label">Verified cause</span><p>{diagnosis.selected_hypothesis_id ? (diagnosis.verified_cause || "Not recorded") : "No verified cause selected"}</p></div><div><span className="field-label">Recommended action</span><p>{diagnosis.recommended_action || "Not recorded"}</p></div></div>
      {diagnosis.evidence.length > 0 && <div className="subsection"><h3>Supporting evidence</h3><ul className="evidence-bullets">{diagnosis.evidence.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
      {diagnosis.changed_files.length > 0 && <div className="subsection"><h3>Changed files</h3><div className="file-tags">{diagnosis.changed_files.map((file) => <code key={file}>{file}</code>)}</div></div>}
      {diagnosis.rejected_hypotheses.length > 0 && <div className="subsection"><h3>Other hypotheses</h3><div className="rejected-list">{diagnosis.rejected_hypotheses.map((item) => <div key={item.hypothesis_id}><strong>{item.summary}</strong><p>{item.rejection_reason}</p></div>)}</div></div>}
    </div>
  </section>;
}
