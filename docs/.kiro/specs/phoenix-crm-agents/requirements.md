# Requirements Document

## Introduction

Phoenix is a CRM/ticketing product used by agency admins (external customers) and internal members. Tickets are created in an Azure-hosted website backed by SQL Server. This feature delivers an agentic AI backbone that runs in the background, reacts to ticket lifecycle events, and performs intelligent actions across five capabilities: defect handling, customer pulse summaries, knowledge-base article creation, ticket status lookup, and similarity linking.

The system is built in Python on LangGraph using an event-driven asynchronous model: the website publishes ticket events that specialist agents consume in the background. A supervisor graph routes each event to the appropriate specialist sub-graph. Agents access CRM data exclusively through a dedicated REST API over SQL Server, exposed to agents as typed tools. The large language model provider is Azure OpenAI behind a pluggable interface, agent run state is persisted in a PostgreSQL durable checkpointer, and the first production stance is recommend-only human-in-the-loop with a roadmap toward tiered autonomy.

Because the system processes multi-tenant customer data, four enterprise concerns are non-negotiable for the first production release (V1): tenant isolation and authorization, prompt-injection and output guardrails, idempotency with a dead-letter queue, and an immutable audit trail. Additional enterprise concerns phase in after V1.

This requirements document covers all five capabilities and the enterprise cross-cutting concerns. Capability 1 (defect handling) is scoped as the first full end-to-end vertical slice; capabilities 2 through 5 are specified for scaffolding with capability-complete acceptance criteria.

## Glossary

- **System**: The Phoenix CRM agentic AI backbone (the `phoenix-crm-agents` service) as a whole.
- **Supervisor**: The LangGraph routing graph that classifies an inbound ticket event and dispatches it to a specialist sub-graph.
- **Defect_Handler**: The specialist sub-graph that analyzes a defect ticket, finds the closest matching prior defects, and proposes a grounded solution.
- **Customer_Pulse**: The specialist sub-graph that summarizes an agency's open defects, pending enhancements, and open interface developments.
- **KB_Creator**: The specialist sub-graph that drafts a knowledge-base article when a ticket is assigned back to a customer.
- **Status_Responder**: The specialist sub-graph that answers ticket-status queries synchronously.
- **Similarity_Linker**: The specialist sub-graph that links related or duplicate tickets based on semantic similarity.
- **Event_Consumer**: The component that consumes ticket lifecycle events from the queue and submits them to the Supervisor.
- **Data_API**: The dedicated REST API over SQL Server that exposes CRM read and write operations; agents access CRM data only through this API.
- **Query_API**: The synchronous REST endpoint used for ticket status lookups (capability 4).
- **HITL_Controller**: The control endpoint that receives human approve, reject, or edit decisions and resumes a paused agent run.
- **Tool_Layer**: The set of typed LangChain tools wrapping the Data_API and Embedding_Store that agents invoke.
- **Embedding_Store**: The pluggable vector-storage interface used for semantic search over defects and knowledge-base articles.
- **LLM_Provider**: The pluggable large language model provider interface, defaulting to Azure OpenAI.
- **Checkpointer**: The PostgreSQL-backed LangGraph durable state store keyed by thread identifier.
- **Audit_Log**: The append-only store recording every agent action.
- **Agency**: An external customer tenant whose admins may access only that tenant's data.
- **Tenant_Context**: The authenticated agency identity derived from the request credentials, used to scope all data access.
- **Ticket_Event**: A message published by the website describing a ticket lifecycle change (for example, created or updated).
- **Recommendation_Draft**: A proposed solution or action produced by an agent and held in draft state pending human approval.
- **DLQ**: The dead-letter queue that receives events that fail processing after bounded retries.
- **Recommend_Only_Mode**: The operating mode in which the System produces drafts that require human approval before any customer-facing effect.

## Requirements

### Requirement 1: Event-Driven Ticket Processing

**User Story:** As an internal member, I want ticket lifecycle events to be processed automatically in the background, so that agentic actions happen without manual triggering.

#### Acceptance Criteria

1. WHEN a Ticket_Event is published to the queue, THE Event_Consumer SHALL consume the Ticket_Event and submit it to the Supervisor.
2. WHEN the Supervisor receives a Ticket_Event, THE Supervisor SHALL classify the Ticket_Event into exactly one capability category.
3. WHEN the Supervisor has classified a Ticket_Event, THE Supervisor SHALL route the Ticket_Event to the specialist sub-graph that corresponds to the classified category.
4. IF the Supervisor cannot classify a Ticket_Event into a defined capability category, THEN THE Supervisor SHALL route the Ticket_Event to the DLQ with a classification-failure reason.
5. WHEN a specialist sub-graph begins processing a Ticket_Event, THE System SHALL persist the run state in the Checkpointer keyed by a thread identifier derived from the Ticket_Event.
6. THE Event_Consumer SHALL process Ticket_Events asynchronously without blocking the website that published the Ticket_Event.

### Requirement 2: Durable State and Resumable Runs

**User Story:** As an operator, I want agent runs to persist their state durably, so that runs can pause, resume, and survive restarts.

#### Acceptance Criteria

1. WHEN an agent run reaches a persistence point, THE System SHALL write the run state to the Checkpointer keyed by the run's thread identifier.
2. WHEN an agent run is resumed with a thread identifier that has persisted state, THE System SHALL restore the run from the most recent checkpoint for that thread identifier.
3. WHILE an agent run is paused at a human-approval point, THE System SHALL retain the run state in the Checkpointer until a resume decision is received.
4. IF the System restarts while a run is paused, THEN THE System SHALL retain the paused run state so the run can resume after restart.

### Requirement 3: CRM Data Access Through the Data API

**User Story:** As a security owner, I want agents to access CRM data only through a dedicated REST API, so that data access is controlled and SQL Server is never touched directly by agents.

#### Acceptance Criteria

1. WHEN an agent needs CRM data, THE System SHALL retrieve the CRM data through the Data_API via the Tool_Layer.
2. THE System SHALL expose Data_API read operations for retrieving a ticket, listing an Agency's open items, searching defects, retrieving recent fixes, and searching knowledge-base articles.
3. THE System SHALL expose Data_API write operations for writing a Recommendation_Draft, creating a knowledge-base draft, and linking tickets.
4. WHEN the Tool_Layer invokes a Data_API operation, THE Tool_Layer SHALL validate the operation inputs against the operation's input schema before sending the request.
5. IF a Data_API operation returns an error, THEN THE Tool_Layer SHALL return a structured error result to the calling agent without raising an unhandled exception.
6. THE System SHALL provide a mock Data_API implementation seeded with sample tickets, defects, fixes, and knowledge-base articles for local development.

### Requirement 4: Pluggable LLM Provider

**User Story:** As a developer, I want the language model provider to be pluggable, so that I can use Azure OpenAI in production and a local or alternative provider in tests.

#### Acceptance Criteria

1. THE System SHALL access the language model through the LLM_Provider interface for all chat and embedding operations.
2. WHERE the configured provider is Azure OpenAI, THE LLM_Provider SHALL route chat and embedding requests to Azure OpenAI.
3. WHERE the configured provider is an alternative provider, THE LLM_Provider SHALL route chat and embedding requests to that alternative provider.
4. WHEN the LLM_Provider is initialized, THE System SHALL select the provider from environment-driven configuration.

### Requirement 5: Defect Handling — Analysis and Closest Match

**User Story:** As an internal member, I want the system to analyze a new defect and surface the closest prior defects, so that I can resolve the defect faster using prior knowledge.

#### Acceptance Criteria

1. WHEN the Defect_Handler receives a defect Ticket_Event, THE Defect_Handler SHALL load the ticket and its context through the Data_API.
2. WHEN the Defect_Handler has loaded the defect ticket, THE Defect_Handler SHALL generate an embedding of the defect text through the LLM_Provider.
3. WHEN the Defect_Handler has generated the defect embedding, THE Defect_Handler SHALL perform a semantic search in the Embedding_Store for the closest prior defects.
4. WHEN the Defect_Handler has retrieved candidate prior defects, THE Defect_Handler SHALL fetch the recent fixes for the candidate defects through the Data_API.
5. WHEN the Defect_Handler has fetched candidate fixes, THE Defect_Handler SHALL produce a ranked list of candidate matches ordered by relevance with a rationale for each candidate.
6. THE semantic search in the Embedding_Store SHALL be scoped to the Agency identified in the Tenant_Context.

### Requirement 6: Defect Handling — Solution Proposal and Draft Write-Back

**User Story:** As an internal member, I want the system to propose a solution grounded in recent fixes and attach it to the ticket as a draft, so that I can review a concrete recommendation.

#### Acceptance Criteria

1. WHEN the Defect_Handler has produced ranked candidate matches, THE Defect_Handler SHALL generate a proposed solution grounded in the retrieved recent fixes.
2. WHEN the Defect_Handler generates a proposed solution, THE Defect_Handler SHALL include citations to the retrieved fixes that support the proposed solution.
3. IF no retrieved fix supports a proposed solution, THEN THE Defect_Handler SHALL withhold the proposed solution from publication and record the absence of supporting citations.
4. WHEN the Defect_Handler has generated a cited proposed solution, THE Defect_Handler SHALL write the proposed solution to the ticket as a Recommendation_Draft through the Data_API.
5. WHILE the System operates in Recommend_Only_Mode, THE Defect_Handler SHALL mark the Recommendation_Draft as pending human approval rather than applying the solution directly.

### Requirement 7: Human-in-the-Loop Approval

**User Story:** As an internal member, I want to approve, reject, or edit an agent's recommendation before it takes effect, so that a human controls customer-facing outcomes.

#### Acceptance Criteria

1. WHEN an agent run produces a Recommendation_Draft in Recommend_Only_Mode, THE System SHALL pause the run at a human-approval point and persist the run state in the Checkpointer.
2. WHEN the HITL_Controller receives an approve decision for a paused run, THE System SHALL resume the run and finalize the Recommendation_Draft.
3. WHEN the HITL_Controller receives a reject decision for a paused run, THE System SHALL resume the run and regenerate the proposed solution.
4. WHEN the HITL_Controller receives an edit decision for a paused run, THE System SHALL resume the run using the edited content as the finalized recommendation.
5. WHILE a run is paused at a human-approval point, THE System SHALL make no customer-facing change for that run.

### Requirement 8: Customer Pulse Summary

**User Story:** As an agency admin, I want a summary of my agency's open defects, pending enhancements, and open interface developments, so that I understand the current state of my account.

#### Acceptance Criteria

1. WHEN the Customer_Pulse receives a pulse request for an Agency, THE Customer_Pulse SHALL list the Agency's open defects, pending enhancements, and open interface developments through the Data_API.
2. WHEN the Customer_Pulse has retrieved the Agency's open items, THE Customer_Pulse SHALL produce a summary that reports the open defects, pending enhancements, and open interface developments.
3. THE Customer_Pulse SHALL scope the summary to the Agency identified in the Tenant_Context.

### Requirement 9: Knowledge-Base Article Creation

**User Story:** As an internal member, I want the system to draft a knowledge-base article when a ticket is assigned back to a customer, so that reusable knowledge is captured without manual authoring.

#### Acceptance Criteria

1. WHEN a Ticket_Event indicates a ticket has been assigned back to a customer, THE KB_Creator SHALL evaluate whether the ticket information warrants a knowledge-base article.
2. WHEN the KB_Creator evaluates a ticket for a knowledge-base article, THE KB_Creator SHALL search existing knowledge-base articles through the Data_API for an equivalent article.
3. IF an equivalent knowledge-base article already exists, THEN THE KB_Creator SHALL skip creation and record that an equivalent article exists.
4. WHERE the ticket information warrants a knowledge-base article and no equivalent article exists, THE KB_Creator SHALL create a knowledge-base draft through the Data_API.
5. WHEN the KB_Creator creates a knowledge-base draft, THE KB_Creator SHALL include citations to the ticket content that supports the draft.

### Requirement 10: Ticket Status Lookup

**User Story:** As an agency admin, I want to ask for the status of a specific ticket, so that I can get an up-to-date answer on demand.

#### Acceptance Criteria

1. WHEN the Query_API receives a status request for a ticket identifier, THE Status_Responder SHALL retrieve the ticket through the Data_API and return a status summary synchronously.
2. IF the requested ticket identifier does not exist within the Tenant_Context, THEN THE Status_Responder SHALL return a not-found result.
3. THE Status_Responder SHALL scope ticket retrieval to the Agency identified in the Tenant_Context.
4. THE Query_API SHALL return the status response synchronously without routing the request through the event queue.

### Requirement 11: Similarity Linking

**User Story:** As an internal member, I want related and duplicate tickets to be linked automatically, so that connected issues are grouped for faster resolution.

#### Acceptance Criteria

1. WHEN the Similarity_Linker receives a Ticket_Event, THE Similarity_Linker SHALL search the Embedding_Store for tickets semantically similar to the event's ticket.
2. WHEN the Similarity_Linker identifies a candidate similar ticket whose similarity meets the configured threshold, THE Similarity_Linker SHALL link the candidate ticket to the event's ticket through the Data_API.
3. THE Similarity_Linker SHALL scope similarity search to the Agency identified in the Tenant_Context.
4. WHILE the System operates in Recommend_Only_Mode, THE Similarity_Linker SHALL record a proposed link as a Recommendation_Draft rather than applying the link directly.

### Requirement 12: Extensible Capability Architecture

**User Story:** As a developer, I want to add new agentic capabilities without reworking the platform, so that the system can grow over time.

#### Acceptance Criteria

1. WHERE a new specialist sub-graph is registered with the Supervisor, THE Supervisor SHALL route events of the new capability's category to the new specialist sub-graph.
2. THE System SHALL allow a new specialist sub-graph to use the existing Tool_Layer, Checkpointer, and Embedding_Store without modification to those components.
3. WHEN a new specialist sub-graph is added, THE System SHALL process the existing capabilities without change to their behavior.

### Requirement 13: Tenant Isolation and Authorization [V1-REQUIRED]

**User Story:** As an agency admin, I want my data to be strictly isolated from other agencies, so that no other tenant can access my agency's information.

#### Acceptance Criteria

1. WHEN a request or agent run begins, THE System SHALL derive the Tenant_Context, including the Agency identifier, from the authenticated request credentials before any Data_API operation is invoked.
2. IF the authenticated request credentials are missing, expired, or cannot be resolved to exactly one Agency identifier, THEN THE System SHALL deny the request, perform no Data_API read or write operation, and return a response indicating that the request is unauthorized.
3. WHEN the Tool_Layer invokes a Data_API operation, THE Tool_Layer SHALL inject the Agency identifier from the Tenant_Context into the operation.
4. THE System SHALL ignore any Agency identifier supplied in model-generated content and use only the Agency identifier from the Tenant_Context.
5. THE Data_API SHALL enforce Tenant_Context scoping on every read and write operation, such that each operation accesses only records whose Agency identifier equals the Agency identifier in the Tenant_Context.
6. IF an agent run scoped to one Agency attempts to read or act on another Agency's data, THEN THE System SHALL deny the operation, make no change to any stored data, and return a response indicating that the operation is not authorized.
7. WHEN the System denies an operation due to a Tenant_Context scoping violation or an authorization failure, THE System SHALL record the denial in the Audit_Log, including the Agency identifier from the Tenant_Context, the attempted operation, the targeted Agency identifier where available, and the timestamp of the denial.

### Requirement 14: Prompt-Injection and Output Guardrails [V1-REQUIRED]

**User Story:** As a security owner, I want untrusted ticket content to be prevented from manipulating the agent, so that malicious input cannot cause cross-tenant access or unsafe actions.

#### Acceptance Criteria

1. THE System SHALL treat all retrieved ticket content and knowledge-base content as untrusted data and SHALL NOT execute any instruction contained within that content.
2. WHEN an agent invokes a tool, THE System SHALL permit the invocation only if the tool is present on the agent's configured allow-list.
3. IF an agent invokes a tool that is not present on the agent's configured allow-list, THEN THE System SHALL refuse the invocation, leave all tenant data unchanged, return an error indication to the caller that the tool is not permitted, and record the refusal in the Audit_Log.
4. WHEN a tool call includes an Agency identifier, THE System SHALL bind the Agency identifier from the Tenant_Context, ignore any Agency identifier present in retrieved content, and record the binding decision in the Audit_Log.
5. IF retrieved content requests an operation that is not present on the agent's configured allow-list, THEN THE System SHALL refuse the operation, leave all tenant data unchanged, and record the refusal in the Audit_Log.
6. WHEN customer-facing text is produced, THE System SHALL redact all personally identifiable information, defined as names, postal addresses, email addresses, phone numbers, and government or financial account identifiers, from the text before the text is written into any prompt or into the Audit_Log.

### Requirement 15: Idempotency and Dead-Letter Queue [V1-REQUIRED]

**User Story:** As an operator, I want duplicate events to be handled safely and failing events to be captured, so that agents never create duplicate artifacts and failures are recoverable.

#### Acceptance Criteria

1. WHEN a Ticket_Event is delivered more than once with the same event identifier within a deduplication guard window of 24 hours, THE System SHALL process the Ticket_Event to produce at most one resulting artifact.
2. IF a Ticket_Event with a matching event identifier has already been processed to completion, THEN THE System SHALL skip reprocessing, retain the existing artifact unchanged, and record the duplicate delivery with the event identifier and the duplicate detection timestamp.
3. WHEN processing a Ticket_Event fails, THE System SHALL retry the Ticket_Event up to a configured retry limit, defaulting to 5 attempts, using exponential backoff starting at 1 second and doubling each attempt up to a maximum interval of 60 seconds.
4. IF a Ticket_Event fails after reaching the configured retry limit, THEN THE System SHALL route the Ticket_Event to the DLQ and record failure metadata including the event identifier, the failure reason, the number of attempts made, and the timestamp of the final failure.
5. IF the DLQ has reached its configured maximum capacity, defaulting to 10,000 entries, when a Ticket_Event must be routed to the DLQ, THEN THE System SHALL reject the new entry, retain all existing DLQ entries unchanged, and record an error indicating that the DLQ capacity limit has been reached.
6. WHEN an operator replays a Ticket_Event from the DLQ, THE System SHALL reprocess the Ticket_Event through the normal pipeline and apply the deduplication guard so that a Ticket_Event whose event identifier was already processed to completion produces no additional artifact.

### Requirement 16: Immutable Audit Trail [V1-REQUIRED]

**User Story:** As a compliance owner, I want every agent action recorded in an append-only log, so that there is a complete and tamper-evident record of what the system did with customer data.

#### Acceptance Criteria

1. WHEN an agent reads CRM data, THE System SHALL record the read action in the Audit_Log with the actor, the timestamp, the Agency, and the identifier of the ticket read.
2. WHEN an agent produces a Recommendation_Draft, THE System SHALL record the proposal in the Audit_Log with the actor, the timestamp, the Agency, and the identifier of the associated ticket.
3. WHEN a human approve, reject, or edit decision is applied, THE System SHALL record the decision in the Audit_Log with the deciding actor, the timestamp, the Agency, and the identifier of the associated ticket.
4. THE Audit_Log SHALL store records in append-only form and SHALL reject any attempt to modify or delete an existing record.
5. IF a write to the Audit_Log fails, THEN THE System SHALL not treat the audited action as complete and SHALL surface the audit-write failure rather than silently proceeding.
6. WHEN an operator queries the Audit_Log for a ticket identifier that has recorded actions, THE System SHALL return the action history for that ticket ordered chronologically ascending by timestamp.
7. WHEN an operator queries the Audit_Log for a ticket identifier that has no recorded actions, THE System SHALL return an empty result.

### Requirement 17: Authentication on Service Surfaces

**User Story:** As a security owner, I want every service surface to require authentication, so that only authorized callers reach the system.

#### Acceptance Criteria

1. WHEN a request reaches the Data_API, THE Data_API SHALL authenticate the request before processing the request.
2. WHEN a request reaches the Query_API, THE Query_API SHALL authenticate the request before processing the request.
3. WHEN a request reaches the HITL_Controller, THE HITL_Controller SHALL authenticate the request before processing the request.
4. IF a request to a service surface fails authentication, THEN THE service surface SHALL deny the request and return an authentication-failure result.

### Requirement 18: Secrets Management

**User Story:** As a security owner, I want credentials kept out of source and configuration files, so that secrets are protected in production.

#### Acceptance Criteria

1. WHERE the System runs in a non-local environment, THE System SHALL retrieve API keys, database credentials, and language model keys from a managed secret store.
2. THE System SHALL exclude production secrets from environment files beyond local development configuration.

### Requirement 19: Cost and Rate Controls

**User Story:** As an operator, I want runaway loops and uncontrolled spend prevented, so that the system stays within cost and quota limits.

#### Acceptance Criteria

1. THE System SHALL enforce a maximum iteration limit on each agent run.
2. IF an agent run reaches the maximum iteration limit, THEN THE System SHALL stop the run and record the limit stop in the Audit_Log.
3. WHEN an agent run completes, THE System SHALL record the token usage and cost for the run per Agency.
4. IF a language model request is rate-limited by the provider, THEN THE System SHALL retry the request using exponential backoff up to the configured retry limit.
5. WHEN the System generates an embedding or performs a lookup that has a cached result, THE System SHALL return the cached result instead of recomputing it.

### Requirement 20: Observability

**User Story:** As an operator, I want agent runs to be traceable through a pluggable backend, so that I can diagnose behavior in both development and production.

#### Acceptance Criteria

1. WHEN an agent run executes, THE System SHALL emit trace spans for the run through the configured observability backend.
2. WHERE the configured observability backend is a development tracer, THE System SHALL route trace spans to the development tracer.
3. WHERE the configured observability backend is a production exporter, THE System SHALL route trace spans to the production exporter.
4. WHEN the observability backend is selected, THE System SHALL select the backend from environment-driven configuration.

### Requirement 21: Pluggable Embedding Store

**User Story:** As a developer, I want vector storage behind an interface, so that I can run pgvector locally and switch to SQL Server native vectors in Azure without rewriting agents.

#### Acceptance Criteria

1. THE System SHALL access vector storage through the Embedding_Store interface for all upsert and search operations.
2. WHERE the configured vector backend is pgvector, THE Embedding_Store SHALL route upsert and search operations to the pgvector implementation.
3. WHERE the configured vector backend is SQL Server native vectors, THE Embedding_Store SHALL route upsert and search operations to the SQL Server implementation.
4. WHEN an Embedding_Store search is performed, THE Embedding_Store SHALL filter results by the Agency identified in the Tenant_Context.

### Requirement 22: Local Development Environment

**User Story:** As a developer, I want the full stack to run locally in Docker, so that I can develop and test the end-to-end pipeline offline.

#### Acceptance Criteria

1. THE System SHALL provide a docker-compose configuration that runs the agent service, PostgreSQL, the mock Data_API, and a local event source.
2. WHEN the local stack is started and a Ticket_Event is published to the local event source, THE System SHALL process the Ticket_Event through the pipeline to the human-approval point.
3. WHERE the System runs locally, THE Event_Consumer SHALL consume Ticket_Events from the local event source.

### Requirement 23: Azure Deployment Readiness

**User Story:** As a developer, I want the local components to swap to Azure services by configuration, so that moving to Azure does not require rewriting agent code.

#### Acceptance Criteria

1. WHERE the System runs in Azure, THE Event_Consumer SHALL consume Ticket_Events from the configured Azure message service.
2. WHERE the System runs in Azure, THE System SHALL use the configured Azure OpenAI provider through the LLM_Provider interface.
3. WHEN the deployment target changes between local and Azure, THE System SHALL select Event_Consumer, LLM_Provider, Embedding_Store, and observability backends from environment-driven configuration without changes to agent code.
