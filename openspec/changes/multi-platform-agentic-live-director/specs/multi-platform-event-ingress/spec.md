## ADDED Requirements

### Requirement: One canonical multi-platform event endpoint
The AI backend SHALL expose one canonical `POST /api/v1/sessions/{session_id}/events` viewer/platform ingestion contract that accepts one or many normalized events in the same request.

#### Scenario: TikTok and Shopee comments arrive together
- **WHEN** the SE Platform Adapter Router submits a batch containing TikTok and Shopee viewer comments
- **THEN** both events SHALL be processed through the same canonical ingestion service
- **AND** core Director/reducer logic SHALL NOT branch on platform-specific protocol behavior.

#### Scenario: Single event
- **WHEN** the SE layer submits one normalized event
- **THEN** it SHALL use the same endpoint/schema as a multi-event batch
- **AND** no separate single-ingest mode SHALL be required.

### Requirement: Stable idempotent event identity
Every canonical event SHALL carry a stable `event_id` used as an idempotency key for the live session.

#### Scenario: SE retries after timeout
- **GIVEN** an event was already accepted
- **WHEN** the identical `event_id` is retried
- **THEN** the backend SHALL return an idempotent duplicate result
- **AND** persistence, embedding, cluster membership, unique-viewer demand, message demand, and pivot demand SHALL NOT be incremented a second time.

### Requirement: Platform provenance without platform business logic
Canonical events SHALL retain platform and source-stream provenance for tracing/analytics while semantic reduction remains platform-neutral.

#### Scenario: Same question from two platforms
- **WHEN** equivalent comments arrive from Facebook and YouTube
- **THEN** platform provenance SHALL remain observable
- **AND** clustering MAY merge them based on semantic/topic/product evidence rather than platform identity.

### Requirement: Normalized viewer identity
Viewer-originated events SHALL carry a normalized stable viewer identity when the source provides one so demand can distinguish unique viewers from repeated messages.

#### Scenario: One viewer repeats twenty times
- **WHEN** one normalized viewer emits twenty equivalent comments
- **THEN** message demand MAY record twenty accepted messages
- **AND** unique-viewer demand SHALL remain one for those messages.

### Requirement: Safety Gate runs before embedding
Comment-like events SHALL pass deterministic safety evaluation before embedding.

#### Scenario: Prompt-injection viewer comment
- **WHEN** a viewer comment attempts to instruct the downstream model to ignore system policy or call arbitrary tools
- **THEN** the Safety Gate SHALL reject or mark it non-actionable according to versioned policy before embedding
- **AND** it SHALL NOT enter semantic clusters or Agent context.

#### Scenario: Spam or curated abusive content
- **WHEN** content matches a configured deterministic rejection rule
- **THEN** it SHALL NOT be embedded
- **AND** sanitized rejection counters SHALL remain observable.

### Requirement: Removed viewer-ingress contracts are not mounted
The new event contract SHALL replace the former platform WebSocket, batch ingest, and chat endpoints without compatibility aliases.

#### Scenario: Client calls old route
- **WHEN** a client calls `/api/v1/sessions/{session_id}/ingest`, `/api/v1/sessions/{session_id}/chat`, or `/api/v1/ws/platform/{session_id}`
- **THEN** that old viewer-ingress contract SHALL not be mounted.

### Requirement: Workbench simulates the SE boundary
Workbench SHALL provide a multi-platform adapter simulator that produces canonical event requests rather than bypassing the integration boundary.

#### Scenario: Retry simulation
- **WHEN** the operator configures a retry in Workbench
- **THEN** the simulator SHALL resend the same `event_id`
- **AND** the UI SHALL show both the simulated upstream event and the exact canonical request delivered to the backend.
