## ADDED Requirements

### Requirement: Editable shop profile
The console SHALL expose structured editable shop-profile fields and SHALL serialize them into the session persona payload only after validation.

#### Scenario: Operator updates shop identity
- **WHEN** the operator edits valid shop name, host name, address, phone, and selling style fields
- **THEN** the attached session uses the validated profile composed with the immutable base sales persona

### Requirement: Selectable and reorderable products
The console SHALL allow products to be selected or omitted and reordered before attachment. The backend SHALL preserve the submitted order as the Director's initial product order.

#### Scenario: P004 is moved to first position
- **WHEN** the operator submits selected products with P004 first
- **THEN** the Director's first product introduction targets P004

### Requirement: Dual form and JSON editing
The console SHALL provide product form editing and an advanced JSON representation backed by one canonical in-memory model. Switching views SHALL preserve valid edits and reveal validation failures without silently discarding data.

#### Scenario: Invalid product JSON is entered
- **WHEN** the JSON contains a duplicate product ID or invalid price
- **THEN** attachment is blocked and the affected field or product is identified

### Requirement: Boundary validation
The backend SHALL validate product IDs, lengths, price relationships, stock values, arrays, image references, and maximum catalog size independently of frontend validation.

#### Scenario: Original price is below sale price
- **WHEN** the submitted original price is lower than the sale price
- **THEN** the backend rejects the session configuration with a typed validation error

### Requirement: Versioned local test drafts
The console SHALL persist one schema-versioned localStorage record containing shop/product draft, product order, and test preferences. An incompatible schema SHALL fall back to fixture defaults. Runtime configuration truth SHALL remain backend-owned.

#### Scenario: Incompatible stored schema
- **WHEN** the console loads a localStorage record with an unknown schema version
- **THEN** the record is discarded, fixture defaults are restored, and no stale runtime state is submitted

### Requirement: Atomic live Re-attach
Attaching while a session is live SHALL atomically update profile and catalog revisions. The current speaking turn SHALL complete; a removed product SHALL advance to the next product per the new order; a new product SHALL append to the lifecycle end; a profile update SHALL apply from the next turn. Opening and product checkpoint SHALL survive re-attach. Stale prepared turns SHALL be invalidated by revision token.

#### Scenario: Product is removed during live session
- **WHEN** the operator removes a product being currently introduced and re-attaches
- **THEN** the current introduction completes, the removed product is skipped, and the next product in the new order begins

#### Scenario: Profile changes during playback
- **WHEN** the operator updates the shop profile and re-attaches while a turn is speaking
- **THEN** the speaking turn finishes with the old profile, the next turn uses the revised profile, and prepared turns with the old profile revision are invalidated

### Requirement: Realtime runtime configuration controls
The console SHALL expose validated controls for comment rate (0.2–5/s), initial ingest mode, max clusters per Q&A window, window hard timeout, topic cooldown, answer-cache variants, prepared-turn depth, retry count, and demand-pivot thresholds. An accepted update SHALL apply from the next turn without resetting opening or the current product checkpoint.

#### Scenario: Prepared depth changes during playback
- **WHEN** the operator changes prepared-turn depth while a turn is speaking and the backend accepts the revision
- **THEN** current playback completes, stale prepared turns beyond the new depth are invalidated via generation token, and subsequent preparation uses the new config revision

### Requirement: Config revision token
Runtime configuration changes SHALL increment a `config_revision` token. Prepared turns whose `generation_token` predates the current `config_revision` SHALL be considered stale and dropped when their preparation completes.

#### Scenario: Rate changes before next tick
- **WHEN** the operator reduces Auto Demo rate from 2/s to 0.5/s
- **THEN** the producer reads the new rate before scheduling the next tick and the config revision is reflected in subsequent diagnostics
