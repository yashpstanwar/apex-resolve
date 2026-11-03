# ApexResolve Benchmark Evaluation Report

**Run Date:** 2026-11-02 18:21:03 UTC  
**Split Evaluated:** `all`  
**Total Evaluated Cases:** 64  
**Pass Rate:** **64/64 (100.0%)**  
**Latency:** p50 = 135.1ms, p95 = 290.0ms  
**Financial Invariant Violations (Duplicate Refunds):** **0**  
**Security / Tenant Isolation Violations:** **0**  

## Category Breakdown

| Category | Passed | Total | Accuracy |
|:---|:---:|:---:|:---:|
| `api_failures` | 10 | 10 | 100.0% |
| `authorization_adversarial` | 10 | 10 | 100.0% |
| `eligible_refund` | 10 | 10 | 100.0% |
| `ineligible_refund` | 10 | 10 | 100.0% |
| `missing_or_ambiguous` | 10 | 10 | 100.0% |
| `order_status` | 10 | 10 | 100.0% |
| `revision_loop` | 4 | 4 | 100.0% |

## Architectural Guarantees Verified
1. **Deterministic Financial Safety**: Proposing an action is isolated from executing money movement.
2. **Two-Phase Commit Settlement**: Idempotent database reservation committed prior to external gateway HTTP dispatch.
3. **Zero Financial Hallucination**: Every monetary figure is audited against validated state invariants.
4. **Tenant Isolation**: Foreign order numbers identically return not-found, preventing cross-tenant leakage.
