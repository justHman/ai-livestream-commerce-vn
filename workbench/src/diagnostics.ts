/** Diagnostics rendering — Director + queue state into DOM. */

import type { ClusterEnvelope, DebugClustersResponse, QueueStats } from "./api_types";
import type { ClusterInfo } from "./api_types";
import type { RuntimeInspectorsSnapshot } from "./api_types";
import {
  INSPECTOR_IDS,
  escapeHtml,
  metric,
  renderArbiterTimeline,
  renderClustersSnapshot,
  renderEnvelope,
  renderEvidence,
  renderMemory,
  renderReconcile,
  renderSafety,
  renderScriptCursor,
} from "./inspectors";

export interface RenderSink {
  setText: (id: string, text: string) => void;
  setHtml: (id: string, html: string) => void;
}

const DIAGNOSTIC_IDS = [
  "metrics",
  "embedderStatus",
  "currentDecision",
  "playbackState",
  "selectedCluster",
  "currentPrompt",
  "generatedScript",
  "upcomingWork",
  "completedHistory",
  "clusterList",
  ...INSPECTOR_IDS,
] as const;

function renderClusters(clusters: ClusterInfo[]): string {
  if (!clusters.length) {
    return "<p>Chưa có active cluster.</p>";
  }
  return clusters
    .map((cluster, index) => {
      const members = (cluster.members ?? [])
        .map((member) => `<li>${escapeHtml(member)}</li>`)
        .join("");
      return (
        `<details><summary>Cụm ${index + 1} · size=${cluster.size} · ` +
        `${cluster.category ?? "unknown"}/${cluster.intent ?? "unknown"} · ` +
        `product=${cluster.product_id ?? "unknown"} · actionable=${cluster.actionable ? "có" : "không"}</summary>` +
        `<ol>${members}</ol></details>`
      );
    })
    .join("");
}

export function clearDiagnostics(sink: RenderSink): void {
  sink.setHtml("metrics", "");
  sink.setText("embedderStatus", "Embedder: chưa có snapshot.");
  sink.setText("currentDecision", "Chưa có quyết định.");
  sink.setText("playbackState", "idle");
  sink.setText("selectedCluster", "Chưa chọn cụm.");
  sink.setText("currentPrompt", "Chưa có prompt đang xử lý.");
  sink.setText("generatedScript", "Chưa có script hoàn tất.");
  sink.setText("upcomingWork", "Không có turn đang chờ.");
  sink.setText("completedHistory", "Chưa có speech hoàn tất.");
  sink.setText("clusterList", "Chưa có active cluster.");
  clearRuntimeInspectors(sink);
}

/** Reset all runtime inspector cards to their empty placeholders. */
export function clearRuntimeInspectors(sink: RenderSink): void {
  for (const id of INSPECTOR_IDS) {
    sink.setHtml(id, "");
  }
  renderRuntimeInspectors(null, sink);
}

export function renderDiagnostics(data: DebugClustersResponse, sink: RenderSink): void {
  const queue = data.queue_stats ?? ({} as QueueStats);
  const history = queue.completed_speech_history ?? [];
  const latest = history.length ? history[history.length - 1] : null;
  const speechQueue = queue.speech_queue ?? {};
  const currentProduct =
    (speechQueue as { current_product?: { product_id: string; name: string } }).current_product ??
    null;

  const metrics =
    metric("received_total", queue.received_total ?? 0) +
    metric("buffered_comments", queue.buffered_comments ?? 0) +
    metric("active_comments", queue.active_comments ?? 0) +
    metric("director_cycles", queue.director_cycles ?? 0) +
    metric("active_decision", (queue.active_decision ?? 0) ? 1 : 0) +
    metric("queued_decisions", queue.queued_decisions ?? 0) +
    metric("completed_speeches", queue.completed_speeches ?? 0) +
    metric("singleton_clusters", data.singleton_clusters ?? 0) +
    metric("actionable_clusters", data.actionable_clusters ?? 0) +
    metric("total_clusters", data.total_clusters ?? 0);
  sink.setHtml("metrics", metrics);
  sink.setText(
    "embedderStatus",
    `Embedder: ${data.embedder_name ?? "unknown"} · status=${data.embedder_status ?? "unknown"}`,
  );
  sink.setText(
    "currentDecision",
    currentProduct
      ? `Không có turn active.\nCurrent product=${currentProduct.product_id} ${currentProduct.name ?? ""}`
      : "Chưa có quyết định.",
  );
  sink.setText("playbackState", latest?.state ?? "idle");
  sink.setText("selectedCluster", (latest?.selected_cluster ?? []).join("\n") || "Chưa chọn cụm.");
  sink.setText("currentPrompt", latest?.prompt ?? "Chưa có prompt đang xử lý.");
  sink.setText("generatedScript", latest?.script ?? "Chưa có script hoàn tất.");
  sink.setText("upcomingWork", "Không có turn đang chờ.");
  sink.setText(
    "completedHistory",
    history.length ? JSON.stringify(history, null, 2) : "Chưa có speech hoàn tất.",
  );
  sink.setText("clusterList", renderClusters(data.clusters ?? []));
}

export function diagnosticsIds(): readonly string[] {
  return DIAGNOSTIC_IDS;
}

/** Render the C15 runtime inspector cards (safety/cluster/reconcile/envelope/
 * memory/evidence/script/arbiter). Accepts null for empty state. */
export function renderRuntimeInspectors(snapshot: RuntimeInspectorsSnapshot | null, sink: RenderSink): void {
  sink.setHtml("safetyInspector", renderSafety(snapshot?.safety));
  sink.setHtml("clusterInspector", renderClustersSnapshot(snapshot?.clusters));
  sink.setHtml("reconcileInspector", renderReconcile(snapshot?.fast_lane, snapshot?.clusters));
  sink.setHtml("envelopeInspector", renderEnvelope(snapshot?.envelope));
  sink.setHtml("memoryInspector", renderMemory(snapshot?.memory));
  sink.setHtml("evidenceInspector", renderEvidence(snapshot?.agent));
  sink.setHtml("scriptCursorInspector", renderScriptCursor(snapshot?.script_cursor));
  sink.setHtml("arbiterTimelineInspector", renderArbiterTimeline(snapshot?.arbiter));
}

export function renderEnvelopeSummary(envelope: ClusterEnvelope): string {
  const platforms = envelope.source_platform_counts
    .map(([platform, count]) => `${escapeHtml(platform)}:${count}`)
    .join(" ");
  return (
    `<div class="envelope-summary">cluster=${escapeHtml(envelope.cluster_id)} · ` +
    `intent=${escapeHtml(envelope.intent)} · score=${envelope.ranking_score} · ` +
    `msgs=${envelope.message_count} · viewers=${envelope.unique_viewer_count} · ` +
    `products=${envelope.resolved_product_ids.map(escapeHtml).join(",")} · ` +
    `platforms=${platforms}</div>`
  );
}

export { renderClusters };