# Domain-free hosted TraceLab migration

**Status (2026-09-27):** Proposed migration; no Vercel, Railway, or Supabase resources have been created. The working AWS EC2/RDS staging stack remains the fallback and continues to incur charges until deliberately stopped or removed.

## Target layout

```text
Browser → Vercel Next.js (*.vercel.app) → authenticated /api proxy
       → Railway FastAPI (*.up.railway.app) → Supabase PostgreSQL
                                          → Railway persistent volume (Git checkout/worktrees)
                                          → Bedrock Mantle, Jira, GitHub
```

Bedrock's API key authenticates model calls only. Supabase requires its own PostgreSQL connection credential; neither credential belongs in the browser or Git. Use the existing FastAPI/SQLAlchemy models and Alembic migrations with Supabase PostgreSQL. Supabase Auth is optional as a product, but the public API needs a real authentication and authorization scheme before exposing approval, rejection, or PR creation.

## Required product changes

1. **Protect the public API.** The current backend has no user authentication; its only review gate checks investigation state, not reviewer identity. Add verified user identity and authorization to all investigation routes. Keep `/health` and `/ready` safe for health checks. Keep the Jira webhook isolated and HMAC-verified before enabling it. A Vercel login wall by itself does not protect the directly reachable Railway hostname. Add an integration test proving an unauthenticated caller cannot approve or create a PR through Railway directly.
2. **Preserve investigation workspaces.** Mount one Railway volume at `/srv/tracelab/repos`. Clone the dedicated target repository at runtime into `/srv/tracelab/repos/target`, because Railway volumes are not mounted during build or pre-deploy. Keep the backend at one replica while worktrees and in-process background tasks depend on local state. On startup, mark interrupted investigations recoverable or blocked explicitly, and wait for active work before routine redeploys. Test restart during verification.
3. **Connect Supabase PostgreSQL.** Start with a new empty project because AWS staging currently contains no real investigations. Run `alembic upgrade head` once using a protected deployment job. Use a separate application database role and TLS. For a persistent Railway backend, use Supabase's direct connection if IPv6 works; otherwise use the session pooler on port 5432. Avoid transaction pooling for this app until its SQLAlchemy/asyncpg prepared-statement behavior is reviewed. Verify migration, `/ready`, and persistence after a backend restart.
4. **Connect Vercel to Railway.** Deploy the repository's `frontend/` directory as the Vercel project root. Keep browser requests on relative `/api/*` paths and proxy them to the Railway HTTPS origin. Add automated browser checks that list/detail, events, approval, and PR calls resolve through the Vercel URL. Backend authorization must hold even if someone calls Railway directly. Keep model, Jira, GitHub, and DB credentials only in Railway service variables.
5. **Prove the live workflow.** Use the dedicated Python test repository and a real Jira issue; test Bedrock tool use, all four verification proofs, human identity and approval, one GitHub draft PR, idempotency, and the Jira comment. Confirm failure states and logs contain no secrets. Do not use Loreforge or placeholder KAN-4 for this acceptance test.

## Cutover order

1. Push the two local commits currently ahead of `origin/master` only when the repository changes have been reviewed; Vercel and Railway Git deployments cannot see local-only commits.
2. Create a Supabase project, Railway project/service and persistent volume, and Vercel project. Record their generated hostnames and region. Configure spending alerts/limits for each provider before the first deploy.
3. Implement and test the authentication and restart/workspace changes above. Deploy Railway backend and verify it with Supabase before connecting Vercel.
4. Deploy Vercel frontend, connect its `/api/*` proxy, and run the browser and live integration checks. Keep the AWS staging stack available until this path passes.
5. Export any AWS investigation data if it becomes valuable. Take an RDS snapshot, then explicitly retire EC2/RDS/ECR and related AWS resources after the replacement is verified. Deleting the stack is a separate reviewed action; the $100 AWS budget alert does not stop charges.

## Platform facts checked on 2026-09-27

- Vercel assigns a `*.vercel.app` URL; Railway can generate a `*.up.railway.app` URL with managed HTTPS. No purchased DNS domain is required for this staging layout.
- Railway volumes persist files across deployments and are mounted at runtime, not during build or pre-deploy. Railway's default deployment behavior maintains one active deployment per service, so in-process jobs still need restart handling.
- Supabase recommends direct PostgreSQL connections for persistent backends, or its IPv4 session pooler where direct IPv6 is unavailable. Its transaction pooler has prepared-statement limitations.
- Vercel announced on 2026-09-09 that Vercel Authentication can protect production deployments on every plan. That protection applies to Vercel requests and does not authenticate direct requests to the Railway API.

Sources: [Vercel generated domains](https://vercel.com/docs/domains/working-with-domains), [Vercel production protection update](https://vercel.com/changelog/protect-production-deployments-for-free-on-every-plan), [Railway public networking](https://docs.railway.com/networking/public-networking), [Railway volumes](https://docs.railway.com/volumes), [Railway deployments](https://docs.railway.com/deployments/reference), [Supabase connection methods](https://supabase.com/docs/guides/database/connecting-to-postgres).
