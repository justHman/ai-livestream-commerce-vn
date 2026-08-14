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
  initial_ingest_mode: "batch" | "single";
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

export interface IngestRequest {
  session_id: string;
  comments: CommentIn[];
  viewer_count: number;
  msg_rate: number;
}

export interface CommentIn {
  text: string;
  t?: number;
}

export interface IngestResponse {
  ok: boolean;
  accepted?: number;
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

export interface PlatformWsMessage {
  text: string;
  author?: string;
  ts?: number;
}

export interface PlatformWsResponse {
  type: string;
  comment_id?: string;
  detail?: string;
  pending?: number;
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

export interface TestPreferences {
  initialIngestMode: "batch" | "single";
  commentRate: number;
}

export interface Draft {
  shop: ShopProfile;
  products: ProductEntity[];
  selectedProductIds: string[];
  productOrder: string[];
  jsonText: string;
  jsonDirty: boolean;
  errors: string[];
  testPreferences?: TestPreferences;
}

export const EMPTY_PRODUCT: ProductEntity = {
  id: "", name: "", description: "", price: null, original_price: null, promotion: "",
  colors: [], sizes: [], material: "", shipping: "", warranty: "", in_stock: true,
  stock_total: null, ref_image: "", features: [],
};

export const EMPTY_SHOP: ShopProfile = {
  shop_name: "", host_name: "", address: "", phone: "", selling_style: "",
};