# Model routing

Roles are `architect`, `planner`, `builder`, `reviewer`, `debugger`, `tester`, `summarizer`, and
`deployer`. Each route names a model profile, while fallbacks are ordered profile names.

Profiles record local/remote placement, context and output limits, planning/coding/debugging/review
scores, tool and structured-output reliability, domain capability, cost, latency, and allowed data
sensitivity.

The router escalates after two task failures, architecture changes, a large affected file set,
security-critical scope, repeated structured-output failures, or persistent test failure. Data
sensitivity can force a suitable local profile. Every selection and reason is persisted.

```bash
vasuki models route builder local-coder --fallback strong-cloud
vasuki models route debugger strong-cloud
```
