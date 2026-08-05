/** Sessions — session lifecycle actions on top of the API seam. */

import type { Api } from "./api";
import type { AttachRequest, StartRequest } from "./api_types";

export interface SessionDeps {
  api: Api;
  viewerToken: () => string;
  adminToken: () => string;
}

export async function startSession(deps: SessionDeps, req: StartRequest) {
  return deps.api.startSession(req);
}

export async function stopSession(deps: SessionDeps, sessionId: string) {
  return deps.api.stopSession(sessionId);
}

export async function say(deps: SessionDeps, sessionId: string, text: string, generate: boolean) {
  return deps.api.say(sessionId, text, generate);
}

export async function interrupt(deps: SessionDeps, sessionId: string) {
  return deps.api.interrupt(sessionId);
}

export async function attach(
  deps: SessionDeps,
  sessionId: string,
  req: Pick<AttachRequest, "shop_profile" | "products">,
) {
  return deps.api.attach(sessionId, req);
}

export async function ingest(
  deps: SessionDeps,
  sessionId: string,
  comments: Array<{ text: string; t?: number }>,
) {
  return deps.api.ingest(sessionId, { comments, viewer_count: 1010, msg_rate: 2.0 });
}

export async function applyRuntimeConfig(
  deps: SessionDeps,
  sessionId: string,
  config: Record<string, unknown>,
) {
  return deps.api.applyRuntimeConfig(sessionId, config);
}