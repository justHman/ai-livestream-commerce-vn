/** Diagnostics rendering tests — behavior/state assertions, no internals. */

import { describe, expect, it } from "vitest";

import { renderDiagnostics, clearDiagnostics, clearRuntimeInspectors, renderRuntimeInspectors, type RenderSink } from "../src/diagnostics";
import type { DebugClustersResponse, RuntimeInspectorsSnapshot } from "../src/api_types";

function makeSink(): { sink: RenderSink; text: Record<string, string>; html: Record<string, string> } {
  const text: Record<string, string> = {};
  const html: Record<string, string> = {};
  const sink: RenderSink = {
    setText: (id, value) => { text[id] = value; },
    setHtml: (id, value) => { html[id] = value; },
  };
  return { sink, text, html };
}

const sampleData: DebugClustersResponse = {
  clusters: [
    { size: 3, category: "price", intent: "ask", product_id: "P001", actionable: true, members: ["m1", "m2", "m3"] },
  ],
  singleton_clusters: 2,
  actionable_clusters: 1,
  total_clusters: 3,
  embedder_name: "hashing",
  embedder_status: "ready",
  queue_stats: {
    received_total: 10,
    buffered_comments: 2,
    active_comments: 4,
    director_cycles: 5,
    active_decision: 1,
    queued_decisions: 2,
    completed_speeches: 1,
    completed_speech_history: [
      { turn_id: "t1", action: "sell_product", text: "abc", state: "playback", selected_cluster: ["c1"], script: "kịch bản" },
    ],
  },
};

describe("diagnostics rendering", () => {
  it("renders queue metrics from response state", () => {
    const { sink, html } = makeSink();
    renderDiagnostics(sampleData, sink);
    expect(html.metrics).toContain("received_total");
    expect(html.metrics).toContain(">10<");
    expect(html.metrics).toContain(">2<");
  });

  it("renders cluster list from state", () => {
    const { sink, text } = makeSink();
    renderDiagnostics(sampleData, sink);
    expect(text.clusterList).toContain("Cụm 1");
    expect(text.clusterList).toContain("size=3");
    expect(text.clusterList).toContain("P001");
  });

  it("renders completed history as JSON", () => {
    const { sink, text } = makeSink();
    renderDiagnostics(sampleData, sink);
    expect(text.completedHistory).toContain("kịch bản");
    expect(text.completedHistory).toContain("t1");
  });

  it("clear resets all diagnostic regions", () => {
    const { sink, text } = makeSink();
    renderDiagnostics(sampleData, sink);
    clearDiagnostics(sink);
    expect(text.currentDecision).toBe("Chưa có quyết định.");
    expect(text.playbackState).toBe("idle");
    expect(text.embedderStatus).toBe("Embedder: chưa có snapshot.");
  });

  it("clear resets all diagnostic regions including metrics html", () => {
    const { sink, html } = makeSink();
    renderDiagnostics(sampleData, sink);
    clearDiagnostics(sink);
    expect(html.metrics).toBe("");
  });

  it("handles empty clusters", () => {
    const { sink, text } = makeSink();
    renderDiagnostics({ ...sampleData, clusters: [] }, sink);
    expect(text.clusterList).toContain("Chưa có active cluster.");
  });

  it("handles absent history", () => {
    const { sink, text } = makeSink();
    renderDiagnostics({ ...sampleData, queue_stats: { ...sampleData.queue_stats, completed_speech_history: [] } }, sink);
    expect(text.completedHistory).toContain("Chưa có speech hoàn tất.");
  });
});

describe("runtime inspectors rendering", () => {
  const sampleInspectors: RuntimeInspectorsSnapshot = {
    safety: {
      counters: { malformed: 1, spam: 2 },
      total_rejected: 3,
      total_accepted: 7,
      ingestion: { accepted: 7, rejected_by_reason: { profanity: 1 } },
    },
    clusters: {
      active_cluster_count: 1,
      unreconciled_count: 5,
      clusters: [
        {
          cluster_id: "c-101",
          intent: "ask",
          message_count: 4,
          unique_viewer_count: 3,
          product_resolution_confidence: 0.92,
          resolved_product_ids: ["P001", "P002"],
          representative_comment_ids: ["m1", "m2"],
        },
      ],
    },
    fast_lane: {
      pending: 2,
      embedded_total: 9,
      embed_calls: 3,
      cache_hits: 6,
      wake_notifications: 4,
      reconciles_run: 1,
      reconcile_merged_total: 1,
      reconcile_split_total: 0,
      reconciliation_failures: 0,
      last_reconcile: { clusters_before: 3, clusters_after: 2, merged: 1, split: 0, members_removed: 1 },
    },
    envelope: {
      cluster_id: "c-101",
      intent: "ask",
      ranking_score: 8.5,
      message_count: 4,
      unique_viewer_count: 3,
      resolved_product_ids: ["P001"],
      source_platform_counts: [["tiktok", 3], ["shopee", 1]],
    },
    memory: {
      session: { size: 40, max_size: 60, revision: 2, is_within_budget: true, last_spoken_topic: "chất liệu", last_spoken_product_id: "P001" },
      topic: { size: 3, max_size: 5, revision: 1, last_topic_key: "chất-liệu" },
      evidence_cache: { size: 8, max_size: 50, revision: 3, stats: { hits: 5, misses: 2, stale: 0 } },
    },
    agent: {
      planner: { requested_selectors: 3, cache_hits: 2, cache_misses: 1, stale_refreshes: 0, batch_fan_in: 2 },
      telemetry: {
        execution_path: "complex",
        evidence_cache_hits: 2,
        evidence_cache_misses: 1,
        evidence_rounds: 1,
        llm_calls: 2,
        prompt_tokens: 100,
        generated_tokens: 50,
        latency_ms: 320,
        terminal: "completed",
      },
    },
    script_cursor: {
      script_set_id: "ss-1",
      script_version: 3,
      product_id: "P001",
      sentence_index: 8,
      last_completed_sentence_index: 7,
      next_sentence: "Câu tiếp theo.",
    },
    arbiter: {
      state: "QNA_PLAYING",
      state_history: [
        { state: "SCRIPT_SENTENCE_PLAYING", ts: "t0" },
        { state: "QNA_PLAYING", ts: "t1" },
      ],
      pending_board: {
        candidate_count: 1,
        max_candidates: 3,
        candidates: [{ cluster_id: "c-102", score: 7.2, first_seen_at: "t0", last_seen_at: "t1" }],
        cooldown_cluster_ids: ["c-100"],
      },
    },
  };

  it("renders safety counters with reason codes and totals", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(sampleInspectors, sink);
    expect(html.safetyInspector).toContain("malformed");
    expect(html.safetyInspector).toContain("spam");
    expect(html.safetyInspector).toContain("total_rejected");
    expect(html.safetyInspector).toContain("total_accepted");
    expect(html.safetyInspector).toContain("profanity=1");
  });

  it("renders stable cluster id, representatives, unique viewers, product confidence", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(sampleInspectors, sink);
    expect(html.clusterInspector).toContain("c-101");
    expect(html.clusterInspector).toContain("Representatives: m1, m2");
    expect(html.clusterInspector).toContain("viewers=3");
    expect(html.clusterInspector).toContain("conf=0.92");
    expect(html.clusterInspector).toContain("P001");
  });

  it("renders reconciliation trigger state and last pass counters", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(sampleInspectors, sink);
    expect(html.reconcileInspector).toContain("unreconciled");
    expect(html.reconcileInspector).toContain("wake_notifications");
    expect(html.reconcileInspector).toContain("reconciles_run");
    expect(html.reconcileInspector).toContain("clusters 3→2");
    expect(html.reconcileInspector).toContain("members_removed=1");
  });

  it("renders the exact selected envelope fields", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(sampleInspectors, sink);
    expect(html.envelopeInspector).toContain("c-101");
    expect(html.envelopeInspector).toContain("ranking_score");
    expect(html.envelopeInspector).toContain("8.5");
    expect(html.envelopeInspector).toContain("unique_viewers");
    expect(html.envelopeInspector).toContain("tiktok:3 · shopee:1");
  });

  it("renders memory sizes and keys only", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(sampleInspectors, sink);
    expect(html.memoryInspector).toContain("session_size");
    expect(html.memoryInspector).toContain("Last spoken topic=chất liệu");
    expect(html.memoryInspector).toContain("Last topic key=chất-liệu");
    expect(html.memoryInspector).toContain("Hits=5 · misses=2 · stale=0");
  });

  it("renders evidence planner hit/miss and agent rounds", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(sampleInspectors, sink);
    expect(html.evidenceInspector).toContain("requested_selectors");
    expect(html.evidenceInspector).toContain("batch_fan_in");
    expect(html.evidenceInspector).toContain("evidence_rounds");
    expect(html.evidenceInspector).toContain("llm_calls");
    expect(html.evidenceInspector).toContain("latency_ms");
    expect(html.evidenceInspector).toContain("Terminal=completed");
  });

  it("renders script cursor version, current, last completed and next sentence", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(sampleInspectors, sink);
    expect(html.scriptCursorInspector).toContain("script_set");
    expect(html.scriptCursorInspector).toContain("ss-1");
    expect(html.scriptCursorInspector).toContain("script_version");
    expect(html.scriptCursorInspector).toContain("sentence_index");
    expect(html.scriptCursorInspector).toContain("last_completed");
    expect(html.scriptCursorInspector).toContain("Next sentence: Câu tiếp theo.");
  });

  it("renders arbiter state history entries and pending board", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(sampleInspectors, sink);
    expect(html.arbiterTimelineInspector).toContain("QNA_PLAYING");
    expect(html.arbiterTimelineInspector).toContain("SCRIPT_SENTENCE_PLAYING @ t0");
    expect(html.arbiterTimelineInspector).toContain("candidate_count");
    expect(html.arbiterTimelineInspector).toContain("c-102 · score=7.2");
    expect(html.arbiterTimelineInspector).toContain("Cooldown: c-100");
  });

  it("renders placeholders for null or partial snapshots", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(null, sink);
    expect(html.safetyInspector).toContain("chưa có dữ liệu");
    expect(html.clusterInspector).toContain("chưa có dữ liệu");
    expect(html.reconcileInspector).toContain("chưa có dữ liệu");
    expect(html.envelopeInspector).toContain("chưa có dữ liệu");
    expect(html.memoryInspector).toContain("chưa có dữ liệu");
    expect(html.evidenceInspector).toContain("chưa có dữ liệu");
    expect(html.scriptCursorInspector).toContain("chưa có dữ liệu");
    expect(html.arbiterTimelineInspector).toContain("chưa có dữ liệu");
    renderRuntimeInspectors({ safety: { counters: { spam: 1 } } }, sink);
    expect(html.safetyInspector).toContain("spam");
    expect(html.clusterInspector).toContain("chưa có dữ liệu");
  });

  it("never renders raw unescaped text in inspector html", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors({
      safety: { counters: { "<script>alert(1)</script>": 1 } },
      clusters: { clusters: [{ cluster_id: "<b>x</b>" }] },
    }, sink);
    expect(html.safetyInspector).not.toContain("<script>alert(1)</script>");
    expect(html.safetyInspector).toContain("&lt;script&gt;");
    expect(html.clusterInspector).not.toContain("<b>x</b>");
    expect(html.clusterInspector).toContain("&lt;b&gt;x&lt;/b&gt;");
  });

  it("clear resets all inspector cards", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(sampleInspectors, sink);
    clearRuntimeInspectors(sink);
    expect(html.safetyInspector).toContain("chưa có dữ liệu");
    expect(html.clusterInspector).toContain("chưa có dữ liệu");
    expect(html.reconcileInspector).toContain("chưa có dữ liệu");
    expect(html.envelopeInspector).toContain("chưa có dữ liệu");
    expect(html.memoryInspector).toContain("chưa có dữ liệu");
    expect(html.evidenceInspector).toContain("chưa có dữ liệu");
    expect(html.scriptCursorInspector).toContain("chưa có dữ liệu");
    expect(html.arbiterTimelineInspector).toContain("chưa có dữ liệu");
  });

  it("clearDiagnostics also resets inspector cards", () => {
    const { sink, html } = makeSink();
    renderRuntimeInspectors(sampleInspectors, sink);
    clearDiagnostics(sink);
    expect(html.safetyInspector).toContain("chưa có dữ liệu");
    expect(html.arbiterTimelineInspector).toContain("chưa có dữ liệu");
  });
});