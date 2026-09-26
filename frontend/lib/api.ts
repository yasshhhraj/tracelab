import type {
  AgentEvent, Hypothesis, HypothesisEvidence, InvestigationDetail,
  InvestigationList, ReviewResponse,
} from "./types";

export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { cache: "no-store", ...init });
  if (!response.ok) {
    let detail: unknown;
    try { detail = (await response.json()).detail; } catch { /* Response may not be JSON. */ }
    throw new ApiError(typeof detail === "string" ? detail : `Request failed (${response.status})`, response.status);
  }
  return response.json() as Promise<T>;
}

const idPath = (id: string) => encodeURIComponent(id);

export const api = {
  list: (limit = 20, offset = 0) => request<InvestigationList>(`/investigations?limit=${limit}&offset=${offset}`),
  detail: (id: string) => request<InvestigationDetail>(`/investigations/${idPath(id)}`),
  hypotheses: (id: string) => request<Hypothesis[]>(`/investigations/${idPath(id)}/hypotheses`),
  events: (id: string) => request<AgentEvent[]>(`/investigations/${idPath(id)}/events`),
  evidence: (id: string) => request<HypothesisEvidence>(`/hypotheses/${idPath(id)}/evidence`),
  approve: (id: string) => request<ReviewResponse>(`/investigations/${idPath(id)}/approve`, { method: "POST" }),
  reject: (id: string) => request<ReviewResponse>(`/investigations/${idPath(id)}/reject`, { method: "POST" }),
};
