# SlideAI Change Record (Engineering)

> Purpose: Keep module/system changes traceable, reviewable, and easy to merge.

## Mandatory Rule (Must Follow)
1. Any future module-level or system-level change **must be recorded in this file** before merge.
2. Each PR/merge should include at least one new entry in the `Change Entries` section.
3. Do not mix unrelated changes in one entry.
4. If a change is reverted, add a new `REVERT` entry (do not silently delete history).

## Scope
- Record functional changes, API behavior changes, config changes, infra/runtime changes, schema changes, and risk fixes.
- Skip trivial formatting-only changes unless they affect behavior.

## Entry Format (Required)
Copy this template for every change:

```md
### [CR-YYYYMMDD-XXX] <Title>
- Date: YYYY-MM-DD
- Author: <name>
- Type: feature | fix | refactor | config | infra | docs | revert
- Scope: frontend | backend | api | db | deploy | cross-module
- Related Issue/PR: <id or N/A>

#### Why
- <problem/background>

#### What Changed
- Files:
  - <path>
  - <path>
- Behavior:
  - <before>
  - <after>

#### API/Contract Impact
- Endpoint/Interface: <none or details>
- Backward Compatibility: compatible | breaking

#### Risks
- <known risks>

#### Verification
- <how verified>

#### Rollback
- <how to rollback>
```

## Merge Checklist (Quick)
- [ ] Change is isolated to intended module scope.
- [ ] Entry added to `docs/CHANGE_RECORD.md`.
- [ ] API/contract impact documented.
- [ ] Risk + rollback documented.
- [ ] Local verification result documented.

## Agent Handoff Note
When asking an agent to implement or merge changes, include:
1. Target scope (which modules can be modified)
2. Non-target scope (what must not be touched)
3. Required output: update `docs/CHANGE_RECORD.md` with a new entry
4. Acceptance criteria + test evidence

---

## Change Entries

### [CR-20260417-001] Frontend Dev API Default Port Alignment (8000 -> 8001)
- Date: 2026-04-17
- Author: Codex Agent
- Type: fix
- Scope: frontend, api
- Related Issue/PR: N/A

#### Why
- In local dev, backend service runs on port `8001`, but frontend debug/dev path was still defaulting to `8000`, causing API 404 in register/login/debug checks.

#### What Changed
- Files:
  - `frontend/src/config/api.js`
  - `frontend/vite.config.js`
- Behavior:
  - Before: frontend dev default API URL/proxy target used `http://localhost:8000`
  - After: frontend dev default API URL/proxy target uses `http://localhost:8001`

#### API/Contract Impact
- Endpoint/Interface: none (same endpoints)
- Backward Compatibility: compatible

#### Risks
- Teammates who still run backend on `8000` need to set `VITE_API_URL` explicitly.

#### Verification
- Checked frontend `/api/me` through WebUI dev server returns `401 Not authenticated` (expected when no token), confirming API is routed to live backend rather than wrong port 404.

#### Rollback
- Revert the two files above to `8000` defaults, or set `VITE_API_URL` per developer environment.
s834
