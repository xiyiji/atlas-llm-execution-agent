# Atlas LLM Execution Agent MVP

Atlas is a five-agent committee coordinated by a control-only orchestrator. It supports planning, deterministic + model-assisted risk assessment, human approval, sandboxed code execution, web retrieval, retries, one bounded verifier rework round, working/episodic memory, append-only auditing, and live SSE updates.

## Run locally

Requires Python 3.10+.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
./run.sh
```

Open <http://127.0.0.1:8000>. The default `FORCE_DEMO=1` needs no credentials and makes no model or web requests.

## API

- `GET /api/health`
- `POST /api/tasks`
- `GET /api/tasks/{task_id}`
- `POST /api/tasks/{task_id}/approval`
- `GET /api/tasks/{task_id}/events` (SSE)
- `GET /api/audit`
- `GET /api/memory`

To use a live provider, set `FORCE_DEMO=0` and one supported provider key in `.env`. Provider errors fall back to demo mode and are disclosed in the final report.
