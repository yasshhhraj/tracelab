# CP-10 — Frontend Dashboard

> **Goal:** Turn the existing Next.js scaffold into a usable investigation list and evidence-first review page.  
> **Builds on:** CP-09 REST API; CP-11 Jira intake and CP-12 GitHub integration are outside this checkpoint.  
> **Core rule:** Display recorded evidence, never model confidence or invented verification counts. Approval is a separate human action; it does not create a PR.

## Current state and boundaries

- `frontend/` already contains Next.js 16, React 19, TypeScript, and Tailwind. `app/page.tsx` is a placeholder; there are no dashboard routes, components, or API client. Extend this scaffold rather than running `create-next-app` again.
- The backend has paginated investigations, investigation detail with an optional diagnosis, hypotheses, per-hypothesis evidence, events, and approve/reject endpoints. `POST /pull-request` is a CP-12 stub that can return a `github.com/placeholder/...` URL. CP-10 must not call it or show a “Create PR” button.
- `bug_context` is nullable and free-form in the response. No UI should assume every ticket field is present.
- Patch records and unified diffs may be absent even when verification ran. Show the `candidate_fix` as a **proposed fix**, never as an applied patch; show “No recorded patch diff” when `patches` is empty.
- This dashboard targets the TraceLab backend. It does not run or display Loreforge's own test suite.

## User flow and pages

1. `/` redirects to `/investigations`. The shared shell carries the TraceLab name and a link back to the list; update the generated page metadata.
2. `/investigations` shows newest investigations in a responsive, paginated table: issue ID, symptom (or a fallback), repository/base branch, status, created/updated times, and a detail link. Use the API's `total`, `limit`, and `offset` for Previous/Next controls. Provide distinct loading, empty, error, and retry states.
3. `/investigations/[id]` shows the ticket context, current status, hypothesis cards, chronological agent events, evidence for each hypothesis, any recorded patch diff, the diagnosis, and review actions. Display a clear 404 state for unknown IDs and a recoverable error state for API failures.
4. During active states (`CREATED`, `CONTEXT_LOADING`, `INVESTIGATING`, `VERIFYING`, `ARBITRATING`), refresh the list and open detail every 3 seconds. Stop background polling in terminal/review states; provide a manual Refresh control. Revalidate relevant data immediately after review actions. Pause polling when the tab is hidden using SWR's visibility behavior.

### Detail layout and truthful labels

| Section | Source | Display rule |
|---|---|---|
| Ticket | `GET /api/investigations/{id}` | Issue ID, symptom, expected, actual, affected area, error type, repository and branch; hide absent optional fields. |
| Hypotheses | `GET /api/investigations/{id}/hypotheses` | Agent type, summary, status, reasoning, suspected files and proposed fix. Do not display or rank by `confidence`. |
| Timeline | `GET /api/investigations/{id}/events` | Time, agent, action, target; keep large payloads collapsed and render text safely. Empty timeline has an explicit message. |
| Evidence | `GET /api/hypotheses/{id}/evidence` | Show experiment commands, exit codes, timeout state, duration, and expandable stdout/stderr. Show test path, actual pre/post/existing result strings, runs, and failure counts when recorded. |
| Verification proof | Evidence response | Reproduction is “Observed failure” only if a non-timed-out `reproduce:` or `reproduce_full_suite` experiment has a nonzero exit code. Pre-fix, post-fix, and existing-suite chips use the recorded result strings (`FAIL`, `PASS`, etc.); missing values say “Not recorded.” A hypothesis is labeled VERIFIED only from its persisted `status`, not from a UI calculation. |
| Patch diff | `patches[].diff` | Render recorded unified diff with line coloring for additions, removals, and headers; preserve whitespace and allow horizontal scrolling. Empty patches/diffs have a clear empty state. Do not infer changed files from suspected files. |
| Diagnosis | `detail.diagnosis` | Show summary, verified cause, supporting evidence, risk, recommended action, selected hypothesis, changed files, and rejected hypotheses with reasons. A null diagnosis says “Diagnosis pending.” A null selected ID says “No verified cause selected.” |
| Review | `detail.status` + mutation endpoints | Only `WAITING_FOR_REVIEW` shows Approve and Reject. Each action calls its corresponding endpoint once, disables both buttons while pending, shows server errors (including 409 conflicts), then revalidates detail and list. `APPROVED` says approval is recorded; PR creation follows CP-12. |

Do not show a fabricated “existing tests 183/183” count: the API exposes a result string and intermittent-run counts, not a total existing-test count. Badge colors always include text and must not be the sole status cue. Long logs and diffs need bounded height or explicit expansion so the diagnosis and actions remain reachable.

## Data and integration design

- Add a small typed client in `frontend/lib/api.ts` with `frontend/lib/types.ts` matching `backend/app/schemas/investigation.py`: `InvestigationListResponse`, detail/diagnosis, hypothesis, evidence, event, and mutation responses. Preserve nullable fields. Centralize URL construction, JSON parsing, non-2xx errors, and the FastAPI `detail` message; never put server credentials in browser code.
- Use same-origin `/api/...` requests in the browser. Configure a Next.js rewrite in `frontend/next.config.ts` from `/api/:path*` to `${API_ORIGIN}/api/:path*`, with `API_ORIGIN=http://127.0.0.1:8000` for local development. The rewrite applies only to `/api`, so it does not intercept the dashboard routes. Document the override for a differently hosted backend. Backend CORS already allows `http://localhost:3000`; the rewrite avoids relying on browser cross-origin access.
- Add `swr` to the frontend dependencies and use client components for data loading, polling, and mutations. Keep `app/investigations/[id]/page.tsx` as a server route that awaits Next 16's Promise-valued `params` and passes the ID to a client detail component. Do not rely on older synchronous `params` examples.
- Fetch investigation, hypotheses, and events independently so one failure does not erase all detail. Fetch evidence for each displayed hypothesis with stable SWR keys; at most three hypotheses exist by project constraint. Show a per-section loading or error state and a retry control.
- Use stable query keys including `limit`/`offset`; keep pagination bounded to valid pages after a refreshed `total` changes. Use an explicit `fetch` cache policy appropriate for live data and avoid showing stale review buttons after a mutation or 409 response.
- Normalize status labels in one helper; unknown future status strings should still render safely. Format timestamps for the viewer, with the raw ISO value available in a `title` or accessible label.
- Keep arbitrary API text, logs, and diffs as escaped React text. Do not inject HTML. Never render environment variables, API keys, or full error traces into the page.

## Files and implementation order

| Step | Files | Result |
|---|---|---|
| 1. API bridge | `frontend/next.config.ts`, `frontend/lib/types.ts`, `frontend/lib/api.ts`, `frontend/package.json`, lockfile | Same-origin typed API access and SWR dependency. |
| 2. Shell and list | `frontend/app/layout.tsx`, `frontend/app/page.tsx`, `frontend/app/globals.css`, `frontend/app/investigations/page.tsx`, `frontend/components/InvestigationList.tsx`, `frontend/components/StatusBadge.tsx` | Navigable list with pagination, polling, and state handling. |
| 3. Detail and evidence | `frontend/app/investigations/[id]/page.tsx`, `frontend/components/InvestigationDetail.tsx`, `HypothesisCard.tsx`, `EvidencePanel.tsx`, `DiffViewer.tsx`, `DiagnosisCard.tsx` | Accurate ticket, timeline, hypothesis, proof, diff, and diagnosis views. |
| 4. Review controls | `frontend/components/ReviewActions.tsx` | Approve/reject gating, pending state, error handling, and refresh. |
| 5. Docs and checks | Frontend README or root README, focused UI tests if test tooling is added | Reproducible local setup and verified user flow. |

Component names are suggested boundaries, not a requirement to create one file per visual block. Keep the code small and preserve the repository's existing styling conventions.

## Local setup and verification

1. Start the existing PostgreSQL database and TraceLab FastAPI service using the project's local configuration. Run the frontend with `API_ORIGIN=http://127.0.0.1:8000 npm run dev` from `frontend/`. The browser visits `http://localhost:3000`. The dashboard itself needs no Jira, GitHub, or Bedrock key; starting a new live investigation through the backend still needs its configured model access.
2. Verify the backend `GET /health` and `/api/investigations`, then open the list and a known investigation. Confirm loading, empty, API failure, and 404 states. Confirm pagination and a status transition appear after polling or Refresh.
3. With a `WAITING_FOR_REVIEW` record, verify that Approve and Reject call only their respective endpoints, disable repeat submission, handle a 409 from a concurrent transition, and update the status. Use separate records for the two mutually exclusive actions. Check there is no PR creation call or placeholder GitHub link.
4. Check representative evidence: one hypothesis with persisted FAIL-before/PASS-after/existing PASS, one with incomplete or blocked evidence, one without a patch, and one with a recorded diff. Verify the UI never fabricates a result, patch, or test count.
5. Run `npm run lint`, `npx tsc --noEmit`, and `npm run build` in `frontend/`. If adding a test runner, include focused tests for the status/polling logic and the review mutation/error path, then run its test script. Run the backend suite if backend files change; CP-10 should normally need no backend changes.

## Acceptance criteria

- The existing Next.js scaffold serves `/investigations` and `/investigations/[id]` against the configured backend with no runtime errors.
- The list is paginated and shows live status updates every 3 seconds while investigations are active.
- Detail renders every available section and makes missing evidence, diagnosis, and patch data explicit.
- A reviewer can approve or reject only a `WAITING_FOR_REVIEW` investigation, with visible pending/success/error states and no duplicate submission.
- The UI does not expose model confidence as a decision signal, invent proof/counts, call the stub PR endpoint, or show a placeholder PR URL.
- Lint, TypeScript validation, production build, and the manual API-backed smoke flow pass.
