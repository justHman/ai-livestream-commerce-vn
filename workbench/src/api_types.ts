/** Canonical API types for the Livento v1 REST/WS contract. */

export interface HealthReadyResponse {
  ok: boolean;
  status: "ready" | "not_ready";
  render_backend: string;
  llm_engine: string;
  tts_engine: string;
}

export interface EnginesResponse {
  llm: EngineInfo;
  tts: EngineInfo;
  available_llm_presets?: EnginePreset[];
  available_tts_presets?: EnginePreset[];
  voices?: VoiceInfo[];
}

export interface EngineInfo {
  id: string;
  name: string;
  engine: string;
  model: string;
}

export interface EnginePreset {
  id: string;
  label: string;
  engine: string;
  model?: string;
  device?: string;
  sample_rate?: number;
  ready?: boolean;
  capabilities?: string[];
  weights_path?: string;
  notes?: string;
}

export interface VoiceInfo {
  id: string;
  name: string;
  active?: boolean;
}

export interface AvatarInfo {
  id: string;
  label: string;
  ready?: boolean;
  capabilities?: string[];
  scope?: string;
}

export interface AvatarsResponse {
  avatars: AvatarInfo[];
}

export interface StartRequest {
  avatar_id?: string | null;
  is_sandbox: boolean;
}

export interface StartResponse {
  session_id: string;
  mode: string;
  livekit_url?: string;
  livekit_client_token?: string;
}

export interface SessionRequest {
  session_id: string;
}

export interface LiteKitCredentials {
  url: string;
  token: string;
}

export interface SayRequest {
  session_id: string;
  text: string;
  generate: boolean;
}

export interface ShopProfile {
  shop_name: string;
  host_name: string;
  address: string;
  phone: string;
  selling_style: string;
}

/** Flat product input — the wire view of the backend `ProductEntityIn`
 * (contracts/v1/openapi.json). The backend converts this to an
 * `EntityDocument` (facts + registry) server-side; this TS shape mirrors the
 * flat JSON body, not the entity graph. */
export interface ProductEntity {
  id: string;
  name: string;
  description: string;
  price: number | null;
  original_price: number | null;
  promotion: string;
  colors: string[];
  sizes: string[];
  material: string;
  shipping: string;
  warranty: string;
  in_stock: boolean;
  stock_total: number | null;
  ref_image: string;
  features: string[];
}

export interface AttachRequest {
  session_id: string;
  shop_profile: ShopProfile;
  products: ProductEntity[];
  runtime_config?: Partial<RuntimeConfig>;
}

export interface RuntimeConfig {
  comment_rate: number;
  max_qa_clusters_per_window: number;
  qa_window_hard_timeout_sec: number;
  qa_topic_cooldown_sec: number;
  answer_cache_variants: number;
  prepared_turn_depth: number;
  transient_retry_count: number;
  demand_pivot_enter_share: number;
  demand_pivot_exit_share: number;
}

export interface RuntimeConfigUpdate extends RuntimeConfig {
  session_id: string;
}

export interface AttachResponse {
  ok: boolean;
  products?: number;
  will_speak: boolean;
}

export interface SandboxVerifyRequest {
  avatar_id?: string | null;
  speech_text: string;
}

export interface SandboxVerifyResponse {
  ready: boolean;
  layers: VerificationLayer[];
}

export interface VerificationLayer {
  name: string;
  status: "pass" | "fail" | "skipped";
  latency_ms: number;
  error?: string;
}

export interface DebugClustersResponse {
  clusters: ClusterInfo[];
  singleton_clusters: number;
  actionable_clusters: number;
  total_clusters: number;
  embedder_name: string;
  embedder_status: string;
  cluster_merge_threshold?: number;
  multi_comment_clusters?: number;
  unanswered_clusters?: number;
  queue_stats: QueueStats;
}

export interface ClusterInfo {
  size: number;
  category: string;
  intent: string;
  product_id?: string;
  actionable: boolean;
  members?: string[];
  /** Content-safe snapshot of the exact Q&A envelope used for a decision
   * (task 7.5); optional — the backend channel that populates it lands with
   * the C15 wiring. */
  envelope?: ClusterEnvelope;
}

export interface ClusterEnvelope {
  cluster_id: string;
  intent: string;
  ranking_score: number;
  message_count: number;
  unique_viewer_count: number;
  resolved_product_ids: string[];
  source_platform_counts: Array<[string, number]>;
}

export interface QueueStats {
  received_total: number;
  buffered_comments: number;
  active_comments: number;
  director_cycles: number;
  active_decision: number;
  queued_decisions: number;
  completed_speeches: number;
  completed_speech_history?: SpeechHistoryItem[];
  singleton_clusters?: number;
  actionable_clusters?: number;
  speech_queue?: SpeechQueue;
}

export interface SpeechHistoryItem {
  turn_id: string;
  action: string;
  text: string;
  state: string;
  selected_cluster?: string[];
  prompt_layers?: PromptLayers;
  prompt?: string;
  script?: string;
}

export interface PromptLayers {
  base_role?: string;
  shop_profile?: string;
  stage_task?: string;
  final_prompt?: string;
}

export interface SpeechQueue {
  current_product?: { product_id: string; name: string };
  next_product?: { product_id: string; name: string } | null;
}

export interface DecisionInfo {
  turn_id: string;
  action: string;
  product_id?: string;
  state: string;
  prompt?: string;
  prompt_layers?: PromptLayers;
  selected_cluster?: string[];
}

export interface LifecycleEvent {
  type: string;
  turn_id?: string;
  state?: string;
  action?: string;
  text?: string;
  product_id?: string;
  selected_cluster?: string[];
  prompt_layers?: PromptLayers;
  received_total?: number;
  buffered_comments?: number;
  active_comments?: number;
  director_cycles?: number;
  completed_speeches?: number;
  active_decision?: number;
  queued_decisions?: number;
  completed_speech_history?: SpeechHistoryItem[];
}

export interface ControlWsMessage {
  type: string;
  session_id?: string;
}

export interface ConfigApplyResponse {
  ok: boolean;
  config_revision?: number;
}

export interface LiveKitRoomResponse {
  livekit_url: string;
  token: string;
  room: string;
}

export interface ViewerMessage {
  text: string;
  id?: string;
}

export interface Draft {
  shop: ShopProfile;
  products: ProductEntity[];
  selectedProductIds: string[];
  productOrder: string[];
  jsonText: string;
  jsonDirty: boolean;
  errors: string[];
}

export const EMPTY_PRODUCT: ProductEntity = {
  id: "", name: "", description: "", price: null, original_price: null, promotion: "",
  colors: [], sizes: [], material: "", shipping: "", warranty: "", in_stock: true,
  stock_total: null, ref_image: "", features: [],
};

export const EMPTY_SHOP: ShopProfile = {
  shop_name: "", host_name: "", address: "", phone: "", selling_style: "",
};

// ---------------- Universal entity context (cluster C9, Entity Data Studio) ----------------

export type EntityType = "product" | "shop" | "campaign";

export interface Fact {
  key: string;
  type: "int" | "float" | "str" | "bool";
  value: number | string | boolean;
  unit?: string | null;
  labels: string[];
  revision: number;
  freshness: "stable" | "volatile";
  updated_at: string;
  source?: string | null;
}

export type KnowledgeBlockKind = "description" | "usage" | "story" | "campaign" | "custom";

export interface KnowledgeBlock {
  id: string;
  kind: KnowledgeBlockKind;
  title: string;
  content: string;
  tags: string[];
  revision: number;
}

export interface Relation {
  target_entity_id: string;
  relation_type: string;
  metadata: Record<string, string>;
}

/** The universal entity envelope (backend `EntityDocument`): revisioned facts,
 * knowledge blocks, aliases/tags/relations. Vertical-specific attributes live
 * in facts (`custom.*` keys), never as new fields. */
export interface EntityDocument {
  id: string;
  entity_type: EntityType;
  revision: number;
  name: string;
  aliases: string[];
  tags: string[];
  facts: Fact[];
  knowledge_blocks: KnowledgeBlock[];
  relations: Relation[];
}

/** One arbitrary label/value row from the Data Studio form. Unknown labels are
 * preserved verbatim — the backend maps them to `custom.*` keys. */
export interface FactRowIn {
  label: string;
  value: string;
  unit?: string;
}

export interface KnowledgeBlockIn {
  kind: KnowledgeBlockKind;
  title: string;
  content: string;
  tags: string[];
}

/** PUT /api/v1/entities/{id} body. `common` keys are limited to the canonical
 * registry keys; `fact_rows` carry arbitrary user labels as-is. */
export interface SimpleEntityUpsertReq {
  id: string;
  entity_type: EntityType;
  name: string;
  aliases: string[];
  tags: string[];
  common: Record<string, string>;
  fact_rows: FactRowIn[];
  knowledge_blocks: KnowledgeBlockIn[];
}

export interface SuggestedFact {
  key: string;
  label: string;
  value: string;
  unit?: string | null;
  type: "int" | "float" | "str" | "bool";
}

export interface SuggestFactsRequest {
  entity_type: EntityType;
  text: string;
  block_kind: KnowledgeBlockKind;
  block_title: string;
}

/** POST /api/v1/entities/suggestions — extraction is optional; may return an
 * empty list when the LLM is disabled. */
export interface SuggestionResponse {
  suggestions: SuggestedFact[];
  source_block_id?: string;
  note?: string;
}

export interface RenderPreviewResponse {
  entity_id: string;
  selectors: string[];
  rendered: string;
}

// ---------------- Runtime inspectors (cluster C15, section 17) ----------------
// Snapshots are content-safe: counts, ids, keys, sizes — never private viewer
// text. Every field is optional so partial payloads render placeholders.

/** Backend `SafetyCounters.to_dict()` + platform event ingestion stats (17.1). */
export interface SafetyCountersSnapshot {
  counters?: Record<string, number>;
  total_rejected?: number;
  total_accepted?: number;
  ingestion?: IngestionStats;
}

export interface IngestionStats {
  accepted?: number;
  rejected_by_reason?: Record<string, number>;
}

/** Backend reducer `ClusterStore` stats + stable `LiveCluster` fields (17.2). */
export interface ClusterStoreSnapshot {
  active_cluster_count?: number;
  evicted_count?: number;
  member_ids_count?: number;
  unreconciled_count?: number;
  clusters?: StableClusterInfo[];
}

export interface StableClusterInfo {
  cluster_id: string;
  intent?: string;
  message_count?: number;
  unique_viewer_count?: number;
  product_resolution_confidence?: number;
  resolved_product_ids?: string[];
  representative_comment_ids?: string[];
  novelty_fingerprint?: string;
  cohesion?: number;
  skip_count?: number;
  last_selected_at?: string;
  last_answered_at?: string;
}

/** Fast reducer + reconciliation trigger state (17.3). */
export interface FastReducerStats {
  pending?: number;
  embedded_total?: number;
  embed_calls?: number;
  cache_hits?: number;
  wake_notifications?: number;
  reconciles_run?: number;
  reconcile_merged_total?: number;
  reconcile_split_total?: number;
  reconciliation_failures?: number;
  last_reconcile?: ReconcileOutcome;
  last_reconciliation_failure?: string;
}

export interface ReconcileOutcome {
  clusters_before?: number;
  clusters_after?: number;
  merged?: number;
  split?: number;
  members_removed?: number;
}

/** Full `ClusterEnvelope` + backend `qa_resolver.latest_envelope_decisions` (17.4). */
export interface EnvelopeDecision {
  cluster_id: string;
  intent?: string;
  ranking_score?: number;
  message_count?: number;
  unique_viewer_count?: number;
  resolved_product_ids?: string[];
  source_platform_counts?: Array<[string, number]>;
}

/** Structured memory metadata — sizes/revisions/keys only (17.5). */
export interface MemorySnapshot {
  session?: SessionMemorySnapshot;
  topic?: TopicMemorySnapshot;
  evidence_cache?: EvidenceCacheSnapshot;
}

export interface SessionMemorySnapshot {
  size?: number;
  max_size?: number;
  revision?: number;
  is_within_budget?: boolean;
  last_spoken_topic?: string;
  last_spoken_product_id?: string;
}

export interface TopicMemorySnapshot {
  size?: number;
  max_size?: number;
  revision?: number;
  last_topic_key?: string;
}

export interface EvidenceCacheSnapshot {
  size?: number;
  max_size?: number;
  revision?: number;
  stats?: { hits?: number; misses?: number; stale?: number };
}

/** Agent execution telemetry + evidence planner (17.6). */
export interface AgentTelemetrySnapshot {
  telemetry?: ExecutionTelemetry;
  planner?: EvidencePlannerStats;
}

export interface ExecutionTelemetry {
  execution_path?: string;
  evidence_cache_hits?: number;
  evidence_cache_misses?: number;
  evidence_rounds?: number;
  llm_calls?: number;
  prompt_tokens?: number;
  generated_tokens?: number;
  latency_ms?: number;
  terminal?: string;
}

export interface EvidencePlannerStats {
  requested_selectors?: number;
  cache_hits?: number;
  cache_misses?: number;
  stale_refreshes?: number;
  batch_fan_in?: number;
}

/** Bound script cursor position (17.7). */
export interface ScriptPositionSnapshot {
  script_set_id?: string;
  script_version?: number;
  product_id?: string;
  sentence_index?: number;
  last_completed_sentence_index?: number;
  next_sentence?: string;
}

/** Speech arbiter state machine + timeline + pending board (17.8). */
export type ArbiterStateName =
  | "SCRIPT_READY"
  | "SCRIPT_SENTENCE_PLAYING"
  | "QNA_PENDING"
  | "QNA_PREPARING"
  | "QNA_PLAYING"
  | "RESUME_BRIDGE"
  | "STOPPED"
  | "FAILED";

export interface ArbiterTimelineSnapshot {
  state?: ArbiterStateName;
  updated_at?: string;
  state_history?: Array<{ state: ArbiterStateName; ts: string }>;
  pending_board?: ArbiterPendingBoard;
}

export interface ArbiterPendingBoard {
  candidate_count?: number;
  max_candidates?: number;
  candidates?: Array<{
    cluster_id: string;
    score?: number;
    first_seen_at?: string;
    last_seen_at?: string;
  }>;
  cooldown_cluster_ids?: string[];
}

/** Aggregated inspector payload fed by main.ts — all sections optional. */
export interface RuntimeInspectorsSnapshot {
  safety?: SafetyCountersSnapshot;
  clusters?: ClusterStoreSnapshot;
  fast_lane?: FastReducerStats;
  envelope?: EnvelopeDecision;
  memory?: MemorySnapshot;
  agent?: AgentTelemetrySnapshot;
  script_cursor?: ScriptPositionSnapshot;
  arbiter?: ArbiterTimelineSnapshot;
}