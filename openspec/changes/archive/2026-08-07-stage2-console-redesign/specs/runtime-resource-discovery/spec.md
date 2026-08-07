## ADDED Requirements

### Requirement: Runtime resource discovery
The authenticated API SHALL return available and active LLM, TTS, voice, and avatar resources with stable IDs, labels, provider or engine, model identifier, readiness, capabilities, and relevant metadata.

#### Scenario: Console loads resource controls
- **WHEN** an authorized operator opens the Stage 2 console
- **THEN** each selector is populated from backend discovery data and marks the currently active resource

### Requirement: Runtime selection validation
Resource-selection endpoints SHALL reject unknown or incompatible IDs before mutating active runtime state and SHALL return a consistent typed error response.

#### Scenario: Unknown voice is selected
- **WHEN** the operator submits a voice ID not offered by the selected TTS engine
- **THEN** the API returns a validation error and retains the previous active configuration

### Requirement: TTS preview
The authenticated API SHALL synthesize bounded preview text using the selected TTS model and voice without creating an avatar session, and SHALL return browser-playable audio plus metadata.

#### Scenario: Operator previews a Vietnamese voice
- **WHEN** valid preview text, TTS resource, and voice are submitted
- **THEN** the response contains playable audio with sample rate, content type, model ID, and voice ID

### Requirement: Secrets remain server-side
Discovery and preview responses MUST NOT expose API keys, provider secrets, internal credentials, or unrestricted local filesystem paths.

#### Scenario: Cloud resource is discovered
- **WHEN** a cloud-backed avatar or engine is listed
- **THEN** the response contains only frontend-safe identifiers and metadata

### Requirement: Scheduling configuration discovery
The runtime discovery endpoint SHALL expose per-session scheduling configuration: valid ranges for comment rate, Q&A window limits, cooldown, answer-cache variants, prepared-turn depth, retry count, pivot thresholds, and current accepted revision snapshots.

#### Scenario: Operator inspects available runtime config
- **WHEN** an authorized operator queries runtime configuration
- **THEN** the response includes accepted ranges for every control and the current `config_revision`, `profile_revision`, and `catalog_revision`

### Requirement: Revision snapshot in diagnostics
Accepted configuration snapshots SHALL be stored per-session and exposed through diagnostics. Each snapshot SHALL include the accepted profile, catalog ordering, runtime parameters, and corresponding revision tokens at the moment of acceptance.

#### Scenario: Operator reviews what was accepted
- **WHEN** the operator queries diagnostics after a live Re-attach
- **THEN** the accepted snapshot shows the profile, product order, and runtime parameter values that were validated and stored by the backend at attach time
