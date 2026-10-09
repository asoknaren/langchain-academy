# Phoenix CRM Agent Platform — Technical Specification

**Status:** Draft  
**Audience:** Product, CRM, application, data, security, and operations teams  
**Purpose:** Define a safe, testable first release of an agentic application that processes Phoenix CRM tickets with LangGraph. The first deployment target is a developer's local Docker environment; Azure is a later deployment target.

## 1. Summary and recommendation

Build a Python service with LangGraph workflows, a FastAPI integration layer, and a separate background worker. Phoenix remains the system of record. Do not give the LLM direct SQL access. Put CRM reads and approved writes behind a small, authenticated API that exposes business operations and enforces agency-level authorization.

For local development, use Docker Compose for the agent API, worker, PostgreSQL (LangGraph checkpoints, job inbox, and optionally `pgvector`), and a mock Phoenix API populated with synthetic tickets. For Azure, keep the application containerized and replace local adapters with Azure Service Bus, Azure Database for PostgreSQL, Azure OpenAI/Microsoft Foundry, Key Vault, and Azure Monitor/Application Insights as appropriate.

Start with one bounded use case—ticket triage and solution recommendations. Add customer pulse, knowledge-base drafts, ticket linking, and Teams/chatbot access as separate workflows after the integration and safety controls are proven.

## 2. Goals

- Analyze new and updated CRM tickets and return structured, evidence-backed recommendations.
- Find relevant historical tickets, fixes, known issues, and approved knowledge articles.
- Generate a customer/agency pulse from explicitly scoped open defects, enhancements, and interface work.
- Draft a knowledge-base article when a ticket is returned to a customer, checking for duplicates first.
- Suggest related tickets and provide authorized, read-only ticket status through a chatbot or Teams.
- Support resumable, observable workflows and human review before consequential actions.
- Develop and test locally without production credentials or production customer data.
- Preserve a migration path to Azure without coupling graph logic to a particular cloud service.

## 3. Non-goals for the first release

- Autonomous production changes to CRM ticket status, assignments, priorities, or customer-visible comments.
- An LLM-generated replacement for Phoenix business rules or its authorization model.
- Arbitrary natural-language-to-SQL execution.
- Autonomous multi-agent delegation. Begin with explicit LangGraph nodes and workflows; split agents only if a measured need emerges.
- Making customer-facing diagnoses or promises without a human review and source evidence.

## 4. Users and entry points

| User/system | Initial interaction |
|---|---|
| Phoenix CRM | Sends a signed ticket-created/updated event, or an integration service polls and submits changes if webhooks are unavailable. |
| Support analyst | Views triage results, evidence, related tickets, and a proposed response; approves, edits, or rejects drafts. |
| Support lead/account owner | Requests or schedules an agency pulse and reviews the sourced summary. |
| Customer-facing support user | Reviews and publishes a proposed KB article; article creation is not automatic in v1. |
| Teams/chatbot | Requests ticket status for an authenticated user; only returns data that user is authorized to see. |
| Operator | Inspects job state, retries safe failures, and reviews traces/audit events. |

## 5. Proposed architecture

```text
Phoenix CRM
  └─ signed webhook / integration poller
       └─ FastAPI ingress ──> durable job inbox (local PostgreSQL)
                                 └─ background worker
                                      └─ LangGraph workflows
                                           ├─ Phoenix API tools (read; constrained writes later)
                                           ├─ retrieval (historical fixes, KB, related tickets)
                                           ├─ LLM provider adapter
                                           └─ approval interrupt / proposed action

Analyst UI or Teams bot ──> authenticated API ──> status/read-only graph or CRM API

All components ──> structured logs, metrics, traces, audit records
```

The API accepts events and user requests quickly; it does not keep a Phoenix request open while the model runs. The worker processes jobs asynchronously. Each job has a stable idempotency key and a LangGraph `thread_id`, so retries and human-review resumes do not accidentally create duplicate work.

### Component responsibilities

- **Integration/API:** Validate event signatures, authenticate callers, validate schemas, enforce request limits, deduplicate events, create jobs, and expose workflow/status endpoints.
- **Worker:** Claim queued jobs, invoke the relevant graph, handle retryable failures, persist checkpoints, and emit completion/failure events.
- **LangGraph workflows:** Orchestrate typed steps, tool calls, evidence collection, conditional routing, approval interrupts, and resumable execution. Keep domain rules in deterministic code where possible.
- **Phoenix API:** Authoritative, least-privilege interface to CRM data and permitted CRM actions. It enforces agency/tenant scoping on every call.
- **Retrieval service:** Search a curated corpus of permitted historical tickets, resolutions, and published KB articles. Return source identifiers and access-control metadata with every result.
- **Persistence:** PostgreSQL holds job state, LangGraph checkpoints, and optional local vector data. Phoenix SQL Server remains authoritative for tickets.
- **Notification adapter:** Sends completion/approval notifications to supported channels. It must not include sensitive ticket data in notifications by default.

## 6. Workflow specifications

### 6.1 Ticket defect triage and solution recommendation

1. Receive a ticket event, validate it, and enqueue an idempotent job.
2. Fetch the current ticket and its allowed agency/context through the Phoenix API.
3. Normalize the ticket into a bounded, typed input. Treat descriptions, attachments, and retrieved ticket text as untrusted content.
4. Retrieve the most relevant resolved tickets, fixes, known issues, and published KB articles.
5. Ask the model for a structured analysis: issue category, concise summary, likely causes, recommended next steps, confidence, missing information, and references to evidence.
6. Validate the response against a schema and verify that cited source IDs were actually retrieved and are accessible to the requesting agency.
7. Save the recommendation as a draft linked to the ticket; notify the analyst.
8. On approval, a separately authorized action can post the approved response or update an allowed field. Record the approver, exact approved payload, and CRM result.

**Output must distinguish evidence from inference.** If evidence is weak, sources conflict, or required details are missing, the workflow should request clarification or escalate instead of presenting a confident diagnosis.

### 6.2 Agency/customer pulse

1. Require an authenticated request and an agency identifier the caller is authorized to access.
2. Fetch open defects, pending enhancements, and open interface developments from the Phoenix API as of a defined timestamp.
3. Aggregate deterministic counts, aging, priority, ownership, and status before asking the model to summarize themes, blockers, risks, and recent changes.
4. Produce a sourced summary with the reporting period, data timestamp, ticket links/IDs, and explicit caveats for incomplete data.
5. Store the report with its scope and requester; avoid mixing data across agencies.

The summary is advisory. It does not change CRM records.

### 6.3 Knowledge-base draft on ticket return to customer

1. Trigger only on a configured, authenticated Phoenix status transition (not on arbitrary ticket text).
2. Check whether the ticket has enough confirmed resolution information to support an article. If not, skip and record why.
3. Search existing published KB articles and pending drafts for likely duplicates.
4. Draft an article using only approved ticket fields and evidence. Exclude internal notes, secrets, personal data, and unsupported claims.
5. Attach source ticket IDs and duplicate candidates; send to an authorized reviewer.
6. Publish only after explicit human approval through the authorized KB API.

### 6.4 Similar-ticket linking

1. Retrieve candidates using structured filters (agency/product/version/status) and semantic similarity.
2. Return ranked candidates with similarity rationale and links to source tickets.
3. Require an analyst to approve a link in v1. Do not merge, close, or alter ticket status based solely on similarity.
4. Track accepted/rejected suggestions to evaluate retrieval quality.

### 6.5 Ticket status via Teams/chatbot

1. Authenticate the user through the channel identity provider.
2. Resolve the user's authorization and agency scope server-side; never trust an agency ID supplied only in chat text.
3. Parse a ticket identifier, call a read-only status operation, and return a concise response with a link and last-updated time.
4. If access cannot be established or the ticket is not found, return a safe response without disclosing whether an unauthorized ticket exists.

## 7. LangGraph design

Use separate, typed graph entry points for `ticket_triage`, `agency_pulse`, `kb_draft`, `ticket_link_suggestions`, and `ticket_status`. Share narrow nodes/tools where useful, but do not put every behavior behind one unrestricted conversational agent.

### Suggested graph state

- `request_id`, `thread_id`, `workflow_type`, `initiator`
- `agency_id` and authorization context (claims/scopes, not reusable credentials)
- `ticket_id` or validated query parameters
- `input_snapshot` and `data_as_of`
- `retrieved_evidence`: source IDs, excerpts, metadata, and access scope
- `analysis` / `draft` as schema-validated values
- `proposed_actions` and approval status
- `errors`, retry metadata, and completion status

Keep state minimal: do not store API keys, access tokens, unnecessary full ticket bodies, or unredacted sensitive data in checkpoints. Define retention and deletion for checkpoints and traces. Checkpointer persistence must be enabled for jobs that may resume after process restarts or human approval.

### Graph behavior rules

- Prefer deterministic fetch/validate/authorize nodes around a constrained model step.
- Set explicit recursion, time, token, and tool-call limits.
- Validate every model output before it can leave the service or reach a write tool.
- Model-generated text is never authorization. Re-authorize at the API boundary immediately before every write.
- Use LangGraph interrupts for review/resume; bind approvals to a specific draft version and payload hash so edits invalidate old approvals.
- Tool failures should be explicit and observable. Do not convert missing data or exceptions into a successful-looking answer.

## 8. SQL Server and tool/API boundary

### Recommendation

For the first release, build a **Phoenix CRM API** that wraps the existing SQL Server schema and business rules. Give the agent tools that call this API rather than direct SQL connections. Add MCP as a protocol adapter if it helps expose the same tools to multiple agent clients; MCP should call the same authorization-enforcing API, not bypass it.

This keeps the graph independent of schema changes, prevents arbitrary queries, and centralizes access control, auditing, and write validation. If Phoenix cannot host endpoints, use a separately deployed integration API with equivalent identity and policy checks.

### Initial tool surface (illustrative)

**Read-only**

- `get_ticket(ticket_id)`
- `list_agency_work(agency_id, categories, statuses, updated_after, page_token)`
- `search_resolved_tickets(agency_id, product, version, query, limit)`
- `search_knowledge_articles(agency_id, query, limit)`
- `get_ticket_history(ticket_id)`

**Approval-gated, later phase**

- `create_ticket_comment(ticket_id, approved_text, approval_id, idempotency_key)`
- `update_ticket_fields(ticket_id, allowed_fields, approval_id, idempotency_key)`
- `create_or_publish_kb_draft(source_ticket_ids, approved_article, approval_id, idempotency_key)`
- `link_tickets(ticket_id, related_ticket_id, approval_id, idempotency_key)`

The API must enforce field allowlists, ticket/agency relationships, caller roles, optimistic concurrency where available, request size limits, rate limits, pagination, and audit logging. Never expose a generic `execute_sql` tool. The CRM API's service identity should have only the database permissions needed for its endpoints.

### API contract requirements

- Versioned OpenAPI contract and typed request/response models.
- Correlation ID and idempotency key on event/job creation and side-effect operations.
- Consistent error categories (`unauthorized`, `not_found`, `conflict`, `rate_limited`, `unavailable`, `validation_failed`).
- Cursor pagination and explicit maximum page sizes.
- `updated_at` / version fields so results and approvals can detect stale records.
- Agency scope enforced in the server-side query, not filtered after retrieval by the agent.

## 9. Data and retrieval

**System of record:** Phoenix SQL Server, accessed through the API.  
**Agent operational store:** PostgreSQL for job inbox/outbox, workflow status, and LangGraph checkpoints.  
**Similarity index:** For local prototyping, use PostgreSQL with `pgvector` or an interchangeable local vector store on a sanitized sample corpus. In Azure, evaluate Azure AI Search for hybrid keyword/vector retrieval. Re-indexing must be repeatable from approved source data.

Every indexed item should retain source ID, agency/tenant scope, product/version, ticket status, resolution date, publication state, and source ACL metadata. Search must apply authorization scope before returning content. Define source-of-truth precedence (published KB and verified fixes ahead of unverified ticket discussion) and exclude deleted, restricted, or unresolved records as appropriate.

No real customer data should be copied into local fixtures, vector indexes, prompt logs, or test recordings without explicit data-owner approval and adequate controls.

## 10. Local Docker development

Recommended local Compose services:

1. `agent-api` — FastAPI ingress, status, and authenticated local development endpoints.
2. `agent-worker` — LangGraph workflow execution.
3. `postgres` — durable jobs and graph checkpoints; optionally enable `pgvector`.
4. `phoenix-mock-api` — synthetic data implementing the Phoenix API contract.
5. Optional local tracing UI/exporter, enabled only when useful.

Use Docker secrets or untracked environment files for local model credentials; commit only a `.env.example` with placeholders. Bind local ports to loopback where possible. Health checks and readiness should verify database/API connectivity. The app should start without a live Phoenix connection when using the mock API.

Use migrations and seeded synthetic fixtures for repeatable setup. Provide one command to start services and one command to run tests. Do not make Docker Compose a production deployment mechanism.

## 11. Azure deployment direction

Keep the API and worker as separate container roles so they can scale and deploy independently. A likely starting topology is:

- Azure Container Apps for the API and background worker containers.
- Azure Service Bus for durable asynchronous event/job delivery and dead-letter handling.
- Azure Database for PostgreSQL for LangGraph checkpoints and operational state.
- Azure OpenAI or Microsoft Foundry model deployment behind a provider adapter.
- Azure Key Vault and managed identities for secrets and service-to-service access.
- Azure Monitor/Application Insights with OpenTelemetry for traces, metrics, and logs.
- Phoenix API hosted/reached through the organization's approved network boundary; use private networking where required.
- Azure AI Search if evaluation shows hybrid/vector retrieval is needed at scale.

These are candidate services, not final sizing decisions. Confirm data residency, networking, identity, scale, retention, model availability, and cost requirements before deployment. Do not expose PostgreSQL, SQL Server, or internal tools publicly. Keep cloud-specific code in adapters and infrastructure configuration, not in graph decision logic.

## 12. Security, privacy, and safety requirements

- Authenticate every webhook, user, worker, and service call. Use managed identity in Azure and short-lived credentials where supported.
- Authorize every CRM read and write against the initiating user's/agency's scope; apply the same policy to retrieval.
- Treat ticket descriptions, email text, attachments, and retrieved documents as untrusted input that may contain prompt injection.
- Do not let retrieved content change tool permissions or workflow policy. Tools are allowlisted and have strict schemas.
- Require human approval for customer-visible responses, KB publication, ticket links, and record changes in v1.
- Redact or minimize PII in prompts and observability. Define data retention for prompts, traces, checkpoints, and generated reports.
- Encrypt data in transit and at rest; restrict access to secrets, indexes, checkpoints, logs, and approval endpoints.
- Record a tamper-evident audit trail for requester, agency scope, source IDs, model/version, output version, approvals, tool calls, and CRM result.
- Provide a kill switch to disable a workflow or all write tools without redeploying.
- Define content-safety, escalation, and refusal behavior for sensitive or out-of-scope tickets.

## 13. Reliability, idempotency, and operations

- Expect at-least-once event delivery. Deduplicate by source event ID or `(ticket_id, source_version, workflow_type)`.
- Make side-effect calls idempotent and persist their idempotency keys.
- Retry only transient failures with bounded exponential backoff and jitter; do not retry validation, authorization, or policy failures.
- Send exhausted jobs to a dead-letter state/queue with reason and correlation ID; provide an operator-controlled replay path.
- Use per-workflow timeouts, maximum graph steps, concurrency controls, and model rate limits.
- Track job states such as `received`, `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, and `cancelled`.
- Reconcile periodically against Phoenix for missed or out-of-order updates if webhook delivery cannot be guaranteed.
- Separate readiness from liveness; a worker that cannot access its checkpoint store should not claim jobs.

## 14. Observability and evaluation

For each job, capture correlation ID, workflow, elapsed time, node transitions, tool names and outcomes, model/deployment/version, token/cost estimates, retry count, and final status. Avoid logging full prompts, ticket bodies, credentials, or sensitive output by default.

Define dashboards/alerts for queue age/depth, failures, retries, dead letters, model/API latency and throttling, approval wait time, cost, and write-tool outcomes.

Build an evaluation set from approved, de-identified examples. Measure:

- Ticket category and recommendation quality, reviewed by support SMEs.
- Retrieval precision/recall and whether the cited fix actually supports the recommendation.
- Unsupported-claim rate and required escalation/clarification behavior.
- Agency isolation and access-control failures (must be zero).
- Duplicate KB detection quality and ticket-link acceptance rate.
- Customer-pulse factuality against source records.
- Workflow completion, latency, cost, and recovery after worker restart.

Use regression evaluations for prompt/model/retrieval changes. Log human edits and accept/reject outcomes as evaluation signals, not as unreviewed automatic training data.

## 15. Testing strategy

- **Unit:** State schemas, graph routing, output validation, policy decisions, deduplication, and retry classification.
- **Contract:** Phoenix API schemas, authorization behavior, pagination, error handling, idempotency, and mock/real adapter parity.
- **Graph integration:** Each workflow against a fake model and synthetic Phoenix API; assert exact nodes/actions and no unauthorized tool calls.
- **Safety/security:** Cross-agency access tests, prompt-injection fixtures, malformed model responses, stale approvals, replayed events, and PII redaction.
- **Persistence/recovery:** Worker termination and restart during execution; checkpoint resume; duplicate event delivery; approval interruption and resumption.
- **Evaluation:** Golden test set with SME scoring and measurable thresholds set before pilot.
- **End-to-end:** Compose smoke test for event receipt through job completion and approval flow.

## 16. Phased delivery

### Phase 0 — Discovery and contracts

- Confirm Phoenix event mechanism, ticket schema/statuses, agency isolation rules, identity provider, permitted write operations, and KB ownership.
- Agree on the Phoenix API OpenAPI contract and sample synthetic payloads.
- Choose the model/provider and approved data handling/retention policy.

### Phase 1 — Local vertical slice

- Compose API, worker, PostgreSQL, and Phoenix mock.
- Implement one ticket-triage graph, read-only tools, evidence references, checkpointing, and analyst-review output.
- Add idempotency, structured logs/traces, tests, and a small reviewed evaluation set.

### Phase 2 — Pilot workflows

- Add agency pulse, similarity suggestions, and KB drafts behind role-based access and approval.
- Integrate with a non-production Phoenix API and a small authorized pilot group.
- Tune retrieval, model, evaluation thresholds, and operational dashboards.

### Phase 3 — Azure production readiness

- Provision target Azure services and networking through infrastructure as code.
- Add managed identities, Key Vault, Service Bus, private endpoints/network rules, monitoring/alerts, retention, and disaster-recovery procedures.
- Complete security/privacy review, load/cost tests, incident runbooks, and staged rollout with feature flags.

### Phase 4 — Controlled automation

- Consider narrowly scoped automatic actions only after measured quality, rollback/recovery, approval/audit, and business-owner sign-off. Keep high-impact actions human-approved.

## 17. Acceptance criteria for the first pilot

- A repeated Phoenix event does not create duplicate jobs or duplicate CRM side effects.
- A ticket event is accepted quickly and processed asynchronously; job status is queryable by correlation ID.
- A worker restart resumes a checkpointed job or marks it failed with a clear recoverable reason.
- Triage output conforms to a schema, cites accessible source records, and labels inference/uncertainty.
- No model output can invoke a write without an authenticated, current, payload-bound approval.
- A caller cannot retrieve another agency's tickets through direct API, retrieval, status, or summary paths.
- Missing Phoenix/retrieval/model dependencies are reported as failures, not empty successful results.
- All core workflows pass automated tests against synthetic data, and SME evaluation scores meet thresholds agreed before pilot.

## 18. Decisions to resolve before implementation

1. Does Phoenix support signed webhooks for ticket creation and relevant status transitions? If not, what is the supported polling/integration mechanism and acceptable delay?
2. Which identity provider and user roles determine ticket and agency-level access, including Teams users?
3. Which Phoenix operations may the first release perform, and which must always require human approval?
4. What ticket fields, comments, attachments, and customer information may be sent to the selected model provider? What are retention and residency requirements?
5. Where are resolved fixes and KB articles stored, who owns publication, and what constitutes a duplicate?
6. What SQL Server/API environment can developers use, and can synthetic or de-identified test data be provided?
7. Which Azure region, subscription, networking boundary, and operational owner are intended for deployment?
8. What are the expected ticket volume, concurrency, response-time targets, and model/hosting budget?

## 19. Key risks and mitigations

| Risk | Mitigation |
|---|---|
| Incorrect or unsupported recommendations | Source-backed structured output, uncertainty/escalation path, SME evaluation, human review. |
| Cross-agency data exposure | Server-side authorization on every API/search path; negative isolation tests. |
| Prompt injection in ticket or KB text | Treat content as data, keep tools/policies outside model control, restrict and validate tools. |
| Duplicate or out-of-order events | Durable inbox, idempotency keys, source versions, and reconciliation. |
| Schema changes couple the graph to SQL Server | Versioned Phoenix API; keep DB details behind its service boundary. |
| Sensitive data leaks through logs/checkpoints | Minimize persisted content, redact telemetry, restrict access, apply retention/deletion policies. |
| Local prototype behaves differently from production | Use adapter/contract tests and exercise the same API/tool schemas in Compose and Azure. |
