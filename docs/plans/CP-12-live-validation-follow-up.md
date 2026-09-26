# CP-12 live GitHub PR validation — deferred until after MVP deployment

**Status (2026-09-26):** Deferred. This is a post-MVP follow-up, not a blocker for continuing with CP-13 or deploying the MVP. Do not mark the live GitHub draft-PR acceptance criterion complete yet.

## What has been validated

- The CP-12 GitHub client, PR body, API route, approval gate, persistence, and idempotency paths are covered by mocked tests. The complete backend suite passed: **273 tests** (two pytest collection warnings).
- The configured GitHub token authenticated to `yasshhhraj/loreforge`; repository metadata reported push access. This was a read-only check. No branch or PR was created.
- The live Jira import for `KAN-4` created investigation `b5fe0b27-26a3-488b-8b0a-0e101ae39c7e`; TraceLab posted a diagnosis-summary comment back to Jira.
- A PR request for that unapproved investigation returned `409`. The endpoint now also rejects an approved investigation with no verified hypothesis or usable patch, and rejects a missing GitHub token instead of returning a placeholder URL.
- A local checkout can now resolve its GitHub `origin` for PR creation.

## Why the live PR test stopped

`KAN-4` is a placeholder issue, not a reproducible Loreforge defect. The investigation found no verified hypothesis or patch. Creating a PR for it would violate TraceLab's evidence contract. TraceLab's search and regression-test generation also currently target Python, while Loreforge's lobby implementation is TypeScript; supporting Loreforge end to end is separate work.

The CP-12 live acceptance criterion remains **unverified**: after a human approves a verified diagnosis, TraceLab must push the winning patch, create a real GitHub **draft** PR, persist and return its URL, and add the PR link to Jira. The CP-13 seeded demo uses a mocked PR and does not close this live criterion.

## Resume after MVP deployment

1. Use a dedicated test GitHub repository with a small, deterministic bug and a failing regression test. A Python fixture can validate CP-12 independently of TypeScript support. To use Loreforge instead, first add TypeScript-aware code search, test execution, and regression-test generation to TraceLab.
2. Give TraceLab repository-scoped access to push a branch and create a draft PR. Configure Jira and GitHub credentials server-side; never put tokens in an issue, prompt, log, or PR body.
3. Import the matching Jira issue and confirm one hypothesis meets all four verification conditions: successful reproduction, regression test **FAIL** before the fix, **PASS** after the fix, and relevant existing tests **PASS**.
4. Have a human review the diagnosis and call `POST /api/investigations/{id}/approve`. Only then call `POST /api/investigations/{id}/pull-request`.
5. Verify the returned URL is a real draft PR in the intended repository, the PR contains the tested diff and evidence, `Patch.pr_url` stores the same URL, a second request returns it without creating another PR, and Jira receives the PR-link comment. Confirm no credential appears in logs or responses. Do not auto-merge.

**Completion condition:** Record the test issue, investigation ID, draft PR URL, and verification evidence here after the live run succeeds; then mark CP-12 live validation complete.
