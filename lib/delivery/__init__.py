"""Anuvia delivery agents.

One module per practice. Each module registers its handlers via
`@register(...)` decorators imported from `lib.orchestrator`, so a simple
`import lib.delivery.<practice>` at app boot is enough to wire it up.

Currently shipped:
  * `finops_audit` — 4-week FinOps Audit delivery (R$ 45-60k)
"""
