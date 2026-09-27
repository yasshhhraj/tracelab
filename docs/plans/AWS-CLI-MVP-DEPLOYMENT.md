# TraceLab MVP deployment with the AWS CLI

**Status (2026-09-27):** Domain-free staging is running in `us-east-1`: EC2, private encrypted RDS, ECR images, a local-only gateway, and SSM access. The `tracelab-mvp-account-100` budget is an alert, not a spending cap. Public HTTPS, Cognito, live Bedrock/Jira/GitHub validation, and the draft-PR acceptance test remain pending.  
**Scope:** A private, single-instance MVP with a real database and live Bedrock/Jira/GitHub integration. The live draft-PR acceptance test remains the separate [CP-12 follow-up](CP-12-live-validation-follow-up.md).  
**Default region:** `us-east-1`, matching the currently configured Bedrock Mantle endpoint. Replace every example domain, account ID, repository and budget value before executing a command.

**Local preparation verified:** `make test` (280 passed, including the seeded demo and ten-case benchmark), `make lint`, frontend build/lint, production Compose interpolation, and local API/frontend Docker builds. The backend image includes pytest, Alembic, Git, and the Git askpass helper. These checks do not replace the RDS migration smoke test or live integrations.

### Domain-free staging access

The CloudFormation stack is `tracelab-mvp-core` in AWS account `958124171224`. EC2 instance `i-032eada579ffd3889` has **no inbound security-group rules**; RDS is private. The RDS migration completed and the API, frontend, and gateway containers started healthy. The gateway returned HTTP 200 for `/investigations` and the investigations API returned an empty list from RDS. The first browser visit showed “No investigations yet,” as expected for a clean database.

From a workstation with AWS CLI credentials for profile `ibm-hack-deployer` and the AWS Session Manager plugin installed, start the tunnel and leave the command running:

```bash
aws ssm start-session \
  --target i-032eada579ffd3889 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8080"],"localPortNumber":["13011"]}' \
  --profile ibm-hack-deployer --region us-east-1
```

Then open `http://localhost:13011/investigations`. Port `13011` belongs to the workstation running the tunnel. The EC2 public IP `35.175.118.170` is **not** a dashboard URL; exposing it would bypass the planned HTTPS/Cognito protection. The staging gateway binds only to EC2 loopback. The Session Manager plugin was installed for this workstation under `/tmp/tracelab-ssm-bin`, so this specific shell needs `PATH=/tmp/tracelab-ssm-bin:$PATH` until a permanent plugin installation is done.

The deployed API digest is `sha256:be3095dc9d75df84a3c95475d29cc167233991f4cf1b2f3e8606b44856558a78`; the frontend digest is `sha256:ccf7759debd897a37826048f839282117da09f234bc1e2d619b09023835b7355`. Deployment files are `deploy/aws/staging-core.yaml`, `deploy/aws/bootstrap-staging.sh`, and `deploy/aws/run-staging.sh`. The app database credential is in the tagged Secrets Manager secret `tracelab-mvp-app-env` and in a root-only env file on EC2. The RDS administrator credential is managed by RDS; neither value is in this document.

To perform a real investigation, configure the Bedrock Mantle API key server-side, a dedicated disposable target repository on EC2, and Jira/GitHub integration secrets. The current staging env only contains the database URL; it intentionally does not import the local Loreforge path or placeholder KAN-4 issue. The absence of a public HTTPS endpoint also prevents Jira webhooks and Cognito login testing. Finish those after choosing a domain.

## 1. Deployment decision and inputs

Use one EC2 instance running the frontend and API containers, with a private RDS PostgreSQL instance. The API must have a local, writable Git checkout and enough disk for three isolated worktrees. Run **one API process/replica** for this MVP: investigations currently run as in-process FastAPI background tasks and will be interrupted by a restart. An ECS/Fargate migration needs an explicit workspace and durable-job design first.

```text
Browser → HTTPS ALB + Cognito → Next.js :3000 (pages)
Browser → HTTPS ALB + Cognito → FastAPI :8000 (/api/*)
Jira webhook (later) → HTTPS ALB path rule → FastAPI :8000, HMAC checked
FastAPI → private RDS PostgreSQL
FastAPI → Bedrock Mantle, Jira Cloud, GitHub over outbound HTTPS
FastAPI → bind-mounted, dedicated Git checkout on EC2 encrypted EBS
```

Use two public subnets for the ALB and one EC2 host; use two private subnets for RDS. Give EC2 outbound internet access for GitHub, Jira, Bedrock, ECR and Secrets Manager, but allow inbound application ports **only from the ALB security group**. Do not open SSH; administer the host with [SSM Session Manager](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/connect-with-systems-manager-session-manager.html). An HTTPS listener with [ALB/Cognito authentication](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-authenticate-users.html) protects all human-facing paths. The initial release does not enable the Jira webhook.

| Input to choose | Proposed value or decision |
|---|---|
| AWS CLI profile/account | `ibm-hack-deployer` in AWS account `958124171224` was verified with STS. It is an IAM user with `AdministratorAccess`; replace it with a narrower deploy role after the required permissions are known. Never copy its keys into this repository. |
| AWS region | `us-east-1`; confirm Bedrock model availability and a suitable EC2/RDS size there. |
| DNS name | Pending: provide a hostname under a domain you control. `tracelab.example.com` is a placeholder. |
| Budget | `tracelab-mvp-account-100` is a **$100/month account-wide alert**, with notifications at 80% and 100% to the chosen recipient. It also includes costs from other projects in this AWS account and does not stop spending. Use the [AWS Budgets CLI](https://docs.aws.amazon.com/cli/latest/reference/budgets/create-budget.html) to inspect it. |
| Target repository | A dedicated Python test repository for the first real PR; do not use Loreforge or placeholder KAN-4 for the acceptance test. |
| Database migration | Start with a clean production RDS database. Do not copy local placeholder investigations into production. |
| Access | One or a few trusted users in a Cognito user pool; application-level roles/audit are follow-up work. |

The checked-in `docker-compose.yml` and `docker-compose.override.yml` are **development configuration**. Production must use an explicit `docker-compose.prod.yml` so the development override cannot silently add hot reload, SQLite or a local Postgres container.

## 2. Code changes required before provisioning

1. **Production containers.** Add a multi-stage `frontend/Dockerfile` with `npm ci`, `npm run build`, and `npm run start`. Extend `backend/Dockerfile` to include `alembic.ini`, `alembic/versions`, and the project's pytest/test runtime. The current runtime image has Git but no pytest; therefore it cannot meet TraceLab's own verification contract. Include only the test tools needed by the allowed target language. Keep build and runtime images pinned to tested versions or digests.
2. **Production Compose and configuration.** Add `docker-compose.prod.yml` with `frontend` and `api` only, immutable ECR image references, explicit health checks, resource limits, a bind mount for the `/srv/tracelab/repos` parent directory (the app creates worktree siblings next to `target`), and log configuration. The ALB routes `/api/*` directly to FastAPI, so remove or disable the production Next.js `/api` rewrite; verify no build-time `API_ORIGIN` value points at localhost. Pass `LLM_BASE_URL`, the Bedrock API key, `JIRA_USER_EMAIL`, `JIRA_WEBHOOK_SECRET` when enabled, `TARGET_REPOSITORY`, and `GITHUB_TOKEN`; the current Compose file omits several of these. Set `ENVIRONMENT=production`. Do not put tokens in image layers or Git.
3. **Access and repository boundary.** Accept traffic on EC2 ports 3000/8000 only from the ALB. Have the ALB authenticate every human path, including `/api/investigations/*/approve` and `/pull-request`. Restrict investigation repositories to an explicit configured allowlist; the current API accepts caller-provided repository paths. Retain the product rule that PR creation requires a prior human `approve` call. Do not allow auto-merge.
4. **Credential handling.** The current GitHub client constructs a token-bearing Git URL for clone/push. Replace this with a credential helper or short-lived GitHub App credentials that do not appear in process arguments, temporary Git remotes, or logs before enabling live PR creation. The EC2 instance profile reads narrowly scoped Secrets Manager secrets; a boot/deploy script writes a root-readable-only temporary environment file outside the checkout, starts Compose, and removes or rotates that material safely. Do not pass secret values in SSM command text or shell history.
5. **Operational behavior.** Add a DB-aware readiness check, log redaction, and a release procedure that waits for active investigations before restarting the single API process. Decide how to recover interrupted investigations. Prepare `alembic upgrade head` as a one-off migration command and test it against an empty PostgreSQL database.
6. **Release gate.** Run `make test`, `make demo` twice, `make benchmark`, frontend build/lint, container build, migration smoke, and Ruff. The current local suite passes 280 tests, including the seeded demo and benchmark, and repository-wide `make lint` passes. Still run the migration smoke against clean PostgreSQL and live integration checks before launch.

Deliverables from this phase: two production Docker images, a production Compose file, a safe secret-loader/deploy script, an allowed-repository configuration, and a small `scripts/aws/` CLI wrapper that records resource IDs and supports re-runs without creating duplicates. Store deployment state outside Git or in ignored files, without secret values.

## 3. AWS CLI execution order

These are **commands to implement and run after the code gate**, not commands already executed. Keep generated IDs (VPC, subnet, security group, RDS, ALB, instance, Cognito, secret ARNs) in a deployment state file. Use `describe-*` before `create-*` so a failed run can resume. Do not print secret values.

### 3.1 Preflight and cost control

```bash
export AWS_PROFILE=<deployment-profile>
export AWS_REGION=us-east-1
export STACK_NAME=tracelab-mvp
aws sts get-caller-identity --profile "$AWS_PROFILE"
aws ec2 describe-availability-zones --region "$AWS_REGION" --profile "$AWS_PROFILE"
```

The account was verified as `958124171224`. The `$100` monthly budget and 80%/100% notifications have been created; do not create a duplicate. Estimate EC2, RDS, ALB, EBS, data transfer, Bedrock inference and CloudWatch before choosing sizes. [AWS Budgets](https://docs.aws.amazon.com/cli/latest/reference/budgets/create-budget.html)

### 3.2 Network and security groups

Create a dedicated VPC (`10.42.0.0/16`) with DNS hostnames/resolution enabled, two public subnets in separate Availability Zones (`10.42.1.0/24`, `10.42.2.0/24`), two private DB subnets (`10.42.11.0/24`, `10.42.12.0/24`), an Internet Gateway and public route table. Use `aws ec2 create-vpc`, `create-subnet`, `create-internet-gateway`, `attach-internet-gateway`, `create-route-table`, `create-route`, and `associate-route-table`. Keep DB subnets without an internet route.

Create three security groups with `aws ec2 create-security-group` and `authorize-security-group-ingress`:

| Group | Inbound rule |
|---|---|
| ALB | TCP 443 from the intended users (initially a restricted CIDR if practical). |
| EC2 app | TCP 3000 and 8000 **from the ALB security group only**; no SSH rule. |
| RDS | TCP 5432 **from the EC2 app security group only**. |

The EC2 host sits in a public subnet with a public IP for outbound access, while its security group still blocks direct application traffic. RDS must be `--no-publicly-accessible`. Record and verify all four subnets' AZs and route tables before creating RDS. [RDS connection/security guide](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_GettingStarted.CreatingConnecting.PostgreSQL.html)

### 3.3 Database and secrets

Use `aws rds create-db-subnet-group`, then `aws rds create-db-instance --engine postgres --no-publicly-accessible --storage-encrypted --backup-retention-period 7 --manage-master-user-password ...`. Choose a currently supported engine version and instance/storage size after a cost check. AWS supports Secrets Manager-managed master passwords and private DB instances; verify the resulting endpoint and `PubliclyAccessible=false` with `describe-db-instances`. Create a separate least-privilege `tracelab_app` database role; never use the RDS master user in the application. Set up a tested restore/snapshot procedure. [RDS CLI reference](https://docs.aws.amazon.com/cli/latest/reference/rds/create-db-instance.html), [backup retention](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.BackupRetention.html)

Create separate Secrets Manager entries for the application `DATABASE_URL`, Bedrock key, Jira token, GitHub token and, later, Jira webhook secret. Use `aws secretsmanager create-secret` with a protected input file (`0600`, outside this repository), never `--secret-string <literal-token>` in shell history. Grant the EC2 role `secretsmanager:GetSecretValue` only for these ARNs. The deploy script keeps a root-readable-only environment file on encrypted disk outside Git and refreshes it on rotation; restart containers to pick up rotated values. [AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/retrieving-secrets.html)

### 3.4 Images, IAM and EC2 host

Create `tracelab/api` and `tracelab/frontend` ECR repositories with `aws ecr create-repository --image-tag-mutability IMMUTABLE`. Configure image scanning at the registry level. Build the tested images, authenticate with `aws ecr get-login-password`, and push both images under the Git commit SHA. Resolve and pin their image digests in the production Compose file. [ECR push procedure](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html)

Create an EC2 role/instance profile with `aws iam create-role`, `attach-role-policy`, `create-instance-profile`, and `add-role-to-instance-profile`. Scope it to SSM core access, read-only ECR pull, specific Secrets Manager reads, and CloudWatch logging. Launch an encrypted-EBS Amazon Linux instance using `aws ec2 run-instances` with `--metadata-options HttpTokens=required,HttpPutResponseHopLimit=1`, the app security group, and no SSH key. Obtain the current AMI ID from the appropriate AWS SSM public parameter rather than hard-coding an old AMI. Install Docker/Compose through reviewed bootstrap/user data. Validate `aws ssm start-session --target <instance-id>` before proceeding. [IMDS options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-IMDS-new-instances.html), [Session Manager](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/connect-with-systems-manager-session-manager.html)

Clone the approved target Git repository onto encrypted EBS at `/srv/tracelab/repos/target`, set its expected `origin` and branch, and mount it into the API container at a stable path. Set `TARGET_REPOSITORY` to that **container path**. Keep worktrees on the same host with enough free disk; do not run the user's Loreforge checkout in the first Python acceptance test.

### 3.5 HTTPS, login and DNS

Request a DNS-validated certificate with `aws acm request-certificate --domain-name <your-domain> --validation-method DNS`. Create the ACM validation record with `aws route53 change-resource-record-sets` or at your external DNS provider, and wait for `ISSUED`. Create a Cognito user pool, an app client with a client secret and authorization-code flow, and a Cognito domain. Add only the intended users. Configure the callback `https://<your-domain>/oauth2/idpresponse`. [ACM CLI](https://docs.aws.amazon.com/cli/latest/reference/acm/request-certificate.html), [ALB/Cognito requirements](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-authenticate-users.html)

Use `aws elbv2 create-load-balancer`, `create-target-group` (frontend port 3000 and API port 8000), and `create-listener` for HTTPS 443. Set the API target-group health-check path to the DB-aware `/ready` endpoint. The default listener action must authenticate via Cognito **then** forward to the frontend target group. Use `aws elbv2 create-rule` to add an authenticated `/api/*` rule that forwards to the API target group. Serve HTTPS only; leave HTTP 80 closed. Check that the API target group is reachable only through the ALB and that `http://<instance-public-ip>:8000` is blocked. Point the domain at the ALB using a Route 53 alias or an external DNS CNAME. [ALB authentication](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-authenticate-users.html)

Do **not** create the unauthenticated Jira webhook listener rule at initial launch. After the HMAC test passes, add an exact `/api/jira/webhook` HTTPS path rule with a higher priority than `/api/*`; it forwards directly to the API target group without Cognito, while all other `/api/*` paths stay behind authentication. Set `JIRA_WEBHOOK_SECRET` and test invalid/missing signatures return 403. Jira Cloud secure admin webhooks send an `X-Hub-Signature` HMAC header when configured with a secret. [Atlassian webhook guide](https://developer.atlassian.com/cloud/jira/software/webhooks/)

### 3.6 Start, migrate and observe

Use `aws ssm send-command` with a reviewed host-side deploy script, then inspect it with `aws ssm get-command-invocation`, or use Session Manager to pull the pinned ECR images and place the reviewed production Compose file on EC2. Fetch secrets via the instance role **on the host**; do not embed their values in SSM command parameters or user data. Run the one-off backend container command `alembic upgrade head` against RDS, then `docker compose -f docker-compose.prod.yml up -d`. Keep a record of the deployed image digests and migration revision. The local `.env` and the key exported in another terminal do not automatically exist on EC2.

Create CloudWatch log groups with `aws logs create-log-group`, set retention with `aws logs put-retention-policy`, and add alarms with `aws cloudwatch put-metric-alarm` for ALB target health/5xx, EC2 CPU/disk, RDS free storage/connections, and application failures. Verify RDS automated backups and a manual snapshot before schema changes. Keep the last working image digests for rollback; do not automatically downgrade the database. [CloudWatch Logs](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html)

## 4. Configuration outside AWS

| Platform | What the owner configures | TraceLab check |
|---|---|---|
| **Bedrock** | Confirm the account/region can invoke `nvidia.nemotron-nano-3-30b` on Mantle. Store a Bedrock API key as `AWS_BEDROCK_API_TEST`; set `LLM_MODEL=nvidia.nemotron-nano-3-30b` and `LLM_BASE_URL=https://bedrock-mantle.us-east-1.api.aws/v1`. Rotate/restrict the key per AWS guidance; never paste it into Jira, GitHub, or this plan. | From the deployed API, test one real Chat Completions request with a tool call; confirm no key appears in logs. The model ID and endpoint are listed in the [AWS model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-nvidia-nemotron-nano-3-30b.html). |
| **GitHub target repo** | Create a dedicated Python repository with a reproducible issue. Grant a repository-scoped fine-grained token (or, after client changes, a GitHub App) **Contents: read/write** for branch push and **Pull requests: read/write** for draft PR creation. Set its `origin` on the EC2 checkout and protect the base branch. Do not enable auto-merge. | Verify metadata access, then after the evidence and human `approve` call, confirm one real draft PR with the regression test and an idempotent second PR request. [GitHub token setup](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens), [PR permission](https://docs.github.com/en/rest/pulls/pulls). |
| **Jira Cloud** | Use a dedicated account with Browse Issues and Add Comments in the test project. Create an API token compatible with this client's `JIRA_USER_EMAIL` + `JIRA_API_TOKEN` Basic-auth flow; set `JIRA_BASE_URL`. Create a **real** issue describing the seeded bug; KAN-4 is a placeholder. | Import issue, verify investigation and a comment. Only after the signed-webhook route is exposed, configure a secure admin `issue_updated` webhook for the `Ready for AI Debugging` status with the same HMAC secret. [Atlassian Basic auth](https://developer.atlassian.com/cloud/jira/service-desk/basic-auth-for-rest-apis/), [webhook signatures](https://developer.atlassian.com/cloud/jira/software/webhooks/). |
| **DNS registrar** | Control the chosen domain. Add ACM's DNS validation record, then an ALB alias/CNAME. If DNS is outside Route 53, perform those record changes in that provider. | Certificate is `ISSUED`; HTTPS loads the site; ALB routes browser requests to `/api/*` to the API behind Cognito. |
| **GitHub Actions (later)** | For automated builds/deployments, create an AWS OIDC role scoped to this repo and protected branch/environment. Avoid long-lived AWS keys in GitHub secrets. | A workflow can build, test, push SHA-tagged ECR images and invoke the reviewed deploy script only after required checks. [GitHub OIDC on AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws). |

The current Jira client uses the `*.atlassian.net` site URL with email/API-token Basic auth. Scoped service-account tokens that require `api.atlassian.com` Bearer requests need a client change and should not be substituted without a test. The current Bedrock key alias is read by `backend/app/config.py`; set either `AWS_BEDROCK_API_TEST` or `LLM_API_KEY`, not conflicting values for both.

## 5. Deployment verification and rollback gate

1. Confirm the ALB certificate, Cognito login, API target-group `/ready` checks, dashboard list/detail, and database persistence after an API restart. A direct request to EC2 ports 3000/8000 must fail.
2. Confirm one Bedrock tool-call response, one Jira issue import/comment, and a disposable Python investigation with exactly one VERIFIED hypothesis satisfying reproduction, FAIL before, PASS after, and existing-suite PASS. The other two hypotheses must be rejected by evidence.
3. Confirm `POST /pull-request` returns 409 before approval. Review the diagnosis, call `approve`, then create a real **draft** PR in the dedicated test repository. Confirm the tested source diff and generated test are present, the URL is persisted, and a repeated request does not create a second PR. Complete the [CP-12 live validation record](CP-12-live-validation-follow-up.md). Do not auto-merge.
4. Check no secret value appears in process command lines, logs, model prompts, Jira comments, PR body, or deployment state. Verify RDS backups, log alarms, budget notification, and a documented way to revert to the prior image digest.
5. If a release fails, stop new investigations, restore the previous frontend/API image digests, and check database compatibility. Restore RDS from a snapshot only through an explicit recovery procedure; do not automatically run an Alembic downgrade.

**Completion condition:** The CLI deployment is reproducible from a clean AWS account/profile with recorded resource IDs, all checks above pass, and the exact deployed image digests, domain, region and test evidence are recorded without credentials. The active budget alone does not mean the application has been deployed.
