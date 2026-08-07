## ADDED Requirements

### Requirement: Three-layer sandbox verification
The system SHALL verify sandbox readiness in three ordered layers: provider credentials/API access, LiveAvatar session plus LiveKit connectivity, and end-to-end LLM-to-TTS-to-avatar speech.

#### Scenario: All verification layers pass
- **WHEN** the operator runs verification with valid selected resources
- **THEN** each layer reports pass with bounded latency metadata and the final result is ready

### Requirement: Fail-fast layer isolation
A failed layer SHALL prevent dependent later layers from running while preserving successful earlier results and returning a sanitized actionable error.

#### Scenario: LiveKit connection fails
- **WHEN** credentials pass but the LiveAvatar session cannot establish LiveKit connectivity
- **THEN** layer one remains pass, layer two reports fail, and end-to-end speech is not attempted

### Requirement: Cleanup after verification
Verification-created sessions and temporary resources SHALL be stopped and released on success, failure, cancellation, or timeout.

#### Scenario: Speech verification times out
- **WHEN** the end-to-end layer exceeds its timeout
- **THEN** the API reports timeout and tears down the temporary avatar session

### Requirement: Verification does not leak secrets
Verification output and logs MUST NOT include credentials, access tokens, LiveKit secrets, internal stack traces, or raw provider payloads containing sensitive fields.

#### Scenario: Provider rejects a credential
- **WHEN** the provider returns an authentication error
- **THEN** the operator receives a sanitized credential failure without the submitted secret

### Requirement: Sandbox is bounded smoke-only
Sandbox verification SHALL be limited to credential probes, connectivity checks, and 1–3 playback-confirmed turns. The sandbox SHALL NOT be used as evidence of full lifecycle correctness. The sandbox session timeout SHALL be respected; verification SHALL NOT run outside the sandbox or against paid LiveAvatar resources.

#### Scenario: Sandbox session is nearing timeout
- **WHEN** the sandbox session approaches its 1-minute limit
- **THEN** verification SHALL report the bounded scope and SHALL NOT claim to have validated a full product lifecycle

### Requirement: Local-real full loop is separate from sandbox
The benchmark harness SHALL support a local-real lane that uses the same fixture, config, FSM, and pipeline as Auto Demo but replaces the avatar with a mock renderer. This lane SHALL run offline, produce gitignored JSON reports under `.runtime/benchmarks/stage2/`, and verify correct lifecycle coverage (3 opening turns, one full product lifecycle, at least 2 Q&A windows, cross-product excursion, demand pivot + resume) or a 10-minute hard timeout.

#### Scenario: Local-real benchmark completes coverage set
- **WHEN** the local-real benchmark reaches all required lifecycle coverage points
- **THEN** it stops early and produces a PASS report with per-turn latency spans, queue pressure, retries, stale work, and cleanup metrics

### Requirement: Benchmark regression gates
The benchmark report SHALL compare p95 per-stage latency against the same profile baseline. A regression greater than 20% in any stage SHALL mark the run as FAIL. Safety caps SHALL include maximum drop rate, maximum retries per session, and maximum playback timeout errors.

#### Scenario: LLM latency regresses by 30%
- **WHEN** the current run's p95 LLM TTFT is 30% higher than baseline
- **THEN** the benchmark report shows FAIL for LLM latency and recommends investigation before deployment
