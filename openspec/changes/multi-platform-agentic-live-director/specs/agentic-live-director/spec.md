## ADDED Requirements

### Requirement: Agent operates after deterministic traffic reduction
The live Agentic Director SHALL consume selected compact cluster envelopes rather than the raw rolling viewer transcript.

#### Scenario: High-traffic live
- **WHEN** hundreds of comments exist in the active horizon
- **THEN** deterministic safety, embedding, clustering, and ranking SHALL reduce them before Agent invocation
- **AND** the Agent SHALL receive only selected bounded demand context.

### Requirement: Structured bounded memory
The Agent runtime SHALL maintain explicit bounded ScriptState, SessionMemory, TopicMemory, and EvidenceCache instead of relying on an ever-growing LLM chat transcript.

#### Scenario: Long session
- **WHEN** a livestream has run for hours
- **THEN** the next Agent request SHALL include only bounded relevant memory/evidence
- **AND** it SHALL not automatically replay the full transcript.

### Requirement: TopicMemory resolves bounded follow-ups
The runtime SHALL retain enough keyed recent topic/entity context to resolve short referential follow-ups.

#### Scenario: Pronoun follow-up
- **GIVEN** the prior answered topic concerned product P020
- **WHEN** a viewer cluster asks “vậy cái đó có sạc nhanh không?”
- **THEN** bounded topic/entity memory MAY resolve “cái đó” to P020
- **AND** the runtime SHALL not require the entire livestream transcript.

### Requirement: Evidence retrieval is cache-aware and batch-native
The Agent runtime SHALL use an application-owned Evidence Planner and EvidenceCache that batch only missing authoritative evidence.

#### Scenario: Partial cache hit
- **GIVEN** P001 price is cached and P020 price is missing
- **WHEN** a comparison requires both prices
- **THEN** the planner SHALL reuse the fresh P001 value
- **AND** fetch only the missing P020 evidence in the batch.

### Requirement: Volatile evidence is revalidated near speech
Price, stock, promotion, and availability evidence SHALL use short freshness or explicit invalidation semantics.

#### Scenario: Stock changed while script sentence played
- **WHEN** a pending Q&A reaches the safe speech boundary
- **THEN** stale stock evidence SHALL be refreshed/revalidated before the answer is spoken
- **OR** the answer SHALL state that fresh authoritative stock evidence is unavailable.

### Requirement: Deterministic factual fast path
Unambiguous factual questions SHALL be eligible to bypass planning-agent generation when deterministic eligibility conditions hold.

#### Scenario: Known price fact
- **WHEN** a selected cluster has high-confidence product and price intent
- **AND** fresh authoritative price evidence is available
- **THEN** the runtime MAY produce a deterministic answer with zero LLM calls
- **OR** use one bounded verbalization generation for natural speech.

### Requirement: Bounded complex-agent path
Complex Q&A SHALL execute under explicit code-owned semantic/tool budgets.

#### Scenario: Normal comparison
- **WHEN** a comparison requires reasoning over two products
- **THEN** the normal path SHALL use at most one planning generation, one batch evidence round, and one final answer generation.

#### Scenario: Model requests repeated evidence rounds
- **WHEN** model output attempts to request work beyond the configured evidence-round ceiling
- **THEN** the runtime SHALL terminate/fallback with a typed budget result
- **AND** SHALL NOT continue an autonomous loop.

### Requirement: Tools are allowlisted evidence operations
Model-requested operations SHALL be validated against typed application-owned evidence schemas.

#### Scenario: Model asks to access filesystem or web
- **WHEN** the model proposes an arbitrary filesystem, shell, open-web, or job-management operation
- **THEN** the executor SHALL reject it
- **AND** no such general tool SHALL be exposed by the live Agent runtime.

### Requirement: Authoritative evidence wins over model claims
Agent output SHALL not create or mutate authoritative shop/product facts.

#### Scenario: Evidence unavailable
- **WHEN** exact authoritative price evidence cannot be resolved
- **THEN** the Agent SHALL NOT invent a price
- **AND** it SHALL produce an appropriate unavailable/clarifying response within policy.

### Requirement: Backend owns orchestration authority
The model SHALL NOT own retries, candidate selection, pivot policy, script cursor mutation, or job creation.

#### Scenario: Model suggests interrupting the current script sentence
- **WHEN** model output asks to interrupt immediately
- **THEN** the Speech Arbiter SHALL ignore that scheduling request
- **AND** deterministic runtime policy SHALL decide the next safe boundary.

### Requirement: Agent execution is observable
Each Q&A SHALL expose content-safe metadata for execution path, evidence cache hits/misses, evidence rounds, LLM call counts, token/latency metrics, and terminal state.

#### Scenario: Operator inspects one answer
- **WHEN** Workbench opens the Q&A trace
- **THEN** it SHALL show whether factual-fast or complex-agent path ran
- **AND** the evidence/tool/LLM round counts used.
