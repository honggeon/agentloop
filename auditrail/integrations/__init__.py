"""Phase 3 external evaluation/observability integrations.

This package holds the *adapters* for external systems (Langfuse-style
trace/score exports, promptfoo static assets) and the unified external
evidence model. Everything here is optional evidence:

- core deterministic paths never import this package;
- no external SDK (``langfuse``, ``promptfoo``) is required at runtime;
- external results never overwrite ``audit.json``, deterministic checks,
  ``agent_judge.json`` overall status or score caps.
"""
