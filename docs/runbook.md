# Operations runbook

## Deploy

1. Inject PostgreSQL, Redis, session, API-key, and model secrets through the platform secret manager.
2. Run `alembic upgrade head` as a one-off release job.
3. Deploy API replicas, then Celery workers.
4. Confirm `/api/health` and `/metrics` before routing traffic.

## Back up and restore

- Use encrypted PostgreSQL snapshots plus point-in-time recovery/WAL archiving.
- Test restoration into a separate environment on a schedule.
- Redis contains transient broker and Pub/Sub data; PostgreSQL remains the durable task/event source.

## Rotate credentials

1. Add a new `tenant:key` entry while retaining the old key.
2. Redeploy, update clients, and confirm successful authentication.
3. Remove the old key and redeploy.
4. Rotate `SESSION_SECRET` with awareness that all browser sessions will be invalidated.

## Incident triage

- Correlate client reports using `X-Request-ID` and task IDs.
- Inspect task status and the durable event sequence before retrying work.
- Pause workers before investigating duplicate or unsafe execution.
- Revoke affected API keys and model-provider credentials if exposure is suspected.
- Never edit historical audit events; append a corrective operational event instead.

## Scaling

- Scale API replicas for connections and workers for execution throughput.
- Keep Celery prefetch at one for long-running heterogeneous tasks.
- Alert on queue depth, task failure rate, approval age, HTTP latency, Redis/PostgreSQL health, and sandbox rejection/timeout rates.
