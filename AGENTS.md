# WARMAGENT-Public development

- This is the owner-independent public source repository. Keep personal account bindings, private personas, memories, logs and credentials in ignored local files.
- Public defaults use an example persona and no authenticated owner. Stable account IDs, never display names, authorize tools.
- Preserve provenance, scope isolation, archive integrity, snapshot/CAS semantics, outbox receipts and unknown-effect handling.
- Reuse the existing single main model, activity scheduler, Harness and memory storage. Avoid extra background model calls.
- Keep changes focused. Validate with synthetic temporary stores and fixture providers; do not log in, send real messages, run paid models or control live devices for tests.
- Start with README.md, docs/DEPLOYMENT.md and docs/ARCHITECTURE.md. Update these maps when changing entries or contracts.
- Run Check-Integrated.cmd for the small offline suite. Run scripts/check_public.py before publishing; an external local policy may supply additional private strings to reject.
- Do not import a private repository's Git history, operational notes or personal dialogue samples into this repository.

2026-10-29 feature sync: see docs/FEATURES_2026-10-29.md and docs/MEMORY_EVALUATION.md. Recent group messages come from the current scope's frozen request window; personal pages are read-only owner views. Notes, interests and idle contact keep the original budgets, receipts and permissions. CompactMemoryIndex adds binary float64 vectors and CSR TF-IDF builds; old JSON generations remain readable. Roll back to a legacy generation with the new reader before disabling the compact reader or downgrading. Application's optional index_factory is the only core wiring change. Function/performance tests were explicitly skipped for this public transplant; retain the test fixtures for later use. Always perform file/history privacy checks before publishing.
