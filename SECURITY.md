# Security model

## Boundaries

- User goals, model output, fetched web content, and generated code are untrusted.
- The deterministic risk engine cannot be overridden by model output.
- Consequential plans require a durable human approval decision.
- Tenant identity is derived from a configured API key or signed session, never request payloads.
- Generated code is filtered, then executed in a short-lived container with no network, read-only root, dropped capabilities, process/memory/CPU limits, and output/time limits.
- Web retrieval rejects credentials in URLs, non-HTTP schemes, unusual ports, private/non-global DNS results, unsafe redirects, binary content, and oversized responses.

## Deployment requirements

- Terminate TLS at a trusted reverse proxy and restrict trusted hosts/origins.
- Store API keys, database credentials, provider keys, and session secrets in a secret manager.
- Restrict `/metrics` at the network layer.
- Do not expose PostgreSQL, Redis, the Docker socket, or workers publicly.
- Prefer a dedicated gVisor, Firecracker, or Kubernetes sandbox service over a Docker socket for high-assurance workloads.
- Run image and dependency scanning and keep major-version ranges updated.

## Reporting

Do not open public issues containing credentials or vulnerability details. Report privately to the repository owner with affected version, reproduction, and impact.
