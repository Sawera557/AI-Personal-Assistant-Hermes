# MVP Phase — Gandi AI Personal Assistant

Source: Notion "🚀 MVP phase" (imported 2026-06-22)

## Goal
Build the first usable version of Gandi's AI Personal Assistant: a simple owner
frontend, Hermes Jarvis (owner-facing AI), Hermes Agent (backend runtime), a
FastAPI/Lambda backend, Pinecone knowledge retrieval, PostgreSQL source of
truth, S3 document storage, and Maton.ai-assisted connected-app workflows.

## Core principle
The AI model is not the security layer — the backend is. No AI agent, Maton.ai
workflow, app connector, or Lambda accesses data unless the backend has verified
user_id, role, active_entity_id, permission_scope, sensitivity_level,
approval_requirement, and audit_requirement.

## MVP must allow the Owner to
1. Log in securely (MFA).
2. Select All Businesses or one business via the workspace switcher.
3. See a simple owner command center.
4. Ask Hermes Jarvis questions.
5. Search documents through Pinecone with safe entity filters.
6. View connected app health.
7. View QuickBooks read-only financial summaries.
8. View Gmail/Calendar/Drive/Sheets/Forms sync status.
9. View activity analytics.
10. View data pipeline health.
11. Approve sensitive actions before they happen.
12. Keep all businesses separate by entity_id.

## In scope
Owner-only frontend, business workspace switcher (All Businesses + single),
Hermes Jarvis chat, Hermes Agent runtime, FastAPI APIs, AWS Lambda, PostgreSQL
schema, S3 storage, Pinecone retrieval, Secrets Manager, basic audit logs,
connected-apps/app-health/data-pipeline/activity-analytics dashboards,
QuickBooks read-only sync, Google Drive/Gmail/Calendar/Docs/Sheets/Forms sync,
Notion documentation, basic approval queue.

## Out of scope
Multi-role staff/tenant/CPA/field-worker frontends, automated payment movement,
QuickBooks writes, direct bank actions, automatic email/DocuSign sending without
approval, full mobile release, full production hardening, connecting all 152
Maton.ai apps at once.

## Build order (drives the task list)
1. Auth + users + roles + entities
2. FastAPI backend + PostgreSQL
3. S3 + Secrets Manager
4. Business switcher backend
5. Simple Owner Command Center frontend
6. Hermes Jarvis chat API
7. Hermes Agent runtime
8. Document upload + Pinecone
9. Basic AI document Q&A
10. Google Drive sync
11. Gmail sync
12. Calendar sync
13. Google Docs/Sheets/Forms sync
14. QuickBooks read-only sync
15. Connected Apps dashboard
16. App Health dashboard
17. Activity Analytics
18. Data Pipeline dashboard
19. Approval queue
20. Owner morning briefing

## Acceptance criteria
Owner can log in securely; select All Businesses or one business; dashboard
reloads by selected business; Hermes Jarvis answers and uses active_entity_id in
every request; documents upload/sync from Drive, store in S3, index in Pinecone
with entity metadata; Pinecone search uses entity/role/sensitivity filters;
Gmail sync creates classified email records; Calendar sync creates
meeting/deadline records; QuickBooks read-only summaries work; Sheets/Forms sync
supports Afro Farm basics; Connected Apps and Data Pipeline dashboards show
health; Activity Analytics shows AI/app/document/task activity; approval queue
blocks sensitive actions; audit logs capture AI/document/sync/approval events;
no secrets in frontend, logs, prompts, or Notion; no write action without owner
approval.
