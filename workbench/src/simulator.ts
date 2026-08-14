/** Simulator input validation.
 *
 * The legacy ViewerSimulator (platform-WS emission) was replaced by the
 * canonical /events EventSimulator (see eventSimulator.ts); this file keeps
 * the shared fixture-category validation used before Auto Demo starts.
 */

import type { ViewerMessageFixture } from "./fixtures";
import { SHOP_PROFILE_CATEGORIES } from "./fixtures";

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
