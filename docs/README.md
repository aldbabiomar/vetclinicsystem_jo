# Documents this code refers to

Comments throughout VetClinicSystem JO cite design and audit documents by filename —
"see ORPHANED_RECORDS_AUDIT.md F-12", "per COMPARISON.md §1.1". Those
documents are **deliberately not vendored into this repository**. They describe
*both* apps at once (this one and `vetclinicsystem_iq`) and are kept in the shared
workspace that holds the two clones side by side:

```
VetClinicSystem/                  <- the shared workspace
├── CLAUDE.md                     the ground rules for working on both apps
├── COMPARISON.md                 the dated, append-only diff between the two
├── RELEASE_WORKFLOW.md           the tag/release checklist both apps follow
├── `SIMULATION_AUDIT_2026-09-11.md`  both apps driven as real users: a full
│                                 clinic day plus rare/edge cases, six findings
├── `SEAM_RULES.md`               rules that must hold on every sibling surface,
│                                 and the register of every time one did not
├── scripts/simulation/           the harness behind it, one repro per finding
├── audits/
│   ├── ERROR_500_AUDIT.md        every action that could raise an unhandled exception
│   └── ORPHANED_RECORDS_AUDIT.md every way a row could be left unreachable
├── features/
│   ├── CLEANUP_FEATURE_PLAN.md   the "Clean Up" write-off design (built)
│   └── MONITORING_FEATURE_PLAN.md the four monitoring layers (built)
└── webapps/
    ├── vetclinicsystem_iq-main/  <- one of these is this repo
    └── vetclinicsystem_jo-main/
```

They are ~3,700 lines between them and duplicating that into two repositories
would guarantee the copies drift, which is the specific failure the workspace
exists to prevent.

**If you have only this repository**, those citations will not resolve for you.
The code they annotate is written to stand on its own: every guard that exists
because of an audit finding also carries the reasoning inline. The citation is
a pointer to the fuller history, not a prerequisite for reading the code.

A test (`tests/test_frontend.py`) fails if a comment cites a `.md` file that
is neither in this repository nor listed above — so this list cannot silently
fall behind the citations again.

## Cited from this repository

- `CLAUDE.md`
- `CLEANUP_FEATURE_PLAN.md`
- `COMPARISON.md`
- `ERROR_500_AUDIT.md`
- `MONITORING_FEATURE_PLAN.md`
- `ORPHANED_RECORDS_AUDIT.md`
- `RELEASE_WORKFLOW.md`

Documents that used to be cited and no longer exist anywhere —
`BUGFIXES.md`, `CLAUDE_CODE_RELEASE_WORKFLOW.md`, `UPDATE_MECHANISM_PLAN.md`,
`Consignment_Feature_Framework.md`, `data_integrity_framework.md`,
`QA_RESULTS.md`, `IQD CURRENCY ROUNDING PLAN.md` — have had their citations
replaced with the reasoning they were carrying, or repointed at the document
that superseded them.
