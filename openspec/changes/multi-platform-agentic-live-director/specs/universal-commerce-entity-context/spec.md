## ADDED Requirements

### Requirement: Universal entity document
Shop, product, campaign, and future commerce entities SHALL use a small common document envelope with revisioned flexible facts, knowledge blocks, aliases/tags, and relations.

#### Scenario: New vertical-specific attribute
- **WHEN** a real-estate product adds `property.bedrooms`
- **THEN** the attribute SHALL be representable without adding a new Python/TypeScript field to the core entity schema.

### Requirement: Common Fact Registry
The system SHALL define canonical semantic keys, aliases, types, and freshness policy for code-relevant common facts while permitting arbitrary custom facts.

#### Scenario: User types “Giá hiện tại”
- **WHEN** the UI/backend recognizes that label
- **THEN** it SHALL map to the canonical current-price semantic key.

#### Scenario: Unknown custom information
- **WHEN** a user adds a field the registry does not know
- **THEN** the information SHALL remain valid as a custom fact rather than being rejected.

### Requirement: Volatile facts remain structured
Exact volatile values such as current price, stock, promotion, and availability SHALL have an authoritative structured representation with freshness/revision metadata.

#### Scenario: Promotion changes
- **WHEN** a promotion revision changes
- **THEN** stale evidence/cache derived from the prior promotion SHALL be invalidated or treated stale
- **AND** long prose SHALL not override the current structured value.

### Requirement: Knowledge blocks hold long irregular context
Long descriptions, shop story, usage guidance, campaign background, and other irregular read-only content SHALL be storable in revisioned knowledge blocks for semantic retrieval.

#### Scenario: Long cosmetics usage guide
- **WHEN** a product has a multi-paragraph usage guide
- **THEN** it MAY remain a tagged knowledge block
- **AND** the runtime SHALL retrieve only query-relevant content rather than serializing the entire entity every turn.

### Requirement: Simple non-technical editing
Workbench SHALL allow normal users to enter common fields, arbitrary label/value rows, and pasted raw knowledge without requiring canonical keys or JSON expertise.

#### Scenario: Unknown user label
- **WHEN** a non-technical operator enters “Dùng cho da dầu”
- **THEN** the system SHALL save the information as mapped/common or custom data
- **AND** the operator SHALL not need to understand internal namespaces.

### Requirement: AI extraction is optional and human-controlled
AI MAY suggest structured facts from pasted knowledge but extraction SHALL not be required to save the source knowledge and SHALL not silently become authoritative.

#### Scenario: Extraction fails
- **WHEN** optional extraction fails
- **THEN** the pasted knowledge SHALL still be saved.

#### Scenario: Suggested fact
- **WHEN** AI suggests a structured fact
- **THEN** explicit user acceptance SHALL be required before it becomes authoritative.

### Requirement: Query-relevant context rendering
The runtime SHALL render only query-relevant entity evidence for Agent/LLM consumption.

#### Scenario: Price question
- **WHEN** a viewer asks only for current price
- **THEN** the evidence bundle SHALL not need to include unrelated long warranty, origin, and campaign prose.

### Requirement: Existing persistence is preferred until disproven
Schema flexibility alone SHALL NOT require introducing a separate operational NoSQL service.

#### Scenario: JSON/document semantics meet requirements
- **WHEN** the existing persistence stack can satisfy revision, indexing, latency, and recovery requirements
- **THEN** the entity repository SHOULD use that stack
- **AND** a new datastore SHALL require separate evidence/design justification.

### Requirement: Breaking migration from rigid shop/product schemas
Affected runtime/API/Workbench/script-authoring consumers SHALL migrate to the universal entity model without a permanent compatibility adapter.

#### Scenario: Migration completes
- **WHEN** all affected consumers use the universal entity model
- **THEN** the rigid legacy product/shop compatibility path SHALL be removed.
