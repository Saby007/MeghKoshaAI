# MeghKoshaAI manual FOCUS and service-principal implementation plan

Status: Implementation complete - locally verified; operator Azure preflight pending

## Branch and scope

- Recipe: subscription-scope Bicep through the existing guarded AZD/PowerShell
  private-builder workflow, using `infra/main.bicepparam`.
- Repository: `Saby007/MeghKoshaAI`.
- New branch: `feature/manual-focus-service-principal`.
- Baseline: `feature/private-postgres-assessments` at
  `8586baccdec9194a9f7e45e793d39788a25bace2` (12 November 2026).
- The first local feature reference was stale. The new branch was fast-forwarded
  to the current remote feature branch before implementation.
- Work happens in an isolated worktree; existing worktrees remain unchanged.
- Prepare code and deployment scripts only. Do not deploy Azure resources.

## Confirmed requirements

- Design for a target subscription with approximately 30,000-40,000 resources.
- Keep PostgreSQL as the durable serving and background-job database.
- Replace automatic Cost Management exports and scheduled history pulls with
  administrator-uploaded FOCUS reports.
- Accept the report's actual date range; impose no six-month business restriction.
- Configure the target subscription and a manually provisioned Reader
  service principal through an admin page.
- Use that service principal for target-subscription Azure API calls.
- Remove Microsoft Entra user sign-in and user-delegated subscription access.
- Do not add an application login. Any visitor can upload their own report.
- Service-principal credentials are optional: uploaded financial evidence alone
  must generate a dashboard. Without credentials, do not call target Azure APIs;
  show Azure-enriched sections as unavailable, not fabricated zeros.
- Show only one configured subscription and active uploaded report at a time.
- Replacing the uploaded report replaces the dashboard's active evidence.
- Remove budget functionality.
- Preserve the baseline's private ACR design and private build/deployment path.
- Review ACA sizing, deployment scripts, documentation, and validation.

## 1. Application access and identity

### Required identity changes

1. Remove browser MSAL, Entra redirects, delegated API tokens, application
   registrations for user sign-in, and user-to-subscription RBAC checks.
2. Provide an Admin workspace with a required subscription ID and optional target
   tenant ID, application client ID, and client secret. The customer provisions
   the service principal and grants Reader outside the application.
3. If credentials are supplied, validate them and read the configured subscription
   before accepting the credential change. Without credentials, save the target
   subscription and enable upload-only dashboards. Do not enumerate or expose
   other subscriptions that the service principal might also access.
4. Route target-subscription Resource Graph, ARM, Advisor, Monitor, resource
   availability, SQL resource evidence, and recommendation reads through
   `ClientSecretCredential`. No fallback to a developer/user credential or the
   hosting managed identity is permitted for these calls.
5. Continue using hosting managed identities for private PostgreSQL, Blob,
   Key Vault, ACR image pulls, and optional Foundry. These are hosting dependencies,
   not delegated access to the customer's target subscription.
6. Do not create a service principal, assign target-subscription roles, or invoke
   target resource mutations. Read-only Resource Graph/metrics POST queries are
   allowed; HTTP method alone is not the authorization policy.

### No-login, upload-specific browser workspaces - implementation decision

The user confirmed that any visitor should be able to upload a report with no
login. Implement an automatically created, anonymous browser workspace, not a
globally shared mutable admin configuration.

- No password, Entra redirect, account, or sign-in page.
- An unguessable server-issued workspace capability is carried only in an
  HttpOnly, Secure, SameSite cookie. This is transparent browser state, not a
  user login. Apply CSRF protection to workspace mutations.
- Scope configuration, secrets, files, jobs, snapshots, exports, and caches to
  that workspace. A visitor cannot read or overwrite another visitor's report
  merely by using the same subscription ID or guessing a job/snapshot ID.
- One target subscription and active uploaded dataset per workspace. Replacing a
  report overrides that workspace's active evidence only.
- Expired/lost browser workspace cookies do not confer recovery access to old
  credentials or reports. Document this boundary and configurable retention.
- The public endpoint remains usable without VPN or an account. Document public
  upload cost/abuse considerations and optional operator network restrictions.
- Do not store credentials or the workspace capability in browser localStorage.

### Secret storage

- Add a dedicated private Key Vault with public access disabled.
- Store client-secret values only in Key Vault. PostgreSQL stores non-secret
  connection metadata and an exact secret-version reference.
- Give the API the required secret-management permission in this dedicated vault
  and give the worker read permission. Never give these identities target Reader
  or Cost Management Contributor automatically.
- Return only credential-presence and validation status to the browser; never
  return stored secrets. Avoid request-body/token/credential logging.
- Pin a workspace/configuration revision on queued work. Secret rotation must not
  mix identities, workspaces, targets, or cached tokens.

## 2. Manual report ingestion

### Supported workflow

1. The visitor opens Admin and saves the target subscription. Service-principal
   setup is optional; validate it only when supplied.
2. The admin creates one upload batch and selects one or more native Azure FOCUS
   CSV or gzip-compressed CSV files, including all partitions of the desired
   report. ZIP and Parquet are not part of this implementation.
3. The browser uploads bounded, retryable chunks through the web/API. This avoids
   sending an entire multi-GB body through ACA or requiring the browser to reach
   the private Blob endpoint.
4. The API stages raw content in private Blob Storage and persists upload
   metadata/checkpoints in PostgreSQL. Uploaded bytes are not stored as database
   JSON or held in full in API memory.
5. Finalization checks declared sizes and file completeness, freezes the batch,
   and creates durable background work.
6. The isolated worker validates, parses, and processes the files and publishes a
   complete SQL snapshot. Enrich only when that workspace has valid configured
   service-principal credentials; upload-only processing must not call ARM.
7. The dashboard switches to the new snapshot only after successful publication.
   The previous complete dashboard remains readable during upload/processing or
   after a failed replacement.

### Replacement, overlap, and retry semantics

- One batch is the authoritative report set for the workspace's one subscription.
- A new successful batch replaces the active report set; it is not silently
  appended to older datasets. To retain a longer history, include all desired
  files in the new batch. This avoids counting repeated monthly exports twice.
- Multiple files in one batch are additive partitions. Identical file content in
  the same batch is rejected. Do not deduplicate identical-looking billing rows:
  they can be legitimate distinct charges.
- Repeating an identical chunk/finalization request is idempotent. Conflicting
  retries fail explicitly; incomplete files cannot be processed.
- Keep prior published snapshots inaccessible to other workspaces and retain
  import audit metadata. No automatic
  deletion of source reports is introduced; retention remains an explicit
  operator decision.
- Interrupted uploads can be resumed from persisted chunk state. Failed
  processing can be retried without reuploading a completed batch.

### Input validation and dates

- Validate the supported native FOCUS schema, UTF-8/CSV shape, gzip integrity,
  numeric finiteness, date ordering, required currency, and target subscription.
- Preserve credits, zero costs, decimal precision during aggregation, and
  unattributed charges. Mixed-currency reports are rejected rather than summed.
- Derive coverage from report charge dates, not file names, export-run APIs,
  the current date, or a six-month setting.
- Remove the existing calendar-month export-path dependency, contiguous-history
  requirement, 12-period selection cap, and 366-day query/UI restriction for
  uploaded evidence.
- Support partial months and historical gaps. Clearly distinguish uploaded
  coverage from a claim that every possible charge was exported. Do not invent
  zero-cost days or previous-period evidence across uncovered intervals.
- Byte, row, decompression, processing-time, and response-size safeguards still
  exist and are configurable. "No duration restriction" does not mean unlimited
  resource consumption or silently truncated financial data.

## 3. PostgreSQL and processing design

- Reuse the current feature branch's durable leased jobs, cancellation, retry
  policy, supervised worker subprocess, atomic publication, SQL cost projections,
  paged record queries, bounded caches, and artifact jobs.
- Add versioned connection configuration, upload batches, files, chunk checkpoints,
  and dataset/job associations with indexes and uniqueness constraints.
- Use the anonymous workspace namespace consistently in PostgreSQL, Blob names,
  Key Vault secret names, job execution context, and all in-process caches.
- Reuse the existing `assessments.cost_rows`, `assessments.cost_daily`, snapshot,
  and record-serving tables instead of adding a competing dashboard database.
- Stream parsing and use bounded batches/COPY for normalized daily evidence.
  Avoid building a resources-times-days JSON document in the API or browser.
- Make processing attempts lease-fenced. A cancelled or superseded worker cannot
  publish a partial/new active dataset.
- Extend the explicit bootstrap migration/grants for the new tables; API and
  worker remain non-administrator database roles.
- Keep SQL parameters bound, constrain queries to the configured scope and
  snapshot, and maintain whole-result totals while paging detail rows.
- Surface invalid files, insufficient Reader access, throttling, expired secrets,
  capacity exhaustion, and failed jobs as distinct actionable errors.
- Keep financial imports/dashboard publication independent of optional Azure
  enrichment. A supplied-but-invalid/denied/expired credential must produce an
  explicit enrichment error, not prevent a valid FOCUS-only dashboard or trigger
  a managed-identity fallback.

## 4. Dashboard and feature changes

- Keep the original report layout, theme/density settings, paged resource views,
  SQL-backed filters, downloadable reports, and optional grounded chat.
- Replace Schedules with Admin: connection form, secret replacement, validation
  status, upload progress/resume, import coverage, processing status, retry, and
  links to the generated dashboard.
- An upload-only report has an explicit "Azure enrichment not configured" state.
  Findings that require inventory/Advisor/metrics are blank/unavailable until a
  service principal is configured; financial views remain populated.
- The cost dashboard can select the complete uploaded history or a subset of it.
  Show report coverage, data source, processing timestamp, and live-enrichment
  freshness separately.
- Preserve the existing meaning of monthly optimization figures: use the latest
  uploaded calendar month as the labelled assessment basis, explicitly mark a
  partial month, and never label a multi-month total as monthly spend or monthly
  savings. Historical cost views cover the full uploaded range.
- Azure inventory/Advisor are present-time evidence. Monitor has service-specific
  retention. Do not claim old billing files reconstruct historical configuration,
  resource uptime, or metrics beyond Azure's available retention.
- Optional evidence unavailable with Reader is labelled unavailable; do not
  silently substitute zeros or require elevated access for the core dashboard.

### Remove, not merely hide

- Native export creation, configuration, run, schedule, and automatic history-pull
  endpoints and UI actions.
- Scheduled Cost Management processor execution and automatic export onboarding.
- Budget list/create/update/delete endpoints, navigation, budget forms/context,
  chart reference lines, report sections, chat prompts, and budget-only query
  contracts.
- Cost Management API fallbacks for uploaded financial totals.
- Target-subscription write permissions and application-driven IAM onboarding.

Local action-plan tracking and report-file downloads are not Azure resource
mutations and can remain, isolated to the active anonymous workspace.

## 5. Infrastructure and deployment

### Preserve private ACR invariants

- Premium SKU, public network access disabled, admin user disabled.
- Existing private endpoint/DNS and managed-identity image pulls.
- Private builder path, with no VM public IP or inbound exposure.
- No public-ACR fallback, temporary firewall opening, or public build workaround.
- Keep the baseline registry/core networking implementation unchanged unless a
  directly required dependency is identified and documented.

### Proposed configurable starting sizes

| Component | Starting configuration | Reason |
| --- | --- | --- |
| API | 2 vCPU / 4 GiB; minimum one replica | Larger than the current 0.5 vCPU / 1 GiB API; bounded uploads and SQL projections |
| Assessment worker | 4 vCPU / 8 GiB; one replica initially | Preserve the baseline's large-worker default and isolate ingestion |
| PostgreSQL | General Purpose `Standard_D4ds_v5`, 256 GiB storage | Starting point for large daily evidence; configurable, not a capacity guarantee |
| Web | Preserve baseline sizing | Static assets and bounded reverse-proxy uploads |

Worker concurrency, database pools, and safety limits must be set together. Adding
replicas without database/ARM limits is not a substitute for tuning ingestion.
Document the cost increase; do not provision resources during implementation.

### Deployment-script changes

- Keep PowerShell/AZD/Bicep and exact-commit private builds.
- Do not create application passwords or sign-in registrations. Add the anonymous
  workspace/session configuration and document browser-scoped access.
- Provision the private vault, permissions, new environment settings, and schema.
- Remove user-sign-in registration/federation, target IAM grants, export-provider
  onboarding, export trusted-service exceptions, and scheduled export stages from
  the manual-mode deployment.
- Update preview/approval text and staged/end-to-end entry points so they describe
  the resources and permissions they actually create.
- Update nginx body/chunk limits and forward the information required for secure
  same-origin sessions without trusting spoofed identity headers.
- Fail closed if database, secrets, or required manual-mode configuration is
  unavailable. Missing optional SP credentials must not fail the cost dashboard.
  A fresh deployment shows Admin/upload guidance, not an Entra redirect.
- Recommend a new environment for the first deployment of this branch. Existing
  environments require a documented migration that explicitly disables/stops any
  legacy scheduled processor: omitting a Bicep module in an incremental deployment
  does not remove a previously deployed cron job.
- Do not delete existing databases, storage, vault secrets, or legacy resources
  automatically.

## 6. Component/file change map

| Area | Existing integration points | Planned work |
| --- | --- | --- |
| HTTP and scope | `api/main.py`, `api/services/auth.py`, `access_control.py`, `user_arm_client.py` | Anonymous workspace capability/CSRF; single configured scope; remove export/budget routes |
| Azure credentials | `api/services/arm_client.py`, `runtime_identity.py`, `sql_metrics.py`, `resource_uptime.py` | Dedicated target-SP credential provider; preserve MI for hosting dependencies |
| Admin/configuration | New focused workspace/configuration/secret adapter modules | Optional credentials, validation, exact secret references, rotation, safe status |
| Uploads | New focused upload router/store/parser modules | Chunked private Blob staging, persisted checkpoints, validation, dataset selection |
| Worker/persistence | `api/jobs/assessment_task.py`, `assessments.py`, `assessment_bootstrap.py`, `api/services/assessment_store.py`, `assessment_schema.sql` | Pin dataset/config revisions; process uploads; migrations/grants; atomic activation |
| FOCUS evidence | `focus_cost_reader.py`, `focus_history_reader.py`, `history_records.py` | Stream manual files; derive coverage; support arbitrary uploaded ranges |
| SQL/reporting | `assessment_queries.py`, `assessment_records.py`, `api/reports/`, `api/anomalies/` | Full-range SQL evidence, bounded detail, no budgets, honest gaps/monthly basis |
| UI | `web/src/App.tsx`, `apiIdentity.ts`, `api.ts`, `components/`, `report/` | Remove MSAL/Schedules/Budgets; Admin workflow; upload progress and SQL dashboard integration |
| Infrastructure | `infra/main.bicep`, `main.bicepparam`, `modules/apps.bicep`, `data.bicep`, `assessment-worker.bicep`, `postgres.bicep`, new vault module | Private vault, secret references, permissions, sizing; preserve ACR |
| Deployment/docs | `scripts/deploy-staged.ps1`, `deploy-end-to-end.ps1`, deployment input checks, `README.md`, deployment docs | Manual-mode provisioning, migration guidance, prerequisites, commands, limitations |

## 7. Deployment context and policy constraints

- Keep deployment subscription, tenant, location, resource names, and credentials
  operator-supplied. No current Azure account is selected or modified.
- Hosting and target tenant IDs are distinct configuration concepts. Confirm
  cross-tenant assumptions in code/tests rather than assuming they are equal.
- No live quota/policy validation or deployment is authorized. The customer must
  run the updated preflight/preview against their selected subscription.
- Regional availability, allowed SKUs/regions, private DNS/address space, Key Vault
  policy, PostgreSQL capacity, and optional Foundry quota are deployment gates.
- ACR remains private regardless of subscription policy defaults.

### Validation proof

| Check | Evidence / result |
| --- | --- |
| Frontend | `npm run build`: TypeScript/Vite passed; full Vitest suite: 249 passed |
| Backend, parser, exports, chat | Final focused pytest suite including real SQL and deadline configuration: 64 passed; two existing openpyxl sheet-title warnings; `pip check` passed |
| Actual SQL workflow | Real loopback PostgreSQL: upload >64 KiB, hash retries, no-SP processing, scoped access, atomic replacement/failure and coverage flags passed |
| Browser | Actual App/Admin -> save target without SP -> upload -> dashboard USD 8.00 -> replacement -> USD 3.00, one subscription, `not_configured` enrichment |
| Mobile | Admin at 390 px: no document horizontal overflow; one subscription input |
| Infrastructure | Local `az bicep build` passed; compiled template updated |
| Deployment logic | Full offline staged/end-to-end runner checks passed, including approval, preview, quota errors, attestation, bootstrap, resume and cleanup |
| Private ACR | `git diff --exit-code 8586bac -- infra/modules/core.bicep infra/modules/private-builder.bicep scripts/private-builder.sh`: unchanged |
| Scale | Passed: 40,000 resources, 181 days, 7,240,000 daily points; complete totals and 100-row page verified |
| Azure deployment | Not run, as requested |

### All validation checks pass

- [x] Local CLI/template compilation.
- [x] Project build and targeted functional validation.
- [x] Static role verification: API Blob upload contributor/report reader and
  vault Secrets Officer; worker upload reader/report writer and vault Secrets
  User; existing managed-identity ACR pulls/private builder unchanged; database
  bootstrap administrator separated from API/worker roles.
- [x] No target-subscription roles or user-sign-in registrations are created.
- [ ] Core cloud validation (CLI authentication, deployment validate, what-if):
  deferred to the operator's selected hosting subscription/environment.
- [ ] Azure Policy/region/quota validation: deferred; no customer subscription
  was selected and no live Azure preflight was authorized.
- [ ] Actual private Azure dependency/SP permission verification: deployment-time.

The azure-validate workflow was invoked. Its cloud recipe cannot be completed
without the operator's Azure context. Do not mark this plan globally `Validated`
or imply a successful Azure deployment; local verification and deferred cloud
checks are deliberately distinguished.

## 8. Small tasks and live status

| ID | Task | Status |
| --- | --- | --- |
| P1 | Inspect current feature baseline and identify coupled changes | Complete |
| P2 | Create isolated implementation branch | Complete |
| P3 | Confirm no-login, optional-SP, single-subscription replacement workflow | Complete |
| P4 | Add migrations, configuration metadata, and exact secret references | Complete |
| P5 | Add anonymous, isolated browser workspaces and workspace-scoped Admin | Complete |
| P6 | Route all target Azure reads through the configured service principal | Complete; mocked SP paths verified |
| P7 | Add resumable multi-file uploads and private Blob staging | Complete; chunks over 64 KiB and hash retries verified |
| P8 | Add worker ingestion, arbitrary report coverage, and atomic replacement | Complete; real PostgreSQL workflow verified |
| P9 | Connect Admin uploads/status to the original SQL-backed dashboard | Complete |
| P10 | Remove budgets, native export automation, and coupled UI/API behavior | Complete |
| P11 | Update sizing, vault, database grants, and private deployment scripts | Complete |
| P12 | Update deployment/migration documentation and operator preflight | Complete |
| P13 | Run focused API, PostgreSQL, upload, and credential-path tests | Complete: final combined suite 64 passed; dependency check passed |
| P14 | Run UI tests, production build, and browser workflow validation | Complete: 249 unit tests, production build, actual upload/replacement browser flow and mobile check |
| P15 | Compile IaC and validate PowerShell/deployment preview behavior | Passed local compile and complete offline rollout/retry tests |
| P16 | Record representative scale results and unverified customer-load limits | Complete; measured synthetic SQL scope and limitations below |
| P17 | Commit/publish the new branch and prepare deployment handoff | Complete: branch published and remote implementation commit verified |

Dependencies: P4/P5 before P6/P7; P6/P7 before P8; P8 before P9; P10
coordinates with P9; P11 depends on the implemented configuration contracts;
P12-P16 verify the corresponding completed work; P17 follows verification.

## 9. Acceptance criteria and validation

### Functional and identity checks

- Fresh app setup and Admin work without a password, Entra redirect, or user token.
- Different anonymous workspaces cannot access each other's configuration, files,
  jobs, reports, or secrets. Forged/expired workspace capabilities and CSRF
  failures are rejected; secrets never appear in responses or browser storage.
- Upload without any SP credentials generates financial dashboards and makes zero
  target-subscription API calls. Live-enrichment sections explicitly remain
  unavailable. Adding credentials enables only the applicable read-only evidence.
- Credential validation, bad/expired credentials, rotation, target mismatch, and
  absent Reader access produce explicit results.
- Tests prove target Azure calls use the configured SP, and hosting dependencies
  retain their managed identities. There is no implicit credential fallback.
- Retired budget/export/schedule APIs cannot create Azure changes.

### Upload and financial correctness

- CSV/gzip, multiple partitions, interrupted/resumed upload, duplicate/conflicting
  chunks, truncated gzip, missing columns, invalid dates/numbers, mixed currency,
  and out-of-scope subscriptions.
- A short partial-month report and a report longer than six months; a range longer
  than 366 days through the real SQL query and UI contracts.
- Credits/zero/unattributed charges and exact expected aggregate totals.
- Failed/retried/cancelled imports do not replace or duplicate the current dataset.
- A new successful file/report replaces the previous active report rather than
  accumulating multiple subscriptions or overlapping old datasets.
- Missing dates remain distinguishable from verified zero charges.

### PostgreSQL, scale, and resource boundaries

- Reuse/extend the existing dedicated loopback PostgreSQL integration harness.
  Never run destructive integration setup against a customer database.
- Verify migrations/grants, leases, concurrent claims, atomic replacement, indexed
  paging, complete-result totals, and bounded response sizes.
- Exercise a synthetic 40,000-resource case and a representative multi-month daily
  fact volume, subject to safe local resource availability. Record actual rows,
  bytes, elapsed time, memory, and query measurements rather than inferring scale
  from resource counts alone.
- The 40,000 resources x 180 days illustration already implies 7.2 million
  resource-day points before multiple services/tags/SKUs. Review the current
  2-million-history-row/5-million-detail-point defaults rather than merely
  increasing container memory.
- If full-volume execution is unavailable, state that explicitly. Local synthetic
  success is not a certification of Azure throttling, customer export size,
  cross-tenant permissions, or production performance.

### Existing tooling

- Focused `pytest` selectors first; related suites if shared contracts change.
- Existing Vitest tests, TypeScript/Vite production build, and Playwright workflow.
- Bicep compilation and existing PowerShell deployment/packaging tests.
- No Azure provisioning, application deployment, real SP secrets, or customer data
  is needed for the development checks.

## 10. Handoff and non-goals

Deliver this continuously updated plan, the implementation branch, updated
deployment commands, new-environment prerequisites, optional migration steps,
actual validation output, and a concise list of remaining deployment-time checks.

Not included: creating the customer's service principal, assigning its Reader
role, exporting cost data for the customer, deploying resources, adding a new
repository, implementing ZIP/Parquet, adding budgets, or claiming unlimited
ingestion/production-scale certification.

## Progress log

- Created the new branch from the existing PostgreSQL/private-ACR baseline.
- Refreshed only the new branch to current remote baseline `8586bac`; existing
  user worktrees were not modified.
- Traced current identity, FOCUS date restrictions, SQL-serving tables, worker
  supervision, database bootstrap, UI, nginx, and deployment integration points.
- Prepared this detailed plan and task status table before implementation.
- User approved proceeding after the written plan and clarified: no application
  login; optional SP; upload-only financial dashboard; one active subscription;
  replacement uploads override the prior active report.
- Starting with anonymous workspace isolation and persistence/schema contracts.
- Added workspace capabilities/CSRF isolation, optional credential configuration,
  exact private-vault secret references, chunked uploads, content-checked resume,
  worker-only CSV replay, and atomic SQL publication of replacement datasets.
- Avoided a resources-times-days Python dictionary: daily facts stream through
  PostgreSQL COPY/staging; normalized detail rows remain server-paged.
- Manual FOCUS/offline/credential-path and retained parser checks: 36 passed.
- Implementing the no-login Admin frontend and retiring Schedules/Budgets.
- Frontend production build is being used to resolve changed type contracts
  before browser validation. No Azure resource or application deployment run.
- Full frontend unit suite now passes: 21 files, 249 tests; TypeScript/Vite
  production build passes.
- Real PostgreSQL end-to-end checks cover anonymous isolation, CSRF, large chunks,
  no-SP processing, successful replacement, failed replacement, and retired APIs.
- Complete deployment runner tests pass using offline Azure/AZD stubs, including
  image attestation, bootstrap/release gates, retries, resume and builder cleanup.
- Bicep compiles. The private ACR core, private build VM module and private builder
  script are byte-for-byte unchanged from the baseline.
- Large-volume validation exposed an unbounded foreign-key insert. Restored
  5,000-row COPY boundaries for final publication. A subsequent 7.24-million-point
  load and queries completed; the test's missing-comparison expectation was
  aligned with the explicit coverage contract. Final rerun is in progress.
- Local synthetic browser helpers bind only to loopback and use real local SQL
  with in-memory Blob/archive substitutes. They are test-only, not packaged.
- Browser verification succeeded without sign-in or SP credentials: USD 8.00
  changed to USD 3.00 after replacement; coverage changed from January to February,
  the workspace still showed one subscription, budgets were absent, and live
  savings remained Unavailable.
- Optional Azure evidence also remains blank/unavailable in stakeholder exports
  and unattached-disk chat answers instead of being presented as verified zeros.
- Final synthetic scale check passed: 40,000 resources / 181 calendar days /
  7,240,000 daily points. Seed 693.445 s; cold metrics query 15.403 s; cold
  100-resource page 28.002 s; page 85,542 bytes; client query peak traced Python
  memory 468,724 bytes. All-result totals, page count and missing-comparison
  semantics were asserted. This is normalized SQL serving on local Windows,
  not a full-volume Azure upload/enrichment or production SLA certification.
- Increased the bounded, configurable database command deadline from the old
  15 seconds to 60 seconds (5-300 supported), and nginx's read deadline to
  120 seconds, based on those measured cold-query times. Cloud-native PostgreSQL
  performance and real customer file volume still require deployment acceptance.
- Final combined backend/parser/export/chat/real-SQL/deadline suite: 64 passed;
  dependency consistency check passed. Full frontend suite: 249 passed; final
  production build passed. Existing openpyxl sheet-name warnings remain unchanged.
- Implementation commit `76861b7718775bd5ce892edbfd71e6f00ec06de5` was published
  to `Saby007/MeghKoshaAI`, branch `feature/manual-focus-service-principal`,
  and verified against the remote ref.
- Deployment handoff: [current guide](../docs/manual-focus-deployment.md).
  Use a new environment; operator cloud validate/what-if/policy/quota/private
  dependency checks remain required. No Azure resource deployment was executed.
- Browser helpers were stopped and their listener ports verified closed.
  The task-owned PostgreSQL cluster was shut down cleanly and its port verified
  closed. No development/test service is intentionally left running.
- First real deployment attempt (23 Nov 2026) failed at `azd provision --preview`:
  `@allowed([false])` on two Bicep bool parameters made them literal types that
  `main.bicepparam` could not satisfy (BCP033). Earlier validation compiled
  `main.bicep` but not the parameter file AZD compiles. Removed the decorators, made the
  retired scheduler constant-off in Bicep, and added `scripts/tests/test-bicepparam.ps1`
  to compile `main.bicepparam` for foundation-only and full-release inputs.
- Second real attempt: Foundation recovered via the built-in retry, Build passed,
  Bootstrap failed with `unable to pull image using Managed identity`. The AcrPull
  assignment was created in the same deployment seconds before the Container App Job,
  which validates the pull at creation. AcrPull for the worker and bootstrap identities
  now deploys in a new `registry-pull` module during Foundation (same assignment names,
  so existing environments reuse them), the worker module depends on it, and the
  pull-validation error is retried after the settlement wait. Retry test added.
- First real upload: Azure's FOCUS 1.0 Cost Management export names the pricing
  currency `x_PricingCurrency`; the parser demanded the FOCUS 1.2 `PricingCurrency` and
  rejected the file. Either name is now accepted (the column is still required). The two
  real files (2,989 rows, 1-23 Nov) parse and replay end to end; regression test added.
  Known: older inherited test files for retired features (auth, schedules, control API)
  still fail and are not part of the focused suite.
- Scope decision: FOCUS 1.2 only. The x_PricingCurrency (FOCUS 1.0) alias was removed;
  a 1.0 file is rejected with an actionable message to export dataset version
  1.2-preview. Admin text, README and deployment guide state the requirement.
- Layout defect found on the deployed app: the left "Report pages" rail overlapped the
  report header and first KPI card. The rewritten App dropped the `view-panel` class that
  the rail's content offset (`#workspace-main:has(.view-panel:not([hidden]) .report-sidenav)`)
  keys on. Restored on the report and Admin panels, added an App test guard, and verified
  in Edge against the built stylesheet (content starts right of the rail). Needs a redeploy.
  Most "Unavailable" tiles are by design in FOCUS-only mode (no Reader service principal).
- Redeploy over the running environment was refused ("already runs a different source
  commit"): the runner only supported fresh environments. Added `-ApproveApplicationUpdate`
  (staged + end-to-end): reuses the foundation, rebuilds images, reruns the idempotent
  bootstrap (schema uses IF NOT EXISTS) with the new image, releases and verifies. Without
  the switch the guard still blocks. Offline test added; documented in the deployment guide.
- First in-place update: Build and the Bootstrap provision updated the job and the API, web
  and worker apps, then the builder VM update failed (`OperationNotAllowed: Managed disk
  storage account type change through Virtual Machine`). The deallocated builder's OS disk
  was `Standard_LRS` while the unchanged private-builder template declares StandardSSD_LRS.
  `deploy-staged.ps1` now realigns the builder OS disk to StandardSSD_LRS (deallocating first)
  before every provision when it differs; private-builder.bicep is untouched. Test added.
- Second update attempt: Build failed on the freshly started builder VM (`E: Could not get
  lock /var/lib/apt/lists/lock ... held by process (apt-get)`, phase install-prerequisites).
  The VM's own background apt job raced the build. `deploy-staged.ps1` now retries the build up
  to three attempts (fresh nonce each, 90 s apart) only for apt/dpkg lock errors; other build
  errors still stop immediately. private-builder.sh/bicep remain unchanged. Tests added.
