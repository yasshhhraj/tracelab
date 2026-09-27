# Domain-free hosted TraceLab migration

**Status (2026-09-27):** Chosen deployment direction. No Vercel, Railway, or Supabase resources have been created. The AWS TraceLab staging stack and its supporting resources were deleted at the owner's request; see the cleanup record below.

## Target layout

```text
Browser → Vercel Next.js (*.vercel.app) → authenticated /api proxy
       → Railway FastAPI (*.up.railway.app) → Supabase PostgreSQL
                                          → Railway persistent volume (Git checkout/worktrees)
                                          → Bedrock Mantle, Jira, GitHub
```

Bedrock's API key authenticates model calls only. Supabase requires its own PostgreSQL connection credential; neither credential belongs in the browser or Git. Use the existing FastAPI/SQLAlchemy models and Alembic migrations with Supabase PostgreSQL. Supabase Auth is optional as a product, but the public API needs a real authentication and authorization scheme before exposing approval, rejection, or PR creation.

## Next steps to first deployment

### 1. Accounts and non-secret inputs from the owner

- Create or select a **Supabase** organization and project, preferably in a region near the Railway service. Enable asymmetric Auth signing keys if using Supabase Auth. Record the project URL and connection method; store the actual database password only in the provider's secret settings.
- Create or select a **Railway** project for one backend service. Attach a persistent volume mounted at `/srv/tracelab/repos` and generate a Railway HTTPS domain only after API authentication is in place.
- Create or select a **Vercel** project linked to the TraceLab GitHub repository, with `frontend/` as Root Directory. It will receive a `*.vercel.app` URL; no DNS purchase is needed. Enable deployment protection during testing.
- Prepare a **separate disposable Python GitHub target repository** and a real matching Jira issue for the live PR test. Supply only their URL and issue key to the deployment process. Do not use Loreforge or KAN-4 for this first acceptance test.
- Keep the Bedrock Mantle, GitHub, Jira, and Supabase credentials in Railway/Vercel secret settings as applicable. Do not paste keys into chat, source files, GitHub issues, or Vercel public environment variables.

### 2. Code gate before public access

- Add login and API authorization. Recommended implementation: Supabase Auth sessions in Next.js, with FastAPI verifying the user JWT and allowing only explicitly approved reviewer identities to call `approve`, `reject`, or `pull-request`. Verify signature, issuer, audience, and expiry. Restrict other investigation routes to authenticated users. Jira's webhook must instead use its own HMAC check. Test direct calls to the Railway URL, since Vercel deployment protection does not cover them.
- Add a Railway startup path that checks/clones the allowlisted target repository into its runtime-mounted volume, then starts the single FastAPI process on Railway's configured `PORT`. Keep worktree parent paths stable. Prevent a redeploy from silently losing an in-progress investigation; record and recover or explicitly block interrupted work. Run tests for volume restart, job restart, authentication, and the approval gate.
- Make the Next.js `/api/*` proxy use the Railway HTTPS origin at deployment time. Keep browser requests same-origin and never expose the Bedrock key, GitHub token, Jira token, or DB password to the frontend.
- Run the backend test suite, CP-13 demo and benchmark, Ruff, frontend lint/build, Docker build, and a PostgreSQL migration smoke test. Keep the live CP-12 draft-PR check separate from scripted test results.

### 3. Deploy and verify in order

1. Push the local TraceLab commits to GitHub after review so Git-based deployments can see them. Set Railway's service root to `backend/`, where it will find `Dockerfile`; configure one replica, health path `/ready`, and the mounted volume.
2. Put the Supabase PostgreSQL URL in Railway's `DATABASE_URL` as a `postgresql+asyncpg://` URL with TLS. Prefer a dedicated app role and Supabase's direct endpoint if Railway can reach IPv6; otherwise use the IPv4 **session** pooler. Apply `alembic upgrade head` once against the new empty DB, then check `/ready` and an empty investigations list.
3. Configure Railway variables for `TARGET_REPOSITORY`, `LLM_MODEL=nvidia.nemotron-nano-3-30b`, `LLM_BASE_URL=https://bedrock-mantle.us-east-1.api.aws/v1`, the Bedrock key alias `AWS_BEDROCK_API_TEST`, and scoped GitHub/Jira credentials. Ensure secrets are available only to the backend service. Check one model request and repository access without printing secret values.
4. Deploy Vercel from `frontend/`, set the backend origin for the same-origin API proxy, and load list/detail and events in a browser. Verify all mutating endpoints return 401/403 for a direct unauthenticated Railway request.
5. Run the real Jira → investigation → four-part verification → human approval → GitHub **draft PR** → Jira comment journey. Confirm the PR is idempotent and no branch is merged automatically. Record the issue key, investigation ID, PR URL, test evidence, and deployed versions.

The source AWS RDS database was an empty staging database, so a fresh Supabase schema is sufficient unless real investigations are added before cutover. Do not copy the AWS app secret or RDS credentials into Supabase.

## Required product changes

1. **Protect the public API.** The current backend has no user authentication; its only review gate checks investigation state, not reviewer identity. Add verified user identity and authorization to all investigation routes. Keep `/health` and `/ready` safe for health checks. Keep the Jira webhook isolated and HMAC-verified before enabling it. A Vercel login wall by itself does not protect the directly reachable Railway hostname. Add an integration test proving an unauthenticated caller cannot approve or create a PR through Railway directly.
2. **Preserve investigation workspaces.** Mount one Railway volume at `/srv/tracelab/repos`. Clone the dedicated target repository at runtime into `/srv/tracelab/repos/target`, because Railway volumes are not mounted during build or pre-deploy. Keep the backend at one replica while worktrees and in-process background tasks depend on local state. On startup, mark interrupted investigations recoverable or blocked explicitly, and wait for active work before routine redeploys. Test restart during verification.
3. **Connect Supabase PostgreSQL.** Start with a new empty project because AWS staging currently contains no real investigations. Run `alembic upgrade head` once using a protected deployment job. Use a separate application database role and TLS. For a persistent Railway backend, use Supabase's direct connection if IPv6 works; otherwise use the session pooler on port 5432. Avoid transaction pooling for this app until its SQLAlchemy/asyncpg prepared-statement behavior is reviewed. Verify migration, `/ready`, and persistence after a backend restart.
4. **Connect Vercel to Railway.** Deploy the repository's `frontend/` directory as the Vercel project root. Keep browser requests on relative `/api/*` paths and proxy them to the Railway HTTPS origin. Add automated browser checks that list/detail, events, approval, and PR calls resolve through the Vercel URL. Backend authorization must hold even if someone calls Railway directly. Keep model, Jira, GitHub, and DB credentials only in Railway service variables.
5. **Prove the live workflow.** Use the dedicated Python test repository and a real Jira issue; test Bedrock tool use, all four verification proofs, human identity and approval, one GitHub draft PR, idempotency, and the Jira comment. Confirm failure states and logs contain no secrets. Do not use Loreforge or placeholder KAN-4 for this acceptance test.

## AWS cleanup and cutover record

AWS stack `tracelab-mvp-core`, ECR repositories `tracelab/api` and `tracelab/frontend`, secret `tracelab-mvp-app-env`, and budget `tracelab-mvp-account-100` were the staging resources. On 2026-09-27, CloudFormation stack deletion completed; API lookup then returned "stack does not exist." The EC2 instance is `terminated`, RDS returns `DBInstanceNotFound`, the stack IAM role returns `NoSuchEntity`, ECR lists no `tracelab/` repositories, Secrets Manager lists no app or RDS-managed administrator secret, and AWS Budgets returns `NotFoundException` for the alert. The final manual RDS snapshot `tracelab-mvp-core-snapshot-database-8tb3gdtepen6` was deleted; no manual snapshots or retained automated backups remain. No TraceLab-tagged EBS volumes remain. Resource Groups Tagging API still lists the terminated EC2 instance as a historical tag record; it is not running.

The owner chose to keep the separately created `ibm-hack-deployer` IAM user and access key. The old `localhost:13011` SSM tunnel/dashboard can no longer serve the deleted EC2 deployment.

## Platform facts checked on 2026-09-27

- Vercel assigns a `*.vercel.app` URL; Railway can generate a `*.up.railway.app` URL with managed HTTPS. No purchased DNS domain is required for this staging layout.
- Railway volumes persist files across deployments and are mounted at runtime, not during build or pre-deploy. Railway's default deployment behavior maintains one active deployment per service, so in-process jobs still need restart handling.
- Supabase recommends direct PostgreSQL connections for persistent backends, or its IPv4 session pooler where direct IPv6 is unavailable. Its transaction pooler has prepared-statement limitations.
- Vercel announced on 2026-09-09 that Vercel Authentication can protect production deployments on every plan. That protection applies to Vercel requests and does not authenticate direct requests to the Railway API.

Sources: [Vercel generated domains](https://vercel.com/docs/domains/working-with-domains), [Vercel production protection update](https://vercel.com/changelog/protect-production-deployments-for-free-on-every-plan), [Railway public networking](https://docs.railway.com/networking/public-networking), [Railway volumes](https://docs.railway.com/volumes), [Railway deployments](https://docs.railway.com/deployments/reference), [Supabase connection methods](https://supabase.com/docs/guides/database/connecting-to-postgres).
