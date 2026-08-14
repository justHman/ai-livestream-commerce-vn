# agent-context-compression-benchmark Specification

## Purpose

Optional benchmark-only hybrid text+image context compression for vision-capable models: an all-text baseline fixture set using the actual target vision-capable model, a hybrid mode that keeps instruction/control/dynamic exact facts as text and renders only eligible read-only descriptive context as images, measurement of effective/model-reported input tokens, TTFT, total latency, and cost, correctness measurement (exact number/identifier accuracy, Vietnamese diacritics, grounding, tool selection, hallucination), non-regression thresholds with a minimum material token/latency benefit, hybrid mode disabled unless thresholds pass, and image context never carrying tool schemas, response schemas, instruction hierarchy, or authoritative volatile facts.

## Requirements

### Requirement: Text remains the control plane
System/developer instructions, exact identifiers, dynamic authoritative facts, tool schemas, response schemas, and the current execution task SHALL remain textual context.

#### Scenario: Hybrid image mode
- **WHEN** hybrid context compression is enabled for a benchmark
- **THEN** tool schemas and current exact price/stock values SHALL remain text
- **AND** they SHALL not be encoded only inside an image.

### Requirement: Only eligible read-only descriptive context may become image context
Long static descriptions, shop story/persona background, campaign background, and similar read-only descriptive context SHALL be the only eligible candidates rendered as high-quality images for vision-capable models.

#### Scenario: Product long description
- **WHEN** the benchmark selects a long stable description as image context
- **THEN** its authoritative exact volatile fields SHALL remain independently available as structured/text evidence.

### Requirement: All-text baseline is mandatory
Every hybrid benchmark SHALL compare against an identical all-text fixture using the same target model and task set.

#### Scenario: Benchmark run
- **WHEN** hybrid mode is evaluated
- **THEN** an all-text baseline SHALL be recorded for the same Q&A cases.

### Requirement: Benchmark measures correctness and efficiency
The benchmark SHALL measure effective/model-reported input tokens, TTFT, total latency, exact numeric/identifier accuracy, Vietnamese-diacritic accuracy, grounding accuracy, tool-selection accuracy, hallucination rate, and cost where measurable.

#### Scenario: Token savings but accuracy loss
- **WHEN** hybrid mode reduces input tokens but materially worsens exact fact accuracy
- **THEN** hybrid mode SHALL fail acceptance.

### Requirement: Hybrid mode is gated and optional
Hybrid context compression SHALL remain disabled by default until configured non-regression and minimum material-benefit thresholds pass.

#### Scenario: No material benefit
- **WHEN** correctness is equal but token/latency savings are below the required material threshold
- **THEN** the production default SHALL remain all-text.
