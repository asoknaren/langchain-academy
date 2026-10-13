# Requirements Document

## Introduction

P1 is a customer-support triage and resolution agent built on LangGraph. It ingests support tickets, routes each to a specialist sub-graph, drafts a reply grounded in the customer's history, and pauses for human approval before any risky action (refunds). Its purpose is twofold: solve a realistic support problem, and exercise the course concepts in one system: router, state reducers, input/output/private schemas, trim and summarize, checkpointer with thread storage, long-term memory (profile, collection, instructions) with Trustcall, Pydantic structured output, breakpoints and HITL, and deployment with double-texting handling.

**Baseline to beat:** keyword routing plus a single RAG chatbot with no memory.
**Targets:** higher first-contact resolution and routing accuracy than the baseline, and zero unapproved refunds.

**Scope:** The MVP is Requirements 1-6 and 11 (router, two specialists, refund approval, checkpointer, guardrails, evals). Requirements 7-10 and 12 are stretch.

**Decisions:**
- LLM: OpenAI API, `gpt-4o-mini` default for routing and drafting (configurable per role; embeddings `text-embedding-3-small`).
- Observability: LangSmith tracing.
- Auth (phase 1): simple password login in Streamlit with seeded users and roles; full auth deferred.
- Data: synthetic tickets, orders, and knowledge-base articles; mocked billing/order APIs.
- Architecture: Streamlit UI, FastAPI middleware with the LangGraph graph embedded, PostgreSQL (with pgvector) backend, a background worker, and a mock billing service, all in Docker Compose run locally.
- Service from the start (no notebook-first phase).
- Policy: maximum refund $100; approval timeout 24 hours.

## Glossary

- **System**: The P1 support agent as a whole.
- **Router**: The graph node that classifies a ticket into one intent with a confidence score.
- **Billing_Specialist**: The sub-graph handling billing and refund tickets.
- **Tech_Specialist**: The sub-graph handling technical-issue tickets.
- **Intent**: Pydantic model `{label, confidence}` produced by the Router.
- **Confidence_Threshold**: The configurable minimum Router confidence for automatic routing.
- **Human_Triage**: The queue where a human assigns tickets the Router cannot route.
- **Refund_Action**: A proposed refund with an amount, an order reference, and a reason.
- **Approval_Gate**: The breakpoint (`interrupt_before`) that pauses a run before a Refund_Action executes.
- **Reviewer**: The human who approves, edits, or rejects at the Approval_Gate.
- **Checkpointer**: The durable LangGraph state store keyed by `thread_id`.
- **Store**: The LangGraph long-term memory store, namespaced per customer.
- **Profile_Memory**: A single structured record per customer (plan, device, tone), updated with Trustcall.
- **Collection_Memory**: A searchable set of past issues and resolutions per customer.
- **Instructions_Memory**: Reply-style rules per support team, refined from reviewer feedback.
- **Trustcall**: The library used to patch memory records instead of overwriting them.
- **Reply_Draft**: The response produced for the customer.
- **Policy_Engine**: Deterministic code, outside the LLM, that enforces refund limits.
- **Eval_Set**: The labeled ticket dataset used to measure the System and the baseline.
- **Tool_Layer**: Typed tools wrapping the ticketing, order, and billing APIs.

## Requirements

### Requirement 1: Ticket Ingestion and Intent Routing

**User Story:** As a support lead, I want each ticket routed to the right specialist automatically, so that customers get relevant answers quickly.

#### Acceptance Criteria

1. WHEN a ticket is submitted, THE System SHALL create or resume a thread identified by the ticket identifier.
2. WHEN a ticket is ingested, THE Router SHALL return an Intent validated by a Pydantic schema containing exactly one label and a confidence between 0 and 1.
3. WHEN the Intent confidence is at or above the Confidence_Threshold, THE System SHALL route the ticket to the specialist for that label.
4. IF the Intent confidence is below the Confidence_Threshold, THEN THE System SHALL send the ticket to Human_Triage and SHALL NOT generate a customer reply.
5. IF the Router output fails schema validation, THEN THE System SHALL retry once and, if it still fails, send the ticket to Human_Triage.
6. THE Confidence_Threshold SHALL be configurable without code changes.

### Requirement 2: State Management

**User Story:** As a developer, I want predictable state merging, so that parallel steps do not overwrite each other.

#### Acceptance Criteria

1. THE System SHALL store conversation history using the `add_messages` reducer.
2. THE System SHALL record each executed action in `actions_taken` using an append reducer that removes duplicates by action identifier.
3. WHEN two branches write to `actions_taken` in the same step, THE System SHALL retain both entries.
4. THE System SHALL define separate input, output, and private state schemas.
5. THE System SHALL expose only `messages` and the final Reply_Draft status in the output schema.
6. THE System SHALL keep intermediate reasoning and scratch data in the private schema and SHALL NOT return them to API callers.

### Requirement 3: Billing and Technical Specialists

**User Story:** As a customer, I want my billing or technical issue answered accurately, so that I do not have to repeat myself.

#### Acceptance Criteria

1. WHEN the Billing_Specialist runs, THE System SHALL retrieve the order and billing data only through the Tool_Layer.
2. WHEN the Tech_Specialist runs, THE System SHALL retrieve relevant knowledge-base content before drafting a reply.
3. THE System SHALL produce a Reply_Draft that cites the source records or articles it used.
4. IF a tool call fails, THEN THE System SHALL retry with backoff up to a configured maximum and, if it still fails, escalate to Human_Triage with the failure reason.
5. THE System SHALL limit each run to a configured maximum number of graph steps and SHALL escalate to Human_Triage when the limit is reached.

### Requirement 4: Refund Approval (Human-in-the-Loop)

**User Story:** As a finance owner, I want every refund approved by a person, so that the agent cannot spend money on its own.

#### Acceptance Criteria

1. WHEN the Billing_Specialist proposes a Refund_Action, THE System SHALL pause at the Approval_Gate before executing it.
2. WHILE paused at the Approval_Gate, THE System SHALL persist the run state in the Checkpointer.
3. WHEN the Reviewer approves, THE System SHALL execute the Refund_Action exactly as presented.
4. WHEN the Reviewer edits the Refund_Action, THE System SHALL update the state with the edited values and execute only the edited action.
5. WHEN the Reviewer rejects, THE System SHALL NOT execute the Refund_Action and SHALL draft a reply explaining that the refund was declined or is under review.
6. IF no decision is received within the configured timeout, THEN THE System SHALL escalate to a senior Reviewer and SHALL NOT auto-approve.
7. THE Refund_Action execution SHALL use an idempotency key so that a resumed or retried run cannot refund twice.
8. THE Policy_Engine SHALL reject any Refund_Action above the configured maximum amount regardless of Reviewer approval or LLM output.
9. THE System SHALL record each Reviewer decision with the reviewer identity, timestamp, and original and final values.

### Requirement 5: Durable Threads and Resume

**User Story:** As an operator, I want conversations to survive restarts, so that a ticket can be continued by any agent or reviewer.

#### Acceptance Criteria

1. THE System SHALL persist state to a durable Checkpointer after each graph step.
2. WHEN a run is resumed with an existing `thread_id`, THE System SHALL continue from the latest checkpoint.
3. WHEN the process restarts while a run is paused at the Approval_Gate, THE System SHALL allow the Reviewer to resume it afterward.
4. THE System SHALL provide a way to inspect the checkpoint history of a thread.

### Requirement 6: Safety and Guardrails

**User Story:** As a security owner, I want untrusted text kept from controlling the agent, so that a malicious ticket cannot trigger actions.

#### Acceptance Criteria

1. THE System SHALL treat ticket text, retrieved documents, and tool outputs as untrusted data and SHALL NOT allow them to change tool permissions or the Policy_Engine configuration.
2. THE System SHALL grant each specialist only the tools it needs.
3. THE System SHALL validate every tool call's arguments against a typed schema before execution.
4. WHEN a ticket contains text attempting to override instructions (prompt injection), THE System SHALL NOT perform any action outside the specialist's allowed tools.
5. THE System SHALL redact personal data (email, phone, payment details) from traces and logs.
6. THE System SHALL store secrets outside graph state.

### Requirement 7: Conversation Length Management (Stretch)

**User Story:** As a developer, I want long tickets to stay within the context window and budget.

#### Acceptance Criteria

1. WHEN the message count exceeds a configured limit, THE System SHALL summarize older messages into a running summary.
2. WHEN a summary is created, THE System SHALL remove the summarized messages from state with `RemoveMessage`.
3. THE System SHALL filter bulky tool outputs before adding them to the model context.
4. THE System SHALL preserve the most recent configured number of messages unsummarized.

### Requirement 8: Customer Profile Memory (Stretch)

**User Story:** As a customer, I want the agent to remember my plan and preferences, so that I do not repeat them.

#### Acceptance Criteria

1. THE System SHALL store one Profile_Memory per customer in the Store under the namespace `(customer_id, "profile")`.
2. THE Profile_Memory SHALL be defined by a Pydantic schema.
3. WHEN a conversation reveals new profile facts, THE System SHALL update the Profile_Memory using Trustcall patching and SHALL NOT overwrite unrelated existing fields.
4. WHEN a new fact contradicts an existing one, THE System SHALL keep the newer value and record the previous value with a timestamp.
5. THE System SHALL write to memory only from customer and reviewer messages and SHALL NOT write text copied from tool outputs or retrieved documents.
6. THE System SHALL prevent reads and writes across customer namespaces.
7. THE System SHALL allow a customer's memory to be viewed, edited, and deleted on request.

### Requirement 9: Issue History Memory (Stretch)

**User Story:** As a support agent, I want past issues retrieved, so that failed fixes are not suggested again.

#### Acceptance Criteria

1. WHEN a ticket is resolved, THE System SHALL save an issue record (summary, cause, fix, outcome) in Collection_Memory under `(customer_id, "issues")`.
2. WHEN drafting a reply, THE System SHALL search Collection_Memory semantically and include relevant past resolutions in context.
3. WHEN a past fix is recorded as failed, THE System SHALL NOT propose it again for the same issue.
4. THE System SHALL avoid creating duplicate records for the same resolved issue.
5. THE System SHALL expire records older than a configured retention period.

### Requirement 10: Reply-Style Instructions Memory (Stretch)

**User Story:** As a support lead, I want my feedback on replies to improve future replies.

#### Acceptance Criteria

1. THE System SHALL store reply-style rules in Instructions_Memory under `(team_id, "instructions")`.
2. WHEN a Reviewer edits a Reply_Draft, THE System SHALL propose an updated instruction derived from the edit.
3. THE System SHALL apply an instruction update only after a support lead confirms it.
4. THE System SHALL load Instructions_Memory into the reply-drafting prompt for every run.

### Requirement 11: Evaluation and Observability

**User Story:** As a developer, I want measurable quality, so that I can show the agent beats the baseline.

#### Acceptance Criteria

1. THE System SHALL include an Eval_Set of at least 20 labeled tickets for the MVP, growing to at least 200.
2. THE System SHALL report routing accuracy, resolution correctness, unapproved-action count, and cost per ticket.
3. THE System SHALL run the same Eval_Set against the keyword-plus-RAG baseline and report results side by side.
4. THE Eval_Set SHALL include prompt-injection tickets and tickets that require a refund above the maximum amount.
5. THE unapproved-action count SHALL be zero for the System to pass.
6. THE System SHALL trace every run in LangSmith with metadata linking it to its thread and ticket, with personal data redacted before export.
7. THE System SHALL provide a regression run that fails when any metric falls below its recorded threshold.

### Requirement 12: Deployment and Concurrent Messages (Stretch)

**User Story:** As an operator, I want the agent served as an API, so that a support UI can use it.

#### Acceptance Criteria

1. THE System SHALL expose the graph through a FastAPI service with the graph embedded in the process.
2. THE System SHALL use Postgres-backed async Checkpointer and Store (with pgvector for semantic search).
3. WHEN a customer sends a new message while a run on the same thread is active, THE System SHALL serialize runs per thread using a per-thread lock or queue and apply `enqueue` by default.
4. THE System SHALL NOT cancel or restart a run that has executed a Refund_Action.
5. THE System SHALL stream progress updates to the Streamlit UI using server-sent events.
6. THE System SHALL run all components as Docker Compose services (ui, api, worker, postgres, mock-billing) started with a single command.
7. THE Streamlit UI SHALL establish the user through a simple password login against seeded users with a customer or Reviewer role, and THE API SHALL derive `customer_id`, `team_id`, and role from the authenticated session, never from the request body. Full authentication (SSO, token issuance) is deferred beyond phase 1.
8. THE System SHALL run a background worker that escalates approvals pending longer than 24 hours.
9. THE System SHALL keep application tables (tickets, approvals, audit log, eval results) under versioned migrations.
10. THE UI SHALL keep conversation state on the server by `thread_id` and SHALL provide separate customer and Reviewer pages.

## Non-Functional Requirements

1. **Cost:** The System SHALL enforce a per-ticket token budget and stop with escalation when exceeded.
2. **Latency:** The System SHOULD produce a Reply_Draft within a configured target for tickets with no human pause.
3. **Testability:** Every graph node SHALL be unit-testable with a mocked model and mocked tools.
4. **Portability:** The model provider SHALL be configurable behind a single interface.

## Out of Scope

- Real payment processor integration (mocked in this project).
- Multi-language support.
- Voice or email channel handling.
- Cloud hosting (local Docker only).

## Open Questions

None blocking. Exact OpenAI model names are configuration (`ROUTER_MODEL`, `DRAFT_MODEL`); `gpt-4o-mini` is the default.
