/** Deterministic viewer simulator.
 *
 * Consumes validated categorized fixtures, emits canonical platform WS
 * messages, deterministic seed/order/rate/batch/cancellation. No backend
 * imports, no debug endpoints.
 */

import type { ViewerMessageFixture } from "./fixtures";
import { SHOP_PROFILE_CATEGORIES } from "./fixtures";

export interface SimulatorOptions {
  seed?: number;
  ratePerSecond?: number;
  batchSize?: number;
}

export interface SimulatorCallbacks {
  onMessage: (message: { text: string; author: string; ts: number }, batchIndex: number) => void;
  onError?: (error: Error) => void;
  onTick?: (sentCount: number) => void;
}

export class ViewerSimulator {
  private seed: number;
  private ratePerSecond: number;
  private batchSize: number;
  private cursor = 0;
  private generation = 0;
  private sentCount = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private cancelled = false;

  constructor(
    private messages: ViewerMessageFixture[],
    private callbacks: SimulatorCallbacks,
    options: SimulatorOptions = {},
  ) {
    this.seed = options.seed ?? 42;
    this.ratePerSecond = options.ratePerSecond ?? 0.67;
    this.batchSize = options.batchSize ?? 1;
  }

  /** Deterministic seed — allows reproducible batches. */
  order(): ViewerMessageFixture[] {
    // In-place deterministic Fisher-Yates via a seeded LCG, so ordering is
    // identical across runs with the same seed. Never mutates the source.
    const copy = [...this.messages];
    let s = this.seed >>> 0;
    const next = () => {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s;
    };
    for (let i = copy.length - 1; i > 0; i--) {
      const j = next() % (i + 1);
      const tmp = copy[i];
      const swp = copy[j];
      if (tmp === undefined || swp === undefined) continue;
      copy[i] = swp;
      copy[j] = tmp;
    }
    return copy;
  }

  /** Deterministic category mix: force at least one of each required category. */
  categoryCoverage(): ViewerMessageFixture[] {
    const required = ["normal_commerce", "product_question", "purchase_intent", "complaint", "spam", "off_topic", "safety"];
    const covered: ViewerMessageFixture[] = [];
    for (const category of required) {
      const found = this.messages.find((m) => m.category === category);
      if (found) covered.push(found);
    }
    return covered;
  }

  start(): void {
    this.cancelled = false;
    this.generation += 1;
    const generation = this.generation;
    const schedule = () => {
      if (this.cancelled || generation !== this.generation) return;
      this.emitBatch(generation);
      this.timer = setTimeout(schedule, Math.round((1000 / this.ratePerSecond) * this.batchSize));
    };
    schedule();
  }

  stop(): void {
    this.cancelled = true;
    this.generation += 1;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  get running(): boolean {
    return !this.cancelled && this.timer !== null;
  }

  get count(): number {
    return this.sentCount;
  }

  private emitBatch(generation: number): void {
    if (this.cancelled || generation !== this.generation) return;
    const ordered = this.order();
    const now = Date.now() / 1000;
    for (let i = 0; i < this.batchSize; i++) {
      const fixture = ordered[(this.cursor + i) % ordered.length];
      if (!fixture) continue;
      this.sentCount += 1;
      this.callbacks.onMessage(
        { text: fixture.text, author: `viewer_${this.cursor + i + 1}`, ts: now + i * 0.001 },
        this.cursor + i,
      );
    }
    this.cursor = (this.cursor + this.batchSize) % ordered.length;
    this.callbacks.onTick?.(this.sentCount);
  }
}

export function fixtureCategories(): string[] {
  return [...SHOP_PROFILE_CATEGORIES].sort();
}

export function validateSimulatorInput(messages: ViewerMessageFixture[]): string[] {
  const issues: string[] = [];
  if (!messages.length) issues.push("simulator: no fixtures");
  for (const category of SHOP_PROFILE_CATEGORIES) {
    if (!messages.some((m) => m.category === category)) {
      issues.push(`simulator: missing category ${category}`);
    }
  }
  return issues;
}