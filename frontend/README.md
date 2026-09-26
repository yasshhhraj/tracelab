# TraceLab dashboard

The Next.js dashboard displays TraceLab investigations, evidence, diagnosis, and human review actions. It requires the TraceLab FastAPI backend; Jira and GitHub credentials are not needed to browse existing investigations.

## Run locally

1. Start the TraceLab database and FastAPI backend. Confirm `http://127.0.0.1:8000/health` returns a healthy response.
2. From this directory, run:

   ```bash
   npm ci
   API_ORIGIN=http://127.0.0.1:8000 npm run dev
   ```

3. Open [http://localhost:3000/investigations](http://localhost:3000/investigations).

`API_ORIGIN` is read by the Next.js server and defaults to `http://127.0.0.1:8000`. Browser requests to `/api/*` are rewritten to the backend. Do not expose API credentials through `NEXT_PUBLIC_*` variables.

The list and detail views poll every three seconds while an investigation is active. Review actions are shown only for `WAITING_FOR_REVIEW` investigations. Approving a diagnosis records approval; GitHub draft PR creation is a later checkpoint.

## Checks

```bash
npm run lint
npx tsc --noEmit
npm run build
```
