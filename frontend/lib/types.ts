export type InvestigationStatus =
  | "CREATED" | "CONTEXT_LOADING" | "INVESTIGATING" | "VERIFYING"
  | "ARBITRATING" | "WAITING_FOR_REVIEW" | "APPROVED" | "REJECTED"
  | "FAILED" | "BLOCKED";

export type BugContext = Partial<{
  issue_id: string;
  symptom: string;
  expected: string;
  actual: string;
  affected_area: string;
  error_type: string;
  known_evidence: string[];
  stack_trace: string | null;
}>;

export interface Investigation {
  id: string;
  external_issue_id: string;
  repository: string;
  base_branch: string;
  status: InvestigationStatus | string;
  bug_context: BugContext | null;
  created_at: string;
  updated_at: string;
}

export interface InvestigationList {
  items: Investigation[];
  total: number;
  limit: number;
  offset: number;
}

export interface RejectedHypothesis {
  hypothesis_id: string;
  agent_type: string;
  summary: string;
  rejection_reason: string;
}

export interface Diagnosis {
  id: string;
  investigation_id: string;
  selected_hypothesis_id: string | null;
  summary: string;
  verified_cause: string;
  evidence: string[];
  rejected_hypotheses: RejectedHypothesis[];
  changed_files: string[];
  risk: string;
  recommended_action: string;
  created_at: string;
}

export interface InvestigationDetail extends Investigation {
  diagnosis: Diagnosis | null;
}

export interface Hypothesis {
  id: string;
  investigation_id: string;
  agent_type: string;
  summary: string;
  reasoning_summary: string;
  candidate_fix: string;
  suspected_files: string[];
  reproduction_plan: string[];
  confidence: string;
  status: string;
  created_at: string;
}

export interface Experiment {
  id: string;
  hypothesis_id: string;
  command: string;
  working_directory: string;
  exit_code: number | null;
  stdout: string;
  stderr: string;
  duration_ms: number;
  timed_out: boolean;
  created_at: string;
}

export interface TestEvidence {
  id: string;
  hypothesis_id: string;
  test_path: string;
  pre_fix_result: string | null;
  post_fix_result: string | null;
  existing_suite_result: string | null;
  runs: number;
  failures_before: number;
  failures_after: number;
  created_at: string;
}

export interface Patch {
  id: string;
  hypothesis_id: string;
  branch: string;
  commit_sha: string | null;
  diff: string;
  files_changed: string[];
  pr_url: string | null;
  created_at: string;
}

export interface HypothesisEvidence {
  hypothesis: Hypothesis;
  experiments: Experiment[];
  test_evidence: TestEvidence[];
  patches: Patch[];
}

export interface AgentEvent {
  id: string;
  investigation_id: string;
  agent: string;
  action: string;
  target: string | null;
  payload: Record<string, unknown> | null;
  timestamp: string;
}

export interface ReviewResponse {
  id: string;
  status: string;
}
