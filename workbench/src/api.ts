/** Workbench API client — canonical v1 REST transport.
 *
 * Owns all fetch/WebSocket transport, safe response parsing, and error
 * normalization. No UI state, no DOM references.
 */

import type {
  AttachRequest,
  AttachResponse,
  AvatarsResponse,
  ConfigApplyResponse,
  EnginesResponse,
  HealthReadyResponse,
  IngestRequest,
  IngestResponse,
  LiveKitRoomResponse,
  SandboxVerifyRequest,
  SandboxVerifyResponse,
  SayRequest,
  StartRequest,
  StartResponse,
} from "./api_types";

export interface ApiDeps {
  backendUrl: string;
  viewerToken: () => string;
  adminToken: () => string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function createApi(deps: ApiDeps) {
  const base = () => deps.backendUrl.replace(/\/$/, "");

  function viewerHeaders(json = false): Record<string, string> {
    const t = deps.viewerToken();
    const h: Record<string, string> = {};
    if (t) h["Authorization"] = `Bearer ${t}`;
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  function adminHeaders(json = false): Record<string, string> {
    const t = deps.adminToken();
    const h: Record<string, string> = {};
    if (t) h["Authorization"] = `Bearer ${t}`;
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  async function requestJson<T>(
    path: string,
    options: RequestInit = {},
  ): Promise<T> {
    const response = await fetch(base() + path, options);
    const text = await response.text();
    let body: unknown = null;
    if (text) {
      try {
        body = JSON.parse(text) as T;
      } catch {
        throw new ApiError(
          response.status,
          `HTTP ${response.status}: phản hồi không phải JSON`,
        );
      }
    }
    if (!response.ok) {
      const detail = (body as Record<string, unknown>)?.detail;
      const message =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? (detail as Array<{ loc: string[]; msg: string }>)
                .map((i) => `${i.loc.join(".")}: ${i.msg}`)
                .join("; ")
            : `HTTP ${response.status}`;
      throw new ApiError(response.status, message, detail);
    }
    return body as T;
  }

  async function healthReady(): Promise<HealthReadyResponse> {
    return requestJson<HealthReadyResponse>("/api/v1/health/ready", {
      headers: viewerHeaders(),
    });
  }

  async function engines(): Promise<EnginesResponse> {
    return requestJson<EnginesResponse>("/api/v1/engines", {
      headers: adminHeaders(),
    });
  }

  async function avatars(): Promise<AvatarsResponse> {
    return requestJson<AvatarsResponse>("/api/v1/avatars", {
      headers: viewerHeaders(),
    });
  }

  async function startSession(
    req: StartRequest,
  ): Promise<StartResponse> {
    return requestJson<StartResponse>("/api/v1/sessions", {
      method: "POST",
      headers: viewerHeaders(true),
      body: JSON.stringify(req),
    });
  }

  async function say(
    sessionId: string,
    text: string,
    generate: boolean,
  ): Promise<{ ok: boolean; reply?: string }> {
    return requestJson<{ ok: boolean; reply?: string }>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/say`,
      {
        method: "POST",
        headers: viewerHeaders(true),
        body: JSON.stringify({ text, generate } as SayRequest),
      },
    );
  }

  async function interrupt(sessionId: string): Promise<{ ok: boolean }> {
    return requestJson<{ ok: boolean }>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/interrupt`,
      { method: "POST", headers: viewerHeaders() },
    );
  }

  async function stopSession(sessionId: string): Promise<{ ok: boolean }> {
    return requestJson<{ ok: boolean }>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/stop`,
      { method: "POST", headers: viewerHeaders() },
    );
  }

  async function attach(
    sessionId: string,
    req: Omit<AttachRequest, "session_id">,
  ): Promise<AttachResponse> {
    return requestJson<AttachResponse>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/attach`,
      {
        method: "POST",
        headers: viewerHeaders(true),
        body: JSON.stringify(req),
      },
    );
  }

  async function ingest(
    sessionId: string,
    req: Omit<IngestRequest, "session_id">,
  ): Promise<IngestResponse> {
    return requestJson<IngestResponse>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/ingest`,
      {
        method: "POST",
        headers: viewerHeaders(true),
        body: JSON.stringify(req),
      },
    );
  }

  async function chat(
    sessionId: string,
    text: string,
    author = "viewer",
    ts?: number,
  ): Promise<{ accepted: boolean; comment_id?: string }> {
    return requestJson<{ accepted: boolean; comment_id?: string }>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/chat`,
      {
        method: "POST",
        headers: viewerHeaders(true),
        body: JSON.stringify({ text, author, ts }),
      },
    );
  }

  async function applyRuntimeConfig(
    sessionId: string,
    config: Record<string, unknown>,
  ): Promise<ConfigApplyResponse> {
    return requestJson<ConfigApplyResponse>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/config`,
      {
        method: "PATCH",
        headers: viewerHeaders(true),
        body: JSON.stringify(config),
      },
    );
  }

  async function sandboxVerify(
    req: SandboxVerifyRequest,
  ): Promise<SandboxVerifyResponse> {
    return requestJson<SandboxVerifyResponse>("/api/v1/admin/sandbox/verify", {
      method: "POST",
      headers: adminHeaders(true),
      body: JSON.stringify(req),
    });
  }

  async function livekitRoom(
    sessionId: string,
  ): Promise<LiveKitRoomResponse> {
    return requestJson<LiveKitRoomResponse>(
      `/api/v1/media/livekit/room/${encodeURIComponent(sessionId)}`,
      { method: "POST", headers: viewerHeaders() },
    );
  }

  async function applyEngine(
    kind: "llm" | "tts",
    payload: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    return requestJson<Record<string, unknown>>(
      `/api/v1/engines/${kind}`,
      { method: "POST", headers: adminHeaders(true), body: JSON.stringify(payload) },
    );
  }

  async function previewTts(
    text: string,
    ttsId: string,
    voiceId: string,
  ): Promise<Response> {
    const response = await fetch(
      base() + "/api/v1/engines/tts/preview",
      {
        method: "POST",
        headers: adminHeaders(true),
        body: JSON.stringify({ text, tts_id: ttsId, voice_id: voiceId }),
      },
    );
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(
        response.status,
        (body as Record<string, unknown>)?.detail as string ??
          `HTTP ${response.status}`,
      );
    }
    return response;
  }

  return {
    healthReady,
    engines,
    avatars,
    startSession,
    say,
    interrupt,
    stopSession,
    attach,
    ingest,
    chat,
    applyRuntimeConfig,
    sandboxVerify,
    livekitRoom,
    applyEngine,
    previewTts,
    baseUrl: base,
    viewerHeaders,
    adminHeaders,
  };
}

export type Api = ReturnType<typeof createApi>;