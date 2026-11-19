# AcmeHarvest — context for agents (fictional example)

## Architecture
- `api/` — .NET backend, REST, MSSQL
- `web/` — React frontend
- `mobile/` — Flutter apps for packhouse and warehouse tablets

## Conventions
- API changes must stay backward-compatible; version new endpoints.
- Every lot carries a traceability ID from intake to invoice.

## Domain terms
- **Lot** — a batch of produce from one harvest intake.
- **Grade** — quality class assigned at grading.
