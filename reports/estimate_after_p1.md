# Estimate after P1 (T03 deliverable)

Task: T03 — upstream compatibility probe (sample plan P0–P1)
Baseline commit of this estimate: the R2 repair commit of `docuagents-v1`
Probe evidence: `reports/upstream_compatibility.json` (17 facts, 5 differences)
Scope: work that starts after P1. The sample-plan tasks P2–P4 are **T07**,
**T14** and **T17** in the Engineering Tasks list.

All figures are person-days (PD) for one engineer working with the locked
environment in `environment.lock.json`. They include implementation, offline
tests and review fixes, and exclude review waiting time.

## Summary

| Task | Scope | Estimate | Confidence |
|---|---|---:|---|
| T07 | P2: matrix, gold and generation core | 6.0 PD | medium |
| T14 | P3: sample-set expansion and coverage | 4.0 PD | medium |
| T17 | P4: freeze, manifest and release checks | 3.0 PD | medium-low |
| — | Cross-task: system-level offline re-verification and firewall rule | 0.5 PD | high |
| | **Total** | **13.5 PD** | |

Risk buffer: +20% for T14 and T17, because the P1 probe cannot see how the
unwritten generators behave. With the buffer, the planning figure is
**about 16 PD**.

## T07 — P2: matrix, gold and generation core (6.0 PD)

| Work item | PD | Basis |
|---|---:|---|
| Spec model and matrix rows (A–D constructions) | 1.5 | Eight probe inputs already show the shape; the matrix multiplies them |
| Gold builder with full fields and missing states | 1.5 | F8: `expected()` has no qty/unit_price and reports an unprinted tax as 0, so gold is built independently |
| Strict validator for C-class conflicts | 1.0 | F6: `validate()` is an extra check only, not the gate |
| Generation driver and per-document manifest | 1.0 | Existing `render_document` contract is stable (F12) |
| Offline tests and fixes | 1.0 | 65 offline tests already run in about 10 s; new tests stay in that range |

Adaptations already paid for by the probe, so they are **not** in this
estimate: explicit zero tax (F5), printed overrides (F7), single-page images
(F9), explicit `USLONG` dates (F1), `Currency: USD` headers (F13).

## T14 — P3: sample-set expansion and coverage (4.0 PD)

| Work item | PD | Basis |
|---|---:|---|
| Add sample families not covered by the probe inputs | 1.5 | Photo/scan/handwritten families exist upstream (F11) but the new matrix rows do not |
| Coverage report per field and per construction | 1.0 | |
| Regenerate and compare against the frozen hashes | 0.5 | F15 byte reproduction holds on this host |
| Offline tests and fixes | 1.0 | |

## T17 — P4: freeze, manifest and release checks (3.0 PD)

| Work item | PD | Basis |
|---|---:|---|
| Freeze manifest, full 181-document style check | 1.0 | `--check-all` already exists in `docuagents_gen/probe.py` |
| Release checks (licence, NOTICE, no local paths, no credentials) | 1.0 | Most checks already exist as tests |
| Documentation and sign-off package | 1.0 | |

## What could move the estimate

| Risk | Effect |
|---|---|
| Byte reproduction fails for a new construction | +1 PD per affected family (re-freeze and re-verify) |
| A new sample needs a layout the upstream engine cannot draw | +2 PD (new layout work, outside upstream) |
| Full 181-document check turns out slow at T17 | +0.5 PD (parallelise or cache) |
| System-level offline verification needs a Windows firewall rule | +0.5 PD and an administrator session |

## Not included

- Any work on the DocuAgentS main repository (this task keeps it read-only).
- Review and approval time.
- Handwritten samples: they need the Ink Free font, which is not
  redistributable (F16). Any task that requires them needs a font decision
  first.
