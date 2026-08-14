/** Runtime inspectors (C15 section 17) — domain-pure snapshot rendering.
 *
 * Every render function takes a typed snapshot (or null) and returns an HTML
 * string. No DOM access, no timers, no side effects. Snapshot fields are
 * optional; absent sections render "chưa có dữ liệu" placeholders.
 */

import type {
  AgentTelemetrySnapshot,
  ArbiterTimelineSnapshot,
  ClusterStoreSnapshot,
  EnvelopeDecision,
  FastReducerStats,
  MemorySnapshot,
  SafetyCountersSnapshot,
  ScriptPositionSnapshot,
} from "./api_types";

export const INSPECTOR_IDS = [
  "safetyInspector",
  "clusterInspector",
  "reconcileInspector",
  "envelopeInspector",
  "memoryInspector",
  "evidenceInspector",
  "scriptCursorInspector",
  "arbiterTimelineInspector",
] as const;

export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function metric(label: string, value: number | string): string {
  return `<div class="metric"><span>${escapeHtml(label)}</span><strong>${String(value ?? 0)}</strong></div>`;
}

function missing(section: string): string {
  return `<p>${escapeHtml(section)}: chưa có dữ liệu.</p>`;
}

function sortKeys(record: Record<string, number> | undefined): Array<[string, number]> {
  return Object.entries(record ?? {}).sort(([a], [b]) => a.localeCompare(b));
}

/** 17.1 Safety Gate counters + ingestion stats. Raw text is never rendered —
 * every value passes through escapeHtml. */
export function renderSafety(snapshot: SafetyCountersSnapshot | null | undefined): string {
  if (!snapshot) return missing("Safety Gate");
  let html = "";
  if (snapshot.counters && Object.keys(snapshot.counters).length) {
    html += `<div class="metrics">${sortKeys(snapshot.counters)
      .map(([reason, count]) => metric(reason, count))
      .join("")}</div>`;
  }
  if (snapshot.total_rejected !== undefined || snapshot.total_accepted !== undefined) {
    html += `<div class="metrics">${metric("total_accepted", snapshot.total_accepted ?? 0)}${metric("total_rejected", snapshot.total_rejected ?? 0)}</div>`;
  }
  if (snapshot.ingestion) {
    html += `<p>Ingestion: accepted=${snapshot.ingestion.accepted ?? 0}</p>`;
    const rejected = sortKeys(snapshot.ingestion.rejected_by_reason);
    if (rejected.length) {
      html += `<p>Rejected by reason: ${rejected.map(([reason, count]) => `${escapeHtml(reason)}=${count}`).join(" · ")}</p>`;
    }
  }
  return html || missing("Safety Gate");
}

/** 17.2 Stable clusters: stable id, representatives, unique viewers, product
 * confidence/resolution. */
export function renderClustersSnapshot(snapshot: ClusterStoreSnapshot | null | undefined): string {
  if (!snapshot) return missing("Clusters ổn định");
  const clusters = snapshot.clusters ?? [];
  if (!clusters.length) return missing("Clusters ổn định");
  return clusters
    .map((cluster) => {
      const reps = (cluster.representative_comment_ids ?? []).map(escapeHtml).join(", ");
      const products = (cluster.resolved_product_ids ?? []).map(escapeHtml).join(",");
      return (
        `<details><summary>${escapeHtml(cluster.cluster_id)} · intent=${escapeHtml(cluster.intent ?? "unknown")} · ` +
        `msgs=${cluster.message_count ?? 0} · viewers=${cluster.unique_viewer_count ?? 0} · ` +
        `conf=${cluster.product_resolution_confidence ?? "?"} · products=${products || "none"}</summary>` +
        `<p>Representatives: ${reps || "—"}</p>` +
        `<p>Novelty=${escapeHtml(cluster.novelty_fingerprint ?? "—")} · cohesion=${cluster.cohesion ?? "—"} · ` +
        `skip=${cluster.skip_count ?? 0} · last_selected=${escapeHtml(cluster.last_selected_at ?? "—")} · ` +
        `last_answered=${escapeHtml(cluster.last_answered_at ?? "—")}</p></details>`
      );
    })
    .join("");
}

/** 17.3 Fast-lane + reconciliation trigger state, plus ClusterStore counters. */
export function renderReconcile(snapshot: FastReducerStats | null | undefined, store: ClusterStoreSnapshot | null | undefined): string {
  if (!snapshot && !store) return missing("Fast-lane + reconciliation");
  let html = "";
  if (store && (store.unreconciled_count !== undefined || store.active_cluster_count !== undefined)) {
    html += `<div class="metrics">${metric("unreconciled", store.unreconciled_count ?? 0)}${metric("active_clusters", store.active_cluster_count ?? 0)}${metric("members", store.member_ids_count ?? 0)}${metric("evicted", store.evicted_count ?? 0)}</div>`;
  }
  if (snapshot) {
    html += `<div class="metrics">${metric("pending", snapshot.pending ?? 0)}${metric("embedded_total", snapshot.embedded_total ?? 0)}${metric("embed_calls", snapshot.embed_calls ?? 0)}${metric("cache_hits", snapshot.cache_hits ?? 0)}${metric("wake_notifications", snapshot.wake_notifications ?? 0)}${metric("reconciles_run", snapshot.reconciles_run ?? 0)}${metric("merged_total", snapshot.reconcile_merged_total ?? 0)}${metric("split_total", snapshot.reconcile_split_total ?? 0)}${metric("failures", snapshot.reconciliation_failures ?? 0)}</div>`;
    const last = snapshot.last_reconcile;
    if (last) {
      html += `<p>Last reconcile: clusters ${last.clusters_before ?? "?"}→${last.clusters_after ?? "?"} · merged=${last.merged ?? 0} · split=${last.split ?? 0} · members_removed=${last.members_removed ?? 0}</p>`;
    }
    if (snapshot.last_reconciliation_failure) {
      html += `<p>Last failure: ${escapeHtml(snapshot.last_reconciliation_failure)}</p>`;
    }
  }
  return html || missing("Fast-lane + reconciliation");
}

/** 17.4 Exact selected ClusterEnvelope (latest envelope decisions list). */
export function renderEnvelope(snapshot: EnvelopeDecision | null | undefined): string {
  if (!snapshot) return missing("ClusterEnvelope đã chọn");
  const platforms = (snapshot.source_platform_counts ?? [])
    .map(([platform, count]) => `${escapeHtml(platform)}:${count}`)
    .join(" · ");
  return (
    `<div class="metrics">${metric("cluster", snapshot.cluster_id)}${metric("intent", snapshot.intent ?? "unknown")}${metric("ranking_score", snapshot.ranking_score ?? 0)}${metric("message_count", snapshot.message_count ?? 0)}${metric("unique_viewers", snapshot.unique_viewer_count ?? 0)}</div>` +
    `<p>Products: ${(snapshot.resolved_product_ids ?? []).map(escapeHtml).join(", ") || "—"}</p>` +
    `<p>Platforms: ${platforms || "—"}</p>`
  );
}

/** 17.5 Structured memory metadata — sizes/revisions/keys only. */
export function renderMemory(snapshot: MemorySnapshot | null | undefined): string {
  if (!snapshot) return missing("Memory layers");
  const session = snapshot.session;
  const topic = snapshot.topic;
  const cache = snapshot.evidence_cache;
  let html = "";
  if (session) {
    html += `<div class="metrics">${metric("session_size", session.size ?? 0)}${metric("session_max", session.max_size ?? 0)}${metric("session_revision", session.revision ?? 0)}${metric("within_budget", session.is_within_budget ? "có" : "không")}</div>`;
    html += `<p>Last spoken topic=${escapeHtml(session.last_spoken_topic ?? "—")} · product=${escapeHtml(session.last_spoken_product_id ?? "—")}</p>`;
  }
  if (topic) {
    html += `<div class="metrics">${metric("topic_size", topic.size ?? 0)}${metric("topic_max", topic.max_size ?? 0)}${metric("topic_revision", topic.revision ?? 0)}</div>`;
    html += `<p>Last topic key=${escapeHtml(topic.last_topic_key ?? "—")}</p>`;
  }
  if (cache) {
    html += `<div class="metrics">${metric("cache_size", cache.size ?? 0)}${metric("cache_max", cache.max_size ?? 0)}${metric("cache_revision", cache.revision ?? 0)}</div>`;
    if (cache.stats) {
      html += `<p>Hits=${cache.stats.hits ?? 0} · misses=${cache.stats.misses ?? 0} · stale=${cache.stats.stale ?? 0}</p>`;
    }
  }
  return html || missing("Memory layers");
}

/** 17.6 Evidence planner + agent execution telemetry. */
export function renderEvidence(snapshot: AgentTelemetrySnapshot | null | undefined): string {
  if (!snapshot) return missing("Evidence + Agent rounds");
  const planner = snapshot.planner;
  const telemetry = snapshot.telemetry;
  let html = "";
  if (planner) {
    html += `<div class="metrics">${metric("requested_selectors", planner.requested_selectors ?? 0)}${metric("cache_hits", planner.cache_hits ?? 0)}${metric("cache_misses", planner.cache_misses ?? 0)}${metric("stale_refreshes", planner.stale_refreshes ?? 0)}${metric("batch_fan_in", planner.batch_fan_in ?? 0)}</div>`;
  }
  if (telemetry) {
    html += `<div class="metrics">${metric("execution_path", telemetry.execution_path ?? "unknown")}${metric("evidence_hits", telemetry.evidence_cache_hits ?? 0)}${metric("evidence_misses", telemetry.evidence_cache_misses ?? 0)}${metric("evidence_rounds", telemetry.evidence_rounds ?? 0)}${metric("llm_calls", telemetry.llm_calls ?? 0)}${metric("prompt_tokens", telemetry.prompt_tokens ?? 0)}${metric("generated_tokens", telemetry.generated_tokens ?? 0)}${metric("latency_ms", telemetry.latency_ms ?? 0)}</div>`;
    html += `<p>Terminal=${escapeHtml(telemetry.terminal ?? "unknown")}</p>`;
  }
  return html || missing("Evidence + Agent rounds");
}

/** 17.7 Bound script cursor: version, current sentence, last completed, next. */
export function renderScriptCursor(snapshot: ScriptPositionSnapshot | null | undefined): string {
  if (!snapshot) return missing("Script cursor");
  return (
    `<div class="metrics">${metric("script_set", snapshot.script_set_id ?? "—")}${metric("script_version", snapshot.script_version ?? 0)}${metric("product", snapshot.product_id ?? "—")}${metric("sentence_index", snapshot.sentence_index ?? 0)}${metric("last_completed", snapshot.last_completed_sentence_index ?? "—")}</div>` +
    `<p>Next sentence: ${escapeHtml(snapshot.next_sentence ?? "—")}</p>`
  );
}

/** 17.8 Speech-arbiter timeline + pending board. */
export function renderArbiterTimeline(snapshot: ArbiterTimelineSnapshot | null | undefined): string {
  if (!snapshot) return missing("Speech-arbiter timeline");
  let html = "";
  if (snapshot.state) {
    html += `<div class="metrics">${metric("state", snapshot.state)}${snapshot.updated_at ? metric("updated_at", snapshot.updated_at) : ""}</div>`;
  }
  const history = snapshot.state_history ?? [];
  if (history.length) {
    html += "<p>Timeline:</p><ol>" + history
      .map((entry) => `<li>${escapeHtml(entry.state)} @ ${escapeHtml(entry.ts)}</li>`)
      .join("") + "</ol>";
  }
  const board = snapshot.pending_board;
  if (board) {
    html += `<div class="metrics">${metric("candidate_count", board.candidate_count ?? 0)}${metric("max_candidates", board.max_candidates ?? 0)}</div>`;
    const candidates = board.candidates ?? [];
    if (candidates.length) {
      html += "<p>Pending:</p><ol>" + candidates
        .map((candidate) => `<li>${escapeHtml(candidate.cluster_id)} · score=${candidate.score ?? "?"} · first=${escapeHtml(candidate.first_seen_at ?? "—")} · last=${escapeHtml(candidate.last_seen_at ?? "—")}</li>`)
        .join("") + "</ol>";
    }
    if (board.cooldown_cluster_ids?.length) {
      html += `<p>Cooldown: ${board.cooldown_cluster_ids.map(escapeHtml).join(", ")}</p>`;
    }
  }
  return html || missing("Speech-arbiter timeline");
}

/** Per-section renderers are pure; the sink-writing composition lives in
 * diagnostics.ts (`renderRuntimeInspectors`). */
