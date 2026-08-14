/** Multi-platform SE adapter simulator (OpenSpec 16).
 *
 * Domain-pure: no DOM, no timers inside the testable core. Emits canonical
 * PlatformEvents (the exact /api/v1/sessions/{id}/events wire shape) from
 * per-platform sources — TikTok/Shopee/Facebook/YouTube — with deterministic
 * seeded scheduling, per-source rate/burst/batch/jitter/out-of-order,
 * identical-event-id retry, malformed emission, and outage/recovery.
 *
 * Timers live only in the `start()` wrapper (per-source rate timers);
 * everything else is manual-advance friendly for deterministic tests.
 */

import type { PlatformEvent, PlatformEventType } from "./api_types";
import type { ViewerMessageFixture } from "./fixtures";

export type SourcePlatform = "tiktok" | "shopee" | "facebook" | "youtube";

export const SOURCE_PLATFORMS: SourcePlatform[] = ["tiktok", "shopee", "facebook", "youtube"];

export interface SimSourceConfig {
  /** Per-second emission rate for the source (runtime scheduling). */
  ratePerSecond?: number;
  /** Emit this many events per emission round (burst). */
  batchSize?: number;
  /** Probability (0..1) of delaying an event to the following round (jitter). */
  jitterProbability?: number;
  /** Swap adjacent event order at each round when true (out-of-order). */
  outOfOrder?: boolean;
  /** Start paused (outage) until resume() is called. */
  paused?: boolean;
}

export interface SimSourceDefinition {
  platform: SourcePlatform;
  streamId: string;
  viewerIds: string[];
  messages: ViewerMessageFixture[];
  config: SimSourceConfig;
}

export interface EmittedEvent {
  /** Source view: platform / stream / viewer / text / occurred_at. */
  source: {
    platform: SourcePlatform;
    streamId: string;
    viewerId: string;
    displayName: string;
    text?: string;
    occurredAt: number;
  };
  /** Canonical event — the exact object included in the /events request. */
  event: PlatformEvent;
  /** True when the emitted event would be rejected by the backend contract. */
  malformed: boolean;
  /** True when this emission is a retry of a previously emitted event_id. */
  retry: boolean;
  /** Monotonic emission sequence, shared across all sources. */
  sequence: number;
  /** When the emission occurred, seconds since simulator epoch t0. */
  emittedAt: number;
}

export interface SimEmission {
  source: SimSourceDefinition;
  emitted: EmittedEvent;
}

export interface EventSimulatorOptions {
  seed?: number;
  /** Simulator epoch (epoch sec) — timestamps become deterministic in tests. */
  t0?: number;
}

/** Deterministic LCG — same seed -> identical sequence (mirrors ViewerSimulator). */
export function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s;
  };
}

/** Build the exact POST body for /api/v1/sessions/{id}/events from events. */
export function eventsRequestBody(events: PlatformEvent[]): { events: PlatformEvent[] } {
  return { events: events.map((e) => structuredClone(e)) };
}

/** 1..100 events per request is the backend contract bound. */
export const MAX_EVENTS_PER_REQUEST = 100;

export class EventSimulator {
  private seed: number;
  private next: () => number;
  private cursor = new Map<string, number>();
  private paused = new Map<string, boolean>();
  private malformed = new Map<string, boolean>();
  private emitted: EmittedEvent[] = [];
  private sequence = 0;
  private t0: number;
  private timers = new Map<string, ReturnType<typeof setTimeout>>();
  private cancelled = false;
  private generation = 0;

  constructor(
    public sources: SimSourceDefinition[],
    private callbacks: {
      onEmit: (emission: SimEmission) => void;
      onError?: (error: Error) => void;
      onTick?: (sentCount: number) => void;
    },
    options: EventSimulatorOptions = {},
  ) {
    this.seed = options.seed ?? 42;
    this.next = lcg(this.seed);
    this.t0 = options.t0 ?? 0;
    for (const source of sources) {
      this.paused.set(source.streamId, Boolean(source.config.paused));
    }
  }

  /** Deterministic emission order (streamId + fixture index) for a fresh
   * simulator with the same seed — pure function of sources + seed. */
  order(): Array<{ streamId: string; index: number }> {
    const plan: Array<{ streamId: string; index: number }> = [];
    const counters = new Map<string, number>();
    const rand = lcg(this.seed);
    for (;;) {
      const candidates = this.sources.filter((s) => (counters.get(s.streamId) ?? 0) < s.messages.length);
      if (!candidates.length) break;
      const source = candidates[rand() % candidates.length] ?? null;
      if (!source) break;
      const index = counters.get(source.streamId) ?? 0;
      plan.push({ streamId: source.streamId, index });
      counters.set(source.streamId, index + 1);
    }
    return plan;
  }

  /** Deterministic stream-id sequence for the first `count` emissions. */
  orderStreamIds(count: number): string[] {
    return this.order().slice(0, count).map((p) => p.streamId);
  }

  /** Simulated event-id for a source's event index: stable across runs. */
  eventIdFor(streamId: string, index: number): string {
    return `sim-${streamId}-${index}`;
  }

  /** Advance every non-paused source by one emission round (deterministic). */
  tick(): SimEmission[] {
    const emitted: SimEmission[] = [];
    for (const source of this.sources) {
      if (this.paused.get(source.streamId)) continue;
      for (const emission of this.emitRound(source)) emitted.push(emission);
    }
    this.callbacks.onTick?.(this.emitted.length);
    return emitted;
  }

  /** Emit one full batch round from a single named source (used by start()). */
  emitRound(source: SimSourceDefinition): SimEmission[] {
    const emitted: SimEmission[] = [];
    const batch = source.config.batchSize ?? 1;
    for (let i = 0; i < batch; i++) {
      const emission = this.emitOne(source);
      if (emission) emitted.push(emission);
    }
    return emitted;
  }

  /** Emit exactly one event from a named source (outage/retry/malformed UI). */
  emitSource(streamId: string): SimEmission | null {
    const source = this.sources.find((s) => s.streamId === streamId);
    return source ? this.emitOne(source) : null;
  }

  /** Retry the most recently emitted event of a source with the SAME event_id. */
  retry(streamId: string): SimEmission | null {
    const source = this.sources.find((s) => s.streamId === streamId);
    const last = [...this.emitted].reverse().find((e) => e.source.streamId === streamId);
    if (!source || !last) return null;
    const emission = this.recordEmission(source, structuredClone(last.event), { retry: true });
    return emission;
  }

  /** Retry the most recent emission across all sources (global UI control). */
  retryLast(): SimEmission | null {
    const last = this.emitted[this.emitted.length - 1];
    return last ? this.retry(last.source.streamId) : null;
  }

  /** Toggle source outage (paused) or recovery. */
  setPaused(streamId: string, paused: boolean): void {
    this.paused.set(streamId, paused);
  }

  /** Toggle malformed emission (missing payload.text) for a source. */
  setMalformed(streamId: string, malformed: boolean): void {
    this.malformed.set(streamId, malformed);
  }

  /** Emit one malformed event from a source without leaving the flag on. */
  emitMalformedOnce(streamId: string): SimEmission | null {
    const source = this.sources.find((s) => s.streamId === streamId);
    if (!source) return null;
    this.malformed.set(streamId, true);
    const emission = this.emitOne(source);
    this.malformed.set(streamId, false);
    return emission;
  }

  /** Events emitted so far, in emission order. */
  emittedEvents(): EmittedEvent[] {
    return [...this.emitted];
  }

  /** Start emitting on wall-clock per-source rate timers (runtime only;
   * tests use tick() or vi.useFakeTimers with start()). */
  start(): void {
    this.cancelled = false;
    this.generation += 1;
    const generation = this.generation;
    this.t0 = Date.now() / 1000;
    for (const source of this.sources) {
      const rate = source.config.ratePerSecond ?? 1;
      const intervalMs = Math.max(50, Math.round(1000 / rate));
      const loop = () => {
        if (this.cancelled || generation !== this.generation) return;
        this.emitRound(source);
        this.timers.set(source.streamId, setTimeout(loop, intervalMs));
      };
      loop();
    }
  }

  stop(): void {
    this.cancelled = true;
    this.generation += 1;
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
  }

  get running(): boolean {
    return !this.cancelled && this.timers.size > 0;
  }

  get count(): number {
    return this.emitted.length;
  }

  private emitOne(source: SimSourceDefinition): SimEmission | null {
    const index = this.cursor.get(source.streamId) ?? 0;
    if (index >= source.messages.length) return null;
    if ((source.config.jitterProbability ?? 0) > 0 && this.next() / 0xffffffff < (source.config.jitterProbability ?? 0)) {
      return null; // delayed — same index retried next round
    }
    if (source.config.outOfOrder && index + 1 < source.messages.length && this.next() % 2 === 0) {
      const a = this.buildEvent(source, index + 1);
      const b = this.buildEvent(source, index);
      this.cursor.set(source.streamId, index + 2);
      this.recordEmission(source, a, {});
      return this.recordEmission(source, b, {});
    }
    const event = this.buildEvent(source, index);
    this.cursor.set(source.streamId, index + 1);
    return this.recordEmission(source, event, {});
  }

  private buildEvent(source: SimSourceDefinition, index: number): PlatformEvent {
    const fixture = source.messages[index];
    const viewerId = source.viewerIds[index % source.viewerIds.length] ?? "viewer-0";
    const type: PlatformEventType = index % 5 === 4 ? "viewer.like" : "viewer.comment";
    const malformed = Boolean(this.malformed.get(source.streamId)) && type === "viewer.comment";
    const event: PlatformEvent = {
      event_id: this.eventIdFor(source.streamId, index),
      platform: source.platform,
      source_stream_id: source.streamId,
      occurred_at: this.t0 + index * (1 / (source.config.ratePerSecond ?? 1)),
      type,
      viewer: { viewer_id: viewerId, display_name: `viewer_${viewerId}` },
      payload: malformed ? {} : { text: fixture?.text ?? "" },
    };
    return event;
  }

  private recordEmission(
    source: SimSourceDefinition,
    event: PlatformEvent,
    extra: { malformed?: boolean; retry?: boolean },
  ): SimEmission | null {
    const text =
      typeof event.payload === "object" && event.payload !== null && "text" in event.payload
        ? String((event.payload as { text: string }).text)
        : undefined;
    const emitted: EmittedEvent = {
      source: {
        platform: source.platform,
        streamId: source.streamId,
        viewerId: event.viewer?.viewer_id ?? "unknown",
        displayName: event.viewer?.display_name ?? "unknown",
        text,
        occurredAt: event.occurred_at,
      },
      event,
      malformed: extra.malformed ?? (event.type === "viewer.comment" && !text),
      retry: extra.retry ?? false,
      sequence: this.sequence++,
      emittedAt: this.t0 + this.sequence * 0.001,
    };
    this.emitted.push(emitted);
    this.callbacks.onEmit({ source, emitted });
    return { source, emitted };
  }
}

/** Deterministic default source set: one stream per platform (OpenSpec 16.2). */
export function defaultSources(messages: ViewerMessageFixture[]): SimSourceDefinition[] {
  return SOURCE_PLATFORMS.map((platform, i) => ({
    platform,
    streamId: `${platform}-live-1`,
    viewerIds: Array.from({ length: 8 }, (_, v) => `v-${i * 100 + v + 1}`),
    messages,
    config: { ratePerSecond: 0.67, batchSize: 1, jitterProbability: 0, outOfOrder: false },
  }));
}
