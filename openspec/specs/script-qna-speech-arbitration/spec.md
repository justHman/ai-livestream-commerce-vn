# script-qna-speech-arbitration Specification

## Purpose

Sentence-level approved-script runtime + reactive Q&A speech arbitration above the canonical Change A TextChunker: immutable approved `spoken_text` with a deterministic derived sentence map, a script cursor that advances only on normal sentence completion, a Speech Arbiter where an active approved sentence is non-preemptible for normal Q&A (Q&A may take the next boundary), pending-Q&A revalidation at the safe boundary, exact next-sentence resume after Q&A, natural deterministic Vietnamese lead-in/resume transitions without a default bridge-only LLM call, stable-evidence prefetch with just-in-time volatile revalidation, and a distinct operator hard-interrupt control plane.

## Requirements

### Requirement: Approved spoken text remains immutable
Runtime Q&A integration SHALL consume exact human-approved `spoken_text` without post-approval rewriting.

#### Scenario: Script is bound
- **WHEN** an approved script version is bound to a session
- **THEN** runtime sentence mapping SHALL derive from that exact text
- **AND** no LLM rewrite SHALL create a replacement script artifact.

### Requirement: Runtime derives a deterministic sentence map
The live runtime SHALL derive sentence spans/cursor metadata from exact approved `spoken_text` without changing content.

#### Scenario: Sentence map round trip
- **WHEN** all sentence spans are concatenated according to their stored boundaries
- **THEN** they SHALL reproduce the exact approved spoken artifact.

### Requirement: Sentence scheduling is above TextChunker
Approved-script sentence boundaries SHALL be a live scheduling concern above the canonical Change A phrase chunker.

#### Scenario: One sentence yields multiple TextChunks
- **WHEN** an approved sentence is longer than one TextChunk phrase
- **THEN** the sentence SHALL remain one non-preemptible script scheduling unit
- **AND** Change A MAY emit multiple TextChunks internally.

### Requirement: Normal Q&A never interrupts an active script sentence
Reactive viewer Q&A SHALL have priority over the next script sentence but SHALL NOT cancel a normal approved sentence already in progress.

#### Scenario: Q&A wins mid-sentence
- **GIVEN** P010 sentence 7 is playing
- **WHEN** a high-priority P020 Q&A cluster becomes the pending winner
- **THEN** P010 sentence 7 SHALL complete normally
- **AND** the arbiter MAY schedule P020 Q&A before P010 sentence 8.

### Requirement: Pending Q&A is revalidated at the safe boundary
The runtime SHALL re-rank/revalidate pending Q&A when the active sentence completes before spending final expensive generation or speech.

#### Scenario: Temporary winner cools before boundary
- **WHEN** a pending cluster loses eligibility before the sentence ends
- **THEN** it SHALL not preempt the next script sentence solely because it was previously pending.

### Requirement: Script cursor resumes at the exact next sentence
The runtime SHALL checkpoint the exact next approved sentence before Q&A and resume from that sentence after Q&A.

#### Scenario: Cross-product Q&A
- **GIVEN** P010 sentence 7 completed and sentence 8 is next
- **WHEN** P020 Q&A is spoken
- **THEN** the subsequent approved-script speech SHALL start at exact P010 sentence 8
- **AND** sentence 7 SHALL not be repeated
- **AND** sentence 8 SHALL not be skipped.

### Requirement: Cursor advances only on normal sentence completion
Runtime SHALL not advance the approved sentence cursor merely because lower-level chunks/windows were emitted.

#### Scenario: Sentence playback fails
- **WHEN** sentence speech fails or is hard-cancelled before normal sentence-level completion
- **THEN** cursor advancement SHALL follow explicit sentence completion semantics
- **AND** lower-level TextChunk finality SHALL not be treated as proof that the approved sentence completed.

### Requirement: Natural Q&A lead-in
Reactive Q&A SHALL enter naturally with a concise representative paraphrase rather than reading raw cluster members.

#### Scenario: Many viewers ask about P020 fast charging
- **WHEN** that cluster wins
- **THEN** the spoken turn MAY begin with a natural phrase equivalent to "Em thấy nhiều anh chị đang hỏi P020 có hỗ trợ sạc nhanh không…"
- **AND** it SHALL not enumerate every raw viewer message.

### Requirement: Natural script resume bridge
After Q&A, runtime SHALL emit a concise natural bridge back to the current script product when a bridge improves continuity, and a separate bridge-only LLM call SHALL not be required by default.

#### Scenario: Return from P020 to P010
- **WHEN** P020 Q&A finishes and P010 sentence 8 is next
- **THEN** runtime MAY speak a deterministic bridge equivalent to "Rồi, em tiếp tục với P010 nhé…"
- **AND** a separate bridge-only LLM call SHALL not be required by default.

### Requirement: Stable evidence may prefetch; volatile evidence is just-in-time
While a script sentence is playing, runtime MAY prefetch stable evidence for a high-confidence pending cluster but SHALL revalidate volatile exact facts near the boundary.

#### Scenario: Pending price question
- **WHEN** price may change between candidate detection and speech boundary
- **THEN** final answer preparation SHALL use fresh/revalidated authoritative price evidence.

### Requirement: Hard interrupt remains separate
Operator/emergency hard interrupt SHALL remain a separate control-plane operation that MAY cancel active speech according to the existing control-plane contract and is not equivalent to normal viewer-Q&A scheduling.

#### Scenario: Operator interrupt
- **WHEN** an authorized hard interrupt is received
- **THEN** the runtime MAY cancel the active sentence immediately
- **AND** this SHALL not weaken the normal Q&A no-mid-sentence rule.
