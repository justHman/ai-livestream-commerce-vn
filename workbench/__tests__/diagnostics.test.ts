/** Diagnostics rendering tests — behavior/state assertions, no internals. */

import { describe, expect, it } from "vitest";

import { renderDiagnostics, clearDiagnostics, type RenderSink } from "../src/diagnostics";
import type { DebugClustersResponse } from "../src/api_types";

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