# Oracle → Databricks AI Migration Factory

**Build marker:** the UI displays `Build 2.3.0 SEMANTIC_MEDALLION_FACTORY`.

## Quick start
1. Copy `.env.example` to `.env` and configure Oracle (`ORACLE_HOST`, `ORACLE_PORT`, `ORACLE_SERVICE_NAME`, `ORACLE_USERNAME`, `ORACLE_PASSWORD`, `ORACLE_SCHEMA`) and Databricks values.
2. Backend: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8010` (or `scripts\run_backend.bat`). API: `http://127.0.0.1:8010`; Swagger: `http://127.0.0.1:8010/docs`.
3. Frontend: `npm run dev` in `frontend/`. Web UI: `http://localhost:5173`.
4. Create the first administrator with `python scripts\bootstrap_admin.py` or through the login UI.

# Oracle → Databricks AI Migration Factory — Enterprise Edition

A metadata-first, project-scoped migration control plane for Oracle → Databricks. This package is intentionally configuration driven: no sample database, source schema, table name, or target catalog is required by the engine.

## Included
- FastAPI backend with canonical metadata repository and project-scoped evidence
- React + TypeScript frontend shell with Dashboard, Medallion Design, Lifecycle and full enterprise navigation
- Deterministic Oracle datatype conversion (`NUMBER(p, s) → BIGINT/INT/DECIMAL`, `VARCHAR2/NVARCHAR2/CLOB → STRING`, `DATE/TIMESTAMP → TIMESTAMP`, `RAW/BLOB → BINARY`, `BINARY_FLOAT → FLOAT`, `BINARY_DOUBLE → DOUBLE`)
- Outbound local connector agent (`scripts/local_connector.py`) for secure, local-only credential handling
- Metadata-driven Bronze/Silver/Gold recommendation engine with automatic semantic inference
- Source snapshot hashing and drift evidence
- Project/environment target mapping and mapped SQL reference conversion
- Static alias/column validation with blocker issues
- Artifact versioning and version-specific review records
- Project-isolated lifecycle/quality-gate evidence
- Dependency ordering/cycle detection utilities
- Governed Databricks retry client for safe transient operations
- Security: PBKDF2 password hashing, JWT, account lockout, CORS, headers, secret masking helpers
- Pytest unit/API tests, Windows launchers, Docker files and documentation
- Governed project-wide AI repair queue with deterministic-first conversion, bounded correction attempts, local Ollama support, automatic artifact versioning and static revalidation

## Live migration connectivity
The package includes a production-oriented Oracle discovery adapter (`python-oracledb` in Thin mode) and a Databricks SQL connector adapter. Live E2E execution requires reachable Oracle and Databricks credentials in your local `.env`. No credentials are committed to Git.

## Important governance behavior
- Explicit `project_id` is required for project evidence.
- AI is optional and disabled by default.
- Generated artifacts are versioned; approvals apply to specific versions.
- Source objects are never automatically dropped.
- PROD destructive changes are not automatically retried or auto-approved.
- Missing mappings/columns must block dependent deployment rather than silently producing invalid SQL.

## Running live Oracle discovery
1. Create/select a project in the UI.
2. In **Sources**, add your Oracle host (e.g. `localhost`), service name / schema, and port (`1521`).
3. Start the local connector in PowerShell: `python scripts/local_connector.py --url 'http://127.0.0.1:8010' --source '<SOURCE_ID>' --server 'localhost' --database '<SERVICE_OR_SCHEMA>' --port '1521' --source-type 'oracle'`.
4. In **Sources** or **Discovery**, click **Test connection** and **Run discovery**.
5. Only after the connection test passes, click **Run discovery**.
6. If it fails, the UI displays the backend's recommended action.

For a named local SQL Server instance, use e.g. `localhost\\SQLEXPRESS` or the actual server/instance name rather than plain `localhost` if your SQL Server is not the default instance.

## DEV execution (Build 1.5)

After Discovery → Assessment → Classification → Mappings → Artifacts → Approval, open **Deployments** and use the governed sequence:

1. Test Databricks
2. DEV Precheck
3. Deploy Approved to DEV
4. Resume Failed Run if required
5. Run Reconciliation
6. Evaluate DEV Gate
7. Confirm Lifecycle shows DEV = PASSED

The Windows launchers use backend port `8010` by default in this build.

## Build 1.6.0 highlights

This build adds deterministic executable conversion for supported SQL Server scalar functions and stored procedures, unique DEV precheck blocker reporting, a role-based Runbook page, and project-scoped DEV log viewing/download from the Deployments page. See `docs/USER_RUNBOOK_1_6.md` and `docs/RELEASE_NOTES_1_6.md`.


## 1.7 AI remediation
See `docs/RELEASE_NOTES_1_7.md` and `docs/AI_REMEDIATION_RUNBOOK_1_7.md`. AI is optional; deterministic rules run first, and accepted AI candidates always require a new human approval before deployment.

## 1.8 Review Governance
Build 1.8 adds governed review decisions: Approve, Reject, Request Changes and Revoke Approval. Approval is allowed only for the current executable artifact version after static validation has PASSED. Review decisions remain immutable audit events and the latest decision is the effective deployment state. See `docs/RELEASE_NOTES_1_8.md`.

## 2.0 Governed AI repair loop

Open **AI Remediation** after artifact generation and static validation. The project-scoped scan identifies non-executable artifacts, validation failures, remediable issues, rejected reviews, and requested changes. **Run safe repair loop** applies deterministic remediation first and uses the configured model only for unresolved semantic cases. Safe candidates become new statically validated versions in **Reviews**; they are never auto-approved or auto-deployed.

For a free local model, configure Ollama in `.env`:

```text
LLM_ENABLED=true
LLM_PROVIDER=OLLAMA
LLM_BASE_URL=http://localhost:11434
LLM_API_KEY=
LLM_MODEL=qwen2.5-coder:3b
LLM_MAX_ATTEMPTS=3
```

See `docs/RELEASE_NOTES_2_0.md` and `docs/AI_REMEDIATION_RUNBOOK_1_7.md`.


## 2.0.1 Deterministic compatibility engine

Build 2.0.1 separates target datatype mapping from connector transport. SQL Server binary families (`binary`, `varbinary`, `image`, `timestamp`, `rowversion`) are extracted as hexadecimal text and bound to Databricks with `unhex(?)`, preventing Python/Arrow/connector inference from turning byte arrays into `ARRAY<VOID>`. The same metadata-driven transport registry normalizes common scalar types and records governed fallbacks for complex or unknown types.

Deployment failures now record stable `error_category`, `error_code`, `failure_stage`, retryability and an evidence-based remediation message. No column name (including `RowVersion`), source schema, sample database or target catalog is hard-coded.

### Upgrade from 2.0.0 without losing your current project

The clean ZIP intentionally does not contain `.env`, secrets or `migration_factory.db`. To continue an existing failed DEV run, stop the old backend/frontend, extract 2.0.1 to a new folder, then copy your existing root `.env` and root `migration_factory.db` into the new extracted root. Start the backend and frontend and use **Deployments → Resume Failed Run**. Do not close the existing issue manually; successful deployment evidence will resolve it.

See `docs/RELEASE_NOTES_2_0_1.md` and `docs/VALIDATION_2_0_1.md`.


## 2.1.0 Local Ollama AI

Build 2.1.0 makes local Ollama a first-class governed AI provider while preserving deterministic-first migration behavior. The application can test the local Ollama daemon, discover installed models, verify that the configured model exists, and display provider readiness directly on the **AI Remediation** page. No Ollama API key is required.

### Fastest Windows setup

After installing Ollama for Windows, run from the extracted project root:

```bat
scripts\setup_ollama_windows.bat qwen2.5-coder:3b
```

The script checks the Ollama executable/service, pulls the selected model, safely updates the root `.env` with a timestamped backup, and verifies `/api/tags`. Restart the Migration Factory backend after configuration.

If the model is already installed, configure only the application:

```bat
backend\.venv\Scripts\python.exe scripts\configure_ollama.py --model qwen2.5-coder:3b
```

Then open **AI Remediation → Test Ollama**. `Reachable = READY` and `Model installed = YES` confirm that AI fallback is ready. The repair loop still uses deterministic rules first and sends a request to Ollama only when deterministic remediation cannot safely preserve the source semantics.

See `docs/OLLAMA_LOCAL_AI.md`, `docs/RELEASE_NOTES_2_1_0.md`, and `docs/VALIDATION_2_1_0.md`.


## 2.2.0 Dynamic Compatibility Framework

Build 2.2.0 centralizes runtime datatype/connector handling into a metadata-driven adapter registry. The application no longer depends on one-off fixes for a specific table or column. Each discovered column receives a source projection, canonical transport strategy, target bind expression, deterministic validator, and review policy.

Open **Compatibility** after Discovery to see project-specific deterministic coverage, adapter families, binary-safe columns, review-required columns, and unknown/user-defined types. Runtime transport remains deterministic; Ollama is reserved for semantic conversion cases that deterministic rules cannot safely preserve.

Unknown or proprietary types are not guessed. They use a governed preservation/review path so the application fails safely rather than silently corrupting data. See `docs/RELEASE_NOTES_2_2_0.md` and `docs/VALIDATION_2_2_0.md`.

## 2.3.0 Semantic Medallion Factory

Build 2.3.0 introduces a true multi-stage target graph. A source table can now produce separate Source, Bronze and Silver nodes, and approved business semantics can add one or more Gold FACT/DIMENSION/AGGREGATE/KPI/REPORTING targets. Gold generation never occurs from an unapproved inference.

Open **Medallion Design** and use the sequence:

1. **Analyze consumers**
2. **Infer fact/dimension**
3. Review/approve inferred semantics or **Define explicit** semantics
4. **Build multi-stage plan**
5. **Generate stage artifacts**
6. Review/approve generated artifacts
7. **Deploy Medallion DEV**

External consumers not visible in SQL Server (Power BI, applications, files) can be explicitly registered. Stored procedures and functions are represented as logic assets and routed to Databricks SQL/PySpark/Workflow/manual review according to deterministic classification. Triggers remain architecture-review items.

See `docs/MEDALLION_SEMANTIC_FRAMEWORK_2_3_0.md` and `docs/RELEASE_NOTES_2_3_0.md`.

## Hybrid V2 Gemini semantic inference

This package includes an additive Hybrid V2 path for **Medallion Design → Infer fact/dimension**. The existing deterministic engine always runs first. When AI is enabled, only `REVIEW_REQUIRED` objects are sent to Gemini with discovered columns, PK/FK evidence, source definition and the deterministic scores. Gemini output is JSON-only, all returned column references are validated, approved definitions are never overwritten, and no AI recommendation is auto-approved or auto-deployed.

Configure only the local root `.env` (never commit or share the key):

```text
LLM_ENABLED=true
LLM_PROVIDER=GEMINI
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta
LLM_API_KEY=<your-gemini-api-key>
LLM_MODEL=gemini-2.5-flash
```

Restart the backend, use **Administration → Test AI Provider**, then open **Medallion Design → Analyze consumers → Infer fact/dimension**. Successful ambiguous results appear as `AI_RECOMMENDED` and still require governed semantic approval before Gold generation. If Gemini is unavailable or a response fails validation, the deterministic `REVIEW_REQUIRED` result is preserved.
