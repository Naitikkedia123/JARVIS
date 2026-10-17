# JARVIS Master Architecture — Baseline

## Principle
JARVIS owns its control-plane semantics: goals, durable tasks, permissions, memory contracts, model-routing policy and completion verification. Models propose; policy code authorizes; tools execute; observations establish actual outcomes.

## Logical flow
```text
Windows UI / Android companion
             |
      Authenticated API
             |
  JARVIS control plane
  (goals, context, devices, policy, audit)
             |
      Intelligence gateway
  (adapters, routing, structured output, evaluation)
             |
      Durable agent runtime
  (planning, task graph, checkpoints, bounded recovery)
             |
      Typed tool registry
  (browser, files, commands, Windows UI, integrations)
             |
   Observations -> Verifier -> Task result
             |
     Memory / project knowledge
```

## Starting deployment
A Python modular monolith on Windows, SQLite when persistence is added, FastAPI and a responsive local interface. Do not start with microservices, a large vector database, Docker agents, large local models or a phone emulator.

## Boundaries
- Provider adapters do not own task state or permissions.
- Model output does not grant tool permissions.
- Actions have typed inputs, explicit capabilities, limits and observable results.
- Local-first data; no remote model receives sensitive context unless explicitly authorized.
- No paid fallback; quotas must fail closed.
- Untrusted web pages/files/messages cannot override system policy.
- Cellular call control/audio is a tested feasibility question, not an assumed Android capability.

## Accepted architecture direction
Phase 00 is accepted. Option B is the approved direction: JARVIS owns its control plane, including task state, permissions, model-routing policy, memory contracts and verification. OpenClaw remains a potential external integration behind a selective adapter; it is not the authoritative JARVIS control plane. Recheck fit, license, security and maintenance before reusing or embedding any external component. See [ADR 0001](../decisions/0001-architecture-choice.md) for the decision and trade-offs.
