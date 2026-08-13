## MODIFIED Requirements

### Requirement: Display and spoken representations
The system SHALL preserve user-facing `display_text` and exact TTS-facing `spoken_text` representations whenever normalization changes how content will be spoken. A fresh human-approved version SHALL remain the exact immutable source of runtime `spoken_text`. Live runtime MAY derive deterministic sentence-span/cursor metadata from that exact text for scheduling, but SHALL NOT rewrite, paraphrase, mutate, or create a new approved artifact.

#### Scenario: Runtime sentence map
- **GIVEN** a fresh approved product script
- **WHEN** it is bound to a live session
- **THEN** runtime MAY derive sentence offsets/text slices
- **AND** concatenating those slices SHALL reproduce the exact approved `spoken_text`.

### Requirement: Complete approved scripts use the same source-agnostic TextChunker
Approved runtime speech SHALL continue to use the canonical Change A `TextChunker` through the existing verbatim speech service. The new sentence scheduler sits above this path and invokes it per exact approved sentence or other deterministic approved span; it SHALL NOT create a script-specific chunker.

#### Scenario: Approved sentence is spoken
- **WHEN** the runtime schedules one approved sentence
- **THEN** that exact sentence SHALL enter the canonical verbatim TextChunker/TTS/render path
- **AND** TextChunker MAY split it into phrase-sized chunks.

### Requirement: TextChunk finality remains Change A-owned
Sentence-level script cursor semantics SHALL NOT stamp or reinterpret TextChunk finality. Runtime SHALL determine sentence completion at the speech-call/scheduler level while Change A remains responsible for exactly-once TextChunk→AudioWindow→VideoWindow finality.

#### Scenario: Sentence has multiple TextChunks
- **WHEN** one approved sentence produces several TextChunks
- **THEN** intermediate/final TextChunk flags SHALL remain Change A concerns
- **AND** script cursor advancement SHALL not be implemented by treating each TextChunk as a sentence.

## ADDED Requirements

### Requirement: Runtime Q&A does not mutate authoring state
Reactive Agentic Director answers, lead-ins, resume bridges, script cursor checkpoints, and demand state SHALL be runtime-only artifacts and SHALL NOT mutate ScriptSet versions, gate runs, or approval records.

#### Scenario: Viewer Q&A interrupts between sentences
- **WHEN** runtime answers a viewer cluster between approved sentences
- **THEN** the approved script version and approval record SHALL remain unchanged
- **AND** runtime SHALL resume from its stored next-sentence cursor.
