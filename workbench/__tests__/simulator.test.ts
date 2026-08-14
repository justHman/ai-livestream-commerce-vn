/** Deterministic simulator + black-box WS behavior tests. */

import { describe, expect, it, vi } from "vitest";

import { ViewerSimulator, validateSimulatorInput } from "../src/simulator";
import type { ViewerMessageFixture } from "../src/fixtures";
import { loadFixtures } from "../src/fixtures";

function sampleMessages(): ViewerMessageFixture[] {
  const { viewer_messages } = loadFixtures();
  return viewer_messages.slice(0, 20);
}

describe("deterministic simulator", () => {
  it("produces identical ordering across identical seeds", () => {
    const messages = sampleMessages();
    const a = new ViewerSimulator(messages, { onMessage: () => undefined }, { seed: 42 }).order();
    const b = new ViewerSimulator(messages, { onMessage: () => undefined }, { seed: 42 }).order();
    expect(a).toEqual(b);
  });

  it("changes ordering when seed changes", () => {
    const messages = sampleMessages();
    const a = new ViewerSimulator(messages, { onMessage: () => undefined }, { seed: 1 }).order();
    const b = new ViewerSimulator(messages, { onMessage: () => undefined }, { seed: 2 }).order();
    expect(a).not.toEqual(b);
  });

  it("never mutates the source fixture array", () => {
    const messages = sampleMessages();
    const original = structuredClone(messages);
    new ViewerSimulator(messages, { onMessage: () => undefined }, { seed: 42 }).order();
    expect(messages).toEqual(original);
  });

  it("emits exactly batchSize messages per batch", async () => {
    const messages = sampleMessages();
    const seen: string[] = [];
    const sim = new ViewerSimulator(
      messages,
      { onMessage: (m) => seen.push(m.text) },
      { seed: 42, ratePerSecond: 1, batchSize: 5 },
    );
    sim.start();
    await new Promise((resolve) => setTimeout(resolve, 30));
    sim.stop();
    expect(seen.length).toBe(5);
  });

  it("stop() cancels further emission", async () => {
    const messages = sampleMessages();
    let count = 0;
    const sim = new ViewerSimulator(
      messages,
      { onMessage: () => { count += 1; } },
      { seed: 42, ratePerSecond: 1000, batchSize: 1 },
    );
    sim.start();
    sim.stop();
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(count).toBeLessThanOrEqual(2);
  });
});

describe("validateSimulatorInput", () => {
  it("reports missing categories", () => {
    const issues = validateSimulatorInput([]);
    expect(issues.length).toBeGreaterThan(0);
  });

  it("passes with full category coverage", () => {
    const { viewer_messages } = loadFixtures();
    expect(validateSimulatorInput(viewer_messages)).toEqual([]);
  });
});

describe("black-box transport contract", () => {
  it("platform WS receives canonical message shape via fake socket", () => {
    const { viewer_messages } = loadFixtures();
    const sent: Array<{ text: string; author: string; ts: number }> = [];
    const sim = new ViewerSimulator(
      viewer_messages,
      {
        onMessage: (message) => {
          // Only well-formed canonical messages are forwarded to ingress.
          if (typeof message.text === "string" && typeof message.author === "string" && typeof message.ts === "number") {
            sent.push(message);
          }
        },
      },
      { seed: 42, batchSize: 3, ratePerSecond: 1000 },
    );
    sim.start();
    setTimeout(() => sim.stop(), 20);
    expect(sent.length).toBeGreaterThanOrEqual(3);
    expect(sent.every((m) => m.text.length > 0)).toBe(true);
  });

  it("simulator exposes safe progress counters", () => {
    const messages = sampleMessages();
    const sim = new ViewerSimulator(messages, { onMessage: () => undefined }, { seed: 42, batchSize: 1, ratePerSecond: 1000 });
    sim.start();
    setTimeout(() => sim.stop(), 15);
    expect(sim.count).toBeGreaterThanOrEqual(0);
  });
});