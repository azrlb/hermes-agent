# Kanban stop cancellation review — 2026-09-02

## Incident reproduced from durable board evidence

The Crispi controller cancelled Hermes task `t_7f1cfa58`, but the next
dispatcher tick promoted the task from `blocked` to `ready` and spawned a new
worker. This happened four times. The task event history showed the same
sequence each time: `cancelled`, then `promoted`, then `claimed`, then
`spawned`.

The cause was in `_has_sticky_block()`: it recognized explicit `blocked`
events as terminally parked, but did not recognize the `cancelled` event
written by `stop_task()`. As a result, `recompute_ready()` treated a cancelled,
dependency-free task as eligible for promotion.

## Bounded correction

The sticky-block check now considers the latest event among `blocked`,
`cancelled`, and `unblocked`. A latest `blocked` or `cancelled` event keeps the
task parked. A later explicit `unblocked` event still releases it. Circuit
breaker events remain unchanged and are not made sticky by this correction.

The existing stop behavior test now verifies that a successful controller
stop remains blocked after a dispatcher recompute tick.

## Verification

The required repository test wrapper was run through the bundled Git Bash,
with the main checkout's isolated development Python supplied through
`HERMES_PYTHON`:

```text
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_blocked_sticky.py -q

27 passed, 0 failed
```

`git diff --check` passed.

## Independent adversarial review

Independent review result: **ACCEPTED**.

The reviewer verified that:

- `stop_task()` is the only scoped producer of the `cancelled` task event.
- `recompute_ready()` now keeps controller-cancelled tasks parked.
- a later explicit `unblocked` event still releases the sticky state.
- `gave_up` circuit-breaker handling remains separate and unchanged.
- the added regression covers the observed cancellation/requeue failure.

The reviewer noted two non-blocking opportunities for additional direct tests:
the full `cancelled -> unblocked` event sequence and a focused assertion that
`gave_up` remains non-sticky. Existing code paths and direct behavioral checks
confirmed both behaviors; neither was a correctness blocker for this bounded
fix.

No live gateway, worker limit, product code, merge, deployment, production
configuration, secret, customer data, or billing state was changed while this
fix was developed and reviewed.
