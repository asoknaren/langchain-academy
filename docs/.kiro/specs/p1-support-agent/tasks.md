# Implementation Plan: P1 Support Agent

Work through the tasks in order. Each task has a **Test** that must pass before you tick the box. Requirement numbers refer to [requirements.md](./requirements.md); design sections refer to [design.md](./design.md).

Conventions: `pytest` for tests, `docker compose` for services, all settings via `.env`. Tasks marked *(stretch)* can wait until the MVP is done.

---

## Phase 0: Foundations

- [ ] **0.1 Project skeleton and config**
  - Create the repository layout from the design, `pyproject.toml` (or `requirements.txt`), `.env.example`, and `api/config.py` using pydantic-settings (models, `CONFIDENCE_THRESHOLD`, `MAX_REFUND=100`, `APPROVAL_TIMEOUT_HOURS=24`, `MAX_STEPS`, `TOKEN_BUDGET`).
  - **Test:** `pytest tests/test_config.py` loads defaults, overrides from env, and fails on a missing `OPENAI_API_KEY` when required.

- [ ] **0.2 Docker Compose with Postgres and health endpoint**
  - Compose services `postgres` (pgvector) and `api` with `GET /health`.
  - **Test:** `docker compose up -d` then `curl localhost:8000/health` returns 200; `docker compose down && up` keeps the `pgdata` volume.

- [ ] **0.3 Migrations and application tables**
  - Alembic setup; tables `users`, `tickets`, `approvals`, `triage_queue`, `audit_log`, `kb_articles`, `eval_runs`, `eval_results`; the app role has no UPDATE/DELETE on `audit_log`.
  - **Test:** `alembic upgrade head` on an empty DB succeeds; `alembic downgrade base` succeeds; a test attempting UPDATE on `audit_log` as the app role fails.

- [ ] **0.4 LangGraph Postgres checkpointer and store setup**
  - Initialize `AsyncPostgresSaver` and `AsyncPostgresStore` (with the embedding index config) at API startup.
  - **Test:** integration test writes a checkpoint and a store item, restarts the API container, and reads both back.

- [ ] **0.5 LangSmith tracing with redaction**
  - Enable tracing; implement `redact.py` (email, phone, card-like numbers) and hook it into LangSmith input/output hiding and logging.
  - **Test:** unit tests assert masked output for sample strings; a manual run shows a trace in LangSmith with masked values and `thread_id`/`ticket_id` metadata.

## Phase 1: Test data and baseline (before the agent)

- [ ] **1.1 Synthetic fixtures**
  - Seed script for customers, orders (with amounts, dates, refund status), and ~30 KB articles with embeddings.
  - **Test:** `python -m scripts.seed` is idempotent (running twice yields the same row counts); a vector query for a known article title returns it first.

- [ ] **1.2 Mock billing service**
  - `mock_billing` FastAPI: `GET /orders/{id}`, `GET /customers/{id}/billing`, `POST /refunds` requiring an `Idempotency-Key`.
  - **Test:** posting the same refund key twice returns the same result and records one refund; a missing key returns 400.

- [ ] **1.3 Eval dataset v0 (20 tickets)**
  - `evals/dataset.jsonl` with fields: text, customer, expected intent, expected refund (yes/no, amount), expected citations, tags. Include at least 3 `injection`, 3 `over_limit`, 3 `ambiguous`.
  - **Test:** a schema validation test loads every row with a Pydantic model; tag counts meet the minimums.

- [ ] **1.4 Baseline (keyword routing + single RAG answer)**
  - `evals/baseline.py` and `evals/run_evals.py` skeleton that scores routing accuracy and writes `eval_results`.
  - **Test:** `python -m evals.run_evals --system baseline` completes on the dataset and stores metrics; routing accuracy is reported (any value).

## Phase 2: Thin end-to-end slice (MVP core)

- [ ] **2.1 State schemas and reducers** (Req 2)
  - `state.py` with Input/Output/Overall schemas, `merge_actions` (append + dedupe by `action_id`).
  - **Test:** unit tests: duplicate ids dedupe; two parallel writes keep both; output schema excludes `scratch` and `intent`.

- [ ] **2.2 Pydantic models and policy engine** (Req 4.8)
  - `Intent`, `RefundAction`, `ReplyDraft`; `policy.py` checks amount <= `MAX_REFUND`, order belongs to the customer, no prior refund.
  - **Test:** table-driven tests: $100 passes, $100.01 fails, other customer's order fails, already-refunded fails; `amount <= 0` rejected by the model.

- [ ] **2.3 Router node** (Req 1)
  - Structured output to `Intent`; one retry on validation failure; below-threshold or invalid goes to triage; threshold from config.
  - **Test:** with a fake LLM: high confidence routes to the right branch; low confidence routes to triage with no reply; two invalid outputs route to triage; changing the env threshold changes the outcome without code edits.

- [ ] **2.4 Tool layer** (Req 3.1, 6.2, 6.3)
  - Typed tools: `get_order`, `get_billing_history`, `propose_refund`, `kb_search`. `customer_id` is injected by code, not a model argument.
  - **Test:** tools reject invalid arguments; a tool called for another customer's order returns not found; `kb_search` returns top results with article ids.

- [ ] **2.5 Tech specialist and draft node** (Req 3.2, 3.3)
  - Retrieve KB, draft a `ReplyDraft` with citations; draft fails validation if there are no citations.
  - **Test:** fake LLM: reply cites retrieved ids; a draft with empty citations is rejected and retried, then triaged.

- [ ] **2.6 Graph assembly with Postgres checkpointer** (Req 1.1, 5.1, 5.2)
  - `build.py` compiles the graph (router, tech, triage, draft) with input/output schemas and `thread_id = ticket_id`.
  - **Test:** integration: invoke a technical ticket end to end with a fake LLM; the checkpoint exists; re-invoking the same `thread_id` continues from the latest checkpoint.

- [ ] **2.7 Ticket API** (Req 1.1)
  - `POST /tickets`, `GET /tickets/{id}`; `customer_id` comes from the session dependency only.
  - **Test:** API test with a test session: creating a ticket returns a reply or status; a request body containing `customer_id` is ignored; another customer's ticket returns 404.

- [ ] **2.8 First eval run of the agent**
  - Extend `run_evals.py` to run the agent; report routing accuracy next to the baseline.
  - **Test:** `python -m evals.run_evals --system agent` runs on all 20 tickets and stores results; a side-by-side report prints for agent and baseline.

## Phase 3: Billing, approval, durability (MVP)

- [ ] **3.1 Billing specialist and refund proposal** (Req 3.1, 3.4)
  - Tool loop with only the billing tools; backoff retry on tool errors (max 3), then triage with reason.
  - **Test:** with a mocked failing tool: three attempts, then a triage record with the failure reason; with a working tool: a `RefundAction` is proposed.

- [ ] **3.2 Policy check node and step/token limits** (Req 3.5, 4.8, NFR-1)
  - Policy node routes over-limit to triage; conditional edge enforces `MAX_STEPS` and `TOKEN_BUDGET`; set `recursion_limit`.
  - **Test:** an over-$100 refund never reaches the approval gate; a forced loop stops at `MAX_STEPS` and ends in triage.

- [ ] **3.3 Approval gate and refund execution** (Req 4.1-4.3, 4.7)
  - Compile with `interrupt_before=["execute_refund"]`; on pause, insert an `approvals` row. Execution calls mock billing with `Idempotency-Key = action_id`.
  - **Test:** a refund ticket pauses; no refund exists in mock billing; resuming executes exactly one refund; resuming twice still leaves one refund.

- [ ] **3.4 Decision endpoint: approve, edit, reject** (Req 4.3-4.5, 4.9)
  - `POST /approvals/{id}/decision` (reviewer role only); edit uses `update_state` then re-runs the policy check; reject drafts a decline reply; every decision writes to `audit_log` with identity, timestamp, original and final values.
  - **Test:** approve executes the original amount; edit to $50 executes $50 and an edit to $150 is refused by policy; reject executes nothing and the reply mentions the decline; a customer-role call returns 403; audit rows exist for each case.

- [ ] **3.5 Approval timeout worker** (Req 4.6)
  - `worker/main.py` loop: pending approvals older than `APPROVAL_TIMEOUT_HOURS` become `escalated`; never approved.
  - **Test:** with a time-travel/fake clock or a 1-second timeout setting: the approval escalates, no refund is executed, an audit row exists; the worker restarted mid-loop does not double-escalate.

- [ ] **3.6 Restart durability** (Req 5.3, 5.4)
  - `GET /threads/{id}/history` (reviewer role).
  - **Test:** integration: pause at approval, `docker compose restart api`, approve, and exactly one refund results; history endpoint lists the checkpoints in order.

- [ ] **3.7 Guardrails and injection tests** (Req 6)
  - Delimited untrusted-data blocks in prompts; tool binding per specialist; redaction in logs.
  - **Test:** the `injection` tickets in the dataset produce zero tool calls outside the specialist's tools and zero refunds without approval; logs contain no raw emails or card numbers.

- [ ] **3.8 Concurrent message handling** (Req 12.3, 12.4)
  - Postgres advisory lock per `thread_id`; second message queued (`enqueue`); no cancellation after a refund has executed.
  - **Test:** two simultaneous messages on one thread run one after the other and both get replies; sending a message during an executed-refund run does not cancel it.

## Phase 4: UI, auth, and Compose completion (MVP)

- [ ] **4.1 Seeded users and password login** (Req 12.7)
  - `users` seed with hashed passwords and roles (`customer`, `reviewer`, `senior`); `POST /login` returns a server-side session; `auth.py` dependency supplies identity and role.
  - **Test:** wrong password returns 401; valid login returns a session; a reviewer-only endpoint returns 403 for a customer session; passwords are stored hashed.

- [ ] **4.2 Streamlit customer page** (Req 12.10)
  - Login, submit ticket, view conversation by `ticket_id`; history fetched from the API, not stored in `st.session_state`.
  - **Test:** manual script: log in as a customer, submit a technical ticket, receive a cited reply, refresh the browser, and still see the history. Add a Streamlit `AppTest` smoke test for the login and submit flow.

- [ ] **4.3 Streamlit reviewer page** (Req 4, 12.10)
  - List pending approvals, show proposed refund, approve/edit/reject.
  - **Test:** manual script: submit a refund ticket as a customer, approve it as a reviewer, and see the customer's reply update; `AppTest` smoke test for the approve flow.

- [ ] **4.4 SSE progress streaming** (Req 12.5)
  - `POST /tickets/{id}/messages` streams node progress and the reply; UI renders progress.
  - **Test:** a client test reads at least one progress event before the final reply event; the UI shows progress in the manual script.

- [ ] **4.5 Full Compose stack** (Req 12.6)
  - Add `worker`, `ui`, `mock-billing` to Compose; one-command startup.
  - **Test:** from a clean checkout, `docker compose up --build` brings all five services healthy; the end-to-end refund scenario from 4.3 passes against it.

## Phase 5: Evaluation gate (MVP exit)

- [ ] **5.1 Full metrics** (Req 11.2, 11.5)
  - Add resolution correctness (LLM judge), unapproved-action count, cost per ticket, escalation rate; scripted reviewer auto-answers the gate.
  - **Test:** eval run outputs all four metrics for agent and baseline; a deliberately broken build that executes a refund without approval makes the unapproved-action count non-zero (verifies the metric works).

- [ ] **5.2 Regression thresholds** (Req 11.7)
  - `evals/thresholds.json` and `--check` mode.
  - **Test:** `run_evals --check` exits 0 on the current build; lowering a metric artificially (or raising a threshold) makes it exit non-zero.

- [ ] **5.3 Grow the dataset to 100+ tickets and review**
  - LLM-generated additions, hand-reviewed; keep tag coverage.
  - **Test:** schema validation passes; tag minimums hold; agent beats the baseline on routing accuracy and has zero unapproved actions.

- [ ] **5.4 CI workflow**
  - Run unit and integration tests and `run_evals --check` (with a stored small dataset) on push.
  - **Test:** a pull request with a failing test or threshold miss fails the workflow.

**MVP exit criteria:** all of Phases 0-5 ticked; unapproved-action count is 0; the agent beats the baseline on routing accuracy; the restart-mid-approval test passes.

---

## Phase 6: Stretch

- [ ] **6.1 Summarizer and tool-output filtering** (Req 7) *(stretch)*
  - `summarize` node with `RemoveMessage`; truncate bulky tool output; keep the last N messages.
  - **Test:** a long conversation fixture ends with a summary, only the last N messages, and removed ids absent from state; token usage drops versus without summarizing.

- [ ] **6.2 Profile memory with Trustcall** (Req 8) *(stretch)*
  - `CustomerProfile` schema, namespace `(customer_id, "profile")`, patch updates, contradiction history, memory writes only from customer/reviewer messages.
  - **Test:** a second conversation adds a field without losing the first; a contradicting fact replaces the value and keeps history; text injected via a tool output is never written; customer A cannot read customer B's namespace.

- [ ] **6.3 Memory view/edit/delete endpoints** (Req 8.7) *(stretch)*
  - `GET/PUT/DELETE /me/memory`.
  - **Test:** deleted memory no longer appears in the next run's prompt; edit persists; another customer's memory is unreachable.

- [ ] **6.4 Issue history collection memory** (Req 9) *(stretch)*
  - `IssueRecord` docs with embeddings, dedupe, retrieval in drafting, failed-fix exclusion, retention cleanup in the worker.
  - **Test:** resolving the same issue twice creates one record; a recorded failed fix is not proposed again; records past retention are deleted by the worker.

- [ ] **6.5 Instructions memory** (Req 10) *(stretch)*
  - Propose instruction updates from reviewer edits; apply only after lead confirmation; load into the draft prompt.
  - **Test:** an unconfirmed proposal does not change replies; after confirmation, a draft follows the new rule; the eval set shows no regression.

- [ ] **6.4b Memory-quality eval** *(stretch)*
  - Eval cases for stale facts, contradictions, and duplicates.
  - **Test:** metrics for profile accuracy and duplicate rate are reported and gated in `thresholds.json`.

---

## Progress tracker

| Phase | Tasks | Done |
|---|---|---|
| 0 Foundations | 5 | 0 |
| 1 Data and baseline | 4 | 0 |
| 2 Thin slice | 8 | 0 |
| 3 Billing, approval, durability | 8 | 0 |
| 4 UI and Compose | 5 | 0 |
| 5 Evaluation gate | 4 | 0 |
| 6 Stretch | 6 | 0 |
