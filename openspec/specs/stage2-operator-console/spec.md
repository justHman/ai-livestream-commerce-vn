# stage2-operator-console Specification

## Purpose
TBD - created by archiving change stage2-console-redesign. Update Purpose after archive.
## Requirements
### Requirement: Consolidated operator layout
The Stage 2 console SHALL present one canonical control block for session status, avatar, LLM, TTS and voice, shop profile, products, video, Auto Demo, diagnostics, and event log without duplicate resource selectors or video panels.

#### Scenario: Console renders initial state
- **WHEN** the page loads on a supported desktop viewport
- **THEN** every canonical block appears once and its disabled or loading state reflects backend readiness

### Requirement: Observable Auto Demo state machine
The console SHALL render explicit Auto Demo states for idle, verifying, attaching, opening, introducing product, answering cluster, generating, synthesizing, prepared, avatar playback, advancing, pivoting, resuming, stopped, and failed.

#### Scenario: Director answers a cluster
- **WHEN** a reactive decision is active
- **THEN** the state machine shows the current product, selected cluster, prompt, generated or cached script when available, playback status, and next planned work

### Requirement: Backend truth drives presentation
The console SHALL derive queue, speech, cluster, revision, pivot, and readiness displays from backend snapshots and events rather than inferring completion from request timing or local counters.

#### Scenario: Avatar playback is still running
- **WHEN** generation and TTS have completed but playback completion has not been confirmed
- **THEN** the console keeps the turn active and does not increment completed speech locally

### Requirement: Complete diagnostics without silent truncation
The console SHALL display complete current prompts, generated scripts, and selected cluster members in expandable or scrollable regions. Summary cards MAY abbreviate content only when the full value remains accessible.

#### Scenario: Long product introduction is generated
- **WHEN** the script exceeds the summary region height
- **THEN** the full script remains inspectable and copyable without data loss

### Requirement: Accessible and responsive controls
Interactive controls SHALL have associated labels, keyboard operation, visible focus, status text not encoded by color alone, and layouts usable at desktop and tablet widths.

#### Scenario: Operator navigates with keyboard
- **WHEN** the operator tabs through session and product controls
- **THEN** focus order follows the visible workflow and every actionable control has a visible focus state

### Requirement: Side-effect-free local fixture bootstrap
The console SHALL prefill local test tokens and render versioned local shop-profile and product fixtures without calling protected mock or resource APIs, creating a session, attaching runtime state, or logging misleading load events during page bootstrap. Tokens and provider credentials MUST NOT be persisted in browser storage.

#### Scenario: Operator opens the local console
- **WHEN** the page finishes loading
- **THEN** local profile and ordered product drafts are immediately selectable and reorderable while no mock-comment, attach, or session request has run

### Requirement: Explicit session prerequisites
Auto Demo SHALL require the operator to Start a session and Attach an accepted profile/catalog revision manually. Auto Demo MUST NOT create or attach a session implicitly.

#### Scenario: Auto Demo is pressed before Attach
- **WHEN** no attached session exists
- **THEN** the console reports the missing prerequisite and does not load comments or start the producer

### Requirement: Continuous configurable Auto Demo producer
The console SHALL support an initial mode that either ingests 20 comments in one batch or sends comments individually from the start, followed by a non-overlapping continuous producer. The producer rate SHALL be configurable from 0.2 through 5 comments per second with default 0.67, SHALL read rate changes before scheduling the next tick, and SHALL repeat the fixture pool with new ingestion identities and timestamps until stopped.

#### Scenario: Batch seed mode starts
- **WHEN** an attached operator starts Auto Demo with batch seed selected
- **THEN** exactly 20 comments are accepted in the initial request and subsequent comments are sent only after the prior request completes at the configured rate

#### Scenario: Fixture pool is exhausted
- **WHEN** the producer reaches the last mock comment while Auto Demo remains active
- **THEN** it restarts from the fixture beginning with new IDs and timestamps without overlapping requests

### Requirement: Rolling feed is distinct from backend windowing
The visible comment feed SHALL retain only the newest 20 accepted comments. The console SHALL label backend comment retention as a time-based selection window and MUST NOT imply that the backend retains exactly 20 comments.

#### Scenario: More than 20 comments are accepted
- **WHEN** the twenty-first visible comment is appended
- **THEN** the oldest visible row is removed while backend diagnostics continue to report their own buffer and active-window counts

### Requirement: Immediate Auto Demo stop
Stopping Auto Demo SHALL stop the producer, interrupt current playback, invalidate pending requests and prepared turns by generation token, and keep the session attached for further manual testing. A late asynchronous result MUST NOT restart the loop.

#### Scenario: Stop occurs during an ingest request
- **WHEN** the operator stops Auto Demo before the request resolves
- **THEN** the response is ignored for scheduling purposes, current playback is interrupted, and no later comment or prepared turn is emitted

### Requirement: Versioned local test preferences
The console SHALL store only local test drafts and preferences in one schema-versioned localStorage record, including shop/product draft, product order, Auto Demo controls, and initial runtime-config values. Runtime truth SHALL remain backend-owned, invalid stored data SHALL fall back to defaults, and the console SHALL provide a reset action.

#### Scenario: Stored schema is incompatible
- **WHEN** the console encounters an unknown or invalid localStorage schema version
- **THEN** it discards that record, restores local fixture defaults, and does not submit stale runtime state

### Requirement: Realtime runtime controls
The console SHALL expose controls for comment rate, initial ingest mode, Q&A window limits, topic cooldown, answer-cache variants, prepared-turn depth, retry count, and demand-pivot gates. An accepted runtime update SHALL apply from the next turn without resetting opening or the current product checkpoint; a rejected update SHALL rollback the control to the last backend-accepted snapshot.

#### Scenario: Prepared depth changes during playback
- **WHEN** the backend accepts a new prepared-turn depth while a turn is speaking
- **THEN** current playback completes, stale prepared turns are invalidated, and subsequent preparation uses the new config revision

