/** Deterministic multi-platform event simulator tests (OpenSpec 16).
 *
 * No timers in the testable core: emission order/counts are advanced
 * manually via tick()/emitSource(), and timestamps are fixed via t0.
 */

import { describe, expect, it } from "vitest";

import {
  EventSimulator,
  MAX_EVENTS_PER_REQUEST,
  SOURCE_PLATFORMS,
  defaultSources,
  eventsRequestBody,
  lcg,
} from "../src/eventSimulator";
import type { SimSourceDefinition } from "../src/eventSimulator";
import { loadFixtures, replayRequestBody } from "../src/fixtures";
import { SE_REPLAY_SCENARIOS } from "../src/fixtures/se_replay_fixtures";
import { validateSimulatorInput } from "../src/simulator";

const T0 = 1_700_000_000;

function makeSources(overrides: Array<Partial<SimSourceDefinition["config"] & { streamId: string }>> = []): SimSourceDefinition[] {
  const { viewer_messages } = loadFixtures();
  const defs = defaultSources(viewer_messages);
  return defs.map((d, i) => {
    const o = overrides[i];
    if (!o) return d;
    return { ...d, streamId: o.streamId ?? d.streamId, config: { ...d.config, ...o } };
  });
}

function collect(sim: EventSimulator): Array<{ platform: string; streamId: string; eventId: string; text?: string; type: string; occurredAt: number }> {
  return sim.emittedEvents().map((e) => ({
    platform: e.source.platform,
    streamId: e.source.streamId,
    eventId: e.event.event_id,
    text: e.source.text,
    type: e.event.type,
    occurredAt: e.event.occurred_at,
  }));
}

describe("EventSimulator determinism", () => {
  it("identical seed -> identical emission order", () => {
    const sources = makeSources();
    const a = new EventSimulator(sources, { onEmit: () => undefined }, { seed: 42 });
    const b = new EventSimulator(sources, { onEmit: () => undefined }, { seed: 42 });
    expect(a.orderStreamIds(20)).toEqual(b.orderStreamIds(20));
  });

  it("different seed -> different emission order", () => {
    const sources = makeSources();
    const a = new EventSimulator(sources, { onEmit: () => undefined }, { seed: 1 });
    const b = new EventSimulator(sources, { onEmit: () => undefined }, { seed: 2 });
    expect(a.orderStreamIds(20)).not.toEqual(b.orderStreamIds(20));
  });

  it("lcg is reproducible", () => {
    const a = [lcg(7)(), lcg(7)(), lcg(7)()];
    const b = [lcg(7)(), lcg(7)(), lcg(7)()];
    expect(a).toEqual(b);
  });

  it("tick() with fixed t0 produces fixed timestamps", () => {
    const sim = new EventSimulator(makeSources(), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    sim.tick();
    const first = sim.emittedEvents()[0];
    expect(first?.event.occurred_at).toBe(T0);
    expect(first?.emittedAt).toBe(T0 + 0.001);
  });
});

describe("per-platform rate/burst/batch/jitter/out-of-order", () => {
  it("burst batchSize emits batchSize events per round", () => {
    const sim = new EventSimulator(makeSources([{ batchSize: 3 }]), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    const emitted = sim.tick();
    expect(emitted.filter((e) => e.source.streamId === "tiktok-live-1")).toHaveLength(3);
  });

  it("ratePerSecond spaces occurred_at per source", () => {
    const sim = new EventSimulator(makeSources([{ ratePerSecond: 2 }]), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    sim.emitSource("tiktok-live-1");
    sim.emitSource("tiktok-live-1");
    const times = collect(sim).map((e) => e.occurredAt);
    expect(times[1]! - times[0]!).toBe(0.5);
  });

  it("jitterProbability delays some events (fewer than burst count)", () => {
    const sim = new EventSimulator(makeSources([{ jitterProbability: 0.8, batchSize: 10 }]), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    const emitted = sim.tick().filter((e) => e.source.streamId === "tiktok-live-1");
    expect(emitted.length).toBeLessThan(10);
  });

  it("outOfOrder swaps adjacent events", () => {
    // Seed 42: the first outOfOrder draw is odd (normal path); the second
    // draw is even, so the second tiktok emission swaps index 2 before 1.
    const sim = new EventSimulator(makeSources([{ outOfOrder: true }]), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    sim.emitSource("tiktok-live-1");
    sim.emitSource("tiktok-live-1");
    const ids = collect(sim).map((e) => e.eventId);
    expect(ids[0]).toBe("sim-tiktok-live-1-0");
    expect(ids[1]).toBe("sim-tiktok-live-1-2");
    expect(ids[2]).toBe("sim-tiktok-live-1-1");
  });
});

describe("retry, malformed, outage", () => {
  it("retry reuses the SAME event_id", () => {
    const sim = new EventSimulator(makeSources(), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    sim.emitSource("tiktok-live-1");
    const before = collect(sim);
    sim.retry("tiktok-live-1");
    const after = collect(sim);
    expect(after[1]?.eventId).toBe(before[0]?.eventId);
    expect(after[1]?.text).toBe(before[0]?.text);
  });

  it("retryLast retries the most recent emission", () => {
    const sim = new EventSimulator(makeSources(), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    sim.tick();
    const last = sim.emittedEvents()[sim.emittedEvents().length - 1];
    const retried = sim.retryLast();
    expect(retried?.emitted.event.event_id).toBe(last?.event.event_id);
  });

  it("malformed emission produces an event without payload.text", () => {
    const sim = new EventSimulator(makeSources(), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    sim.emitMalformedOnce("shopee-live-1");
    const event = sim.emittedEvents().find((e) => e.source.streamId === "shopee-live-1");
    expect(event?.source.text).toBeUndefined();
    expect(event?.event.payload).toEqual({});
    expect(event?.malformed).toBe(true);
  });

  it("outage pauses a source and recovery resumes it", () => {
    const sim = new EventSimulator(makeSources(), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    sim.tick();
    const before = collect(sim).filter((e) => e.streamId === "shopee-live-1").length;
    sim.setPaused("shopee-live-1", true);
    sim.tick();
    sim.tick();
    const during = collect(sim).filter((e) => e.streamId === "shopee-live-1").length;
    expect(during).toBe(before);
    sim.setPaused("shopee-live-1", false);
    sim.tick();
    const after = collect(sim).filter((e) => e.streamId === "shopee-live-1").length;
    expect(after).toBeGreaterThan(before);
  });

  it("sources stop emitting when fixtures are exhausted", () => {
    const sim = new EventSimulator(makeSources(), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    for (let i = 0; i < 120; i++) sim.tick();
    expect(sim.count).toBe(4 * 100); // 4 sources x 100 fixtures
    sim.tick();
    expect(sim.count).toBe(4 * 100);
  });
});

describe("canonical /events request body", () => {
  it("eventsRequestBody produces the EXACT JSON shape the backend expects", () => {
    const sim = new EventSimulator(makeSources(), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    sim.emitSource("facebook-live-1");
    const event = sim.emittedEvents()[0];
    const body = eventsRequestBody(sim.emittedEvents().map((e) => e.event));
    expect(JSON.parse(JSON.stringify(body))).toEqual({
      events: [
        {
          event_id: event?.event.event_id,
          platform: "facebook",
          source_stream_id: "facebook-live-1",
          occurred_at: T0,
          type: "viewer.comment",
          viewer: { viewer_id: "v-201", display_name: "viewer_v-201" },
          payload: { text: expect.any(String) },
        },
      ],
    });
  });

  it("never exceeds the backend 100-event batch bound", () => {
    const sim = new EventSimulator(makeSources(), { onEmit: () => undefined }, { seed: 42, t0: T0 });
    sim.tick();
    const body = eventsRequestBody(sim.emittedEvents().map((e) => e.event));
    expect(body.events.length).toBeLessThanOrEqual(MAX_EVENTS_PER_REQUEST);
  });
});

describe("validateSimulatorInput", () => {
  it("reports missing categories", () => {
    expect(validateSimulatorInput([]).length).toBeGreaterThan(0);
  });

  it("passes with full category coverage", () => {
    const { viewer_messages } = loadFixtures();
    expect(validateSimulatorInput(viewer_messages)).toEqual([]);
  });
});

describe("default multi-platform source set", () => {
  it("covers all four platforms with distinct streams and viewer pools", () => {
    const sources = defaultSources(loadFixtures().viewer_messages);
    expect(sources.map((s) => s.platform)).toEqual(SOURCE_PLATFORMS);
    expect(new Set(sources.map((s) => s.streamId)).size).toBe(4);
    for (const s of sources) expect(s.viewerIds.length).toBeGreaterThan(0);
  });
});

describe("SE replay fixtures (16.7)", () => {
  it("replayRequestBody serializes the exact canonical body", () => {
    const body = replayRequestBody("retry-same-event-id", T0);
    expect(JSON.parse(JSON.stringify(body))).toEqual({
      events: [
        {
          event_id: "ev-2001",
          platform: "shopee",
          source_stream_id: "shopee-live-1",
          occurred_at: T0 + 20.0,
          type: "viewer.comment",
          viewer: { viewer_id: "v-201", display_name: "Lan" },
          payload: { text: "Serum vitamin C dùng ban ngày hay ban đêm?" },
        },
        {
          event_id: "ev-2002",
          platform: "shopee",
          source_stream_id: "shopee-live-1",
          occurred_at: T0 + 20.5,
          type: "viewer.comment",
          viewer: { viewer_id: "v-201", display_name: "Lan" },
          payload: { text: "Serum vitamin C dùng ban ngày hay ban đêm?" },
        },
        {
          event_id: "ev-2003",
          platform: "shopee",
          source_stream_id: "shopee-live-1",
          occurred_at: T0 + 21.0,
          type: "viewer.comment",
          viewer: { viewer_id: "v-202", display_name: "Tuấn" },
          payload: { text: "Kem này có phù hợp da dầu không?" },
        },
      ],
    });
  });

  it("malformed replay events carry an empty payload", () => {
    const body = replayRequestBody("malformed", T0);
    const malformed = body.events.find((e) => e.event_id === "ev-4002");
    expect(malformed?.payload).toEqual({});
  });

  it("every scenario has unique event_ids and explicit timestamps", () => {
    for (const scenario of SE_REPLAY_SCENARIOS) {
      const ids = scenario.events.map((e) => e.event_id);
      expect(new Set(ids).size).toBe(ids.length);
      for (const e of scenario.events) expect(typeof e.offset).toBe("number");
    }
  });
});
