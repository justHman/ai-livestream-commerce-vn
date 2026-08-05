/** WebSocket transport — canonical control/platform channels.
 *
 * - control: /api/v1/ws/control/{session_id}?token=... (lifecycle events)
 * - platform: /api/v1/ws/platform/{session_id}?token=... (viewer chat ingress)
 */

import type { ControlWsMessage, LifecycleEvent } from "./api_types";

export interface PlatformWsMessage {
  text: string;
  author?: string;
  ts?: number;
}

export interface WsDeps {
  backendUrl: string;
  getViewerToken: () => string;
  onLifecycle: (event: LifecycleEvent) => void;
  onStatus: (message: string, tone?: string) => void;
}

export function controlSocketUrl(backend: string, sessionId: string, token: string): string {
  const url = new URL(backend);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `/api/v1/ws/control/${encodeURIComponent(sessionId)}`;
  url.search = token ? `token=${encodeURIComponent(token)}` : "";
  return url.toString();
}

export function platformSocketUrl(backend: string, sessionId: string, token: string): string {
  const url = new URL(backend);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `/api/v1/ws/platform/${encodeURIComponent(sessionId)}`;
  url.search = token ? `token=${encodeURIComponent(token)}` : "";
  return url.toString();
}

export class ControlSocket {
  private ws: WebSocket | null = null;

  constructor(private deps: WsDeps) {}

  connect(sessionId: string) {
    this.disconnect();
    const url = controlSocketUrl(
      this.deps.backendUrl,
      sessionId,
      this.deps.getViewerToken(),
    );
    const ws = new WebSocket(url);
    this.ws = ws;
    ws.addEventListener("open", () => this.deps.onStatus("Control WebSocket đã kết nối.", "success"));
    ws.addEventListener("message", (event) => {
      try {
        const data = JSON.parse(event.data as string) as ControlWsMessage;
        if ("type" in data) this.deps.onLifecycle(data as LifecycleEvent);
      } catch {
        this.deps.onStatus("Không đọc được WebSocket event.", "danger");
      }
    });
    ws.addEventListener("error", () => this.deps.onStatus("Control WebSocket gặp lỗi kết nối.", "danger"));
    ws.addEventListener("close", () => this.deps.onStatus("Control WebSocket đã đóng.", "warning"));
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  get connected(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }
}

export class PlatformSocket {
  private ws: WebSocket | null = null;

  constructor(private deps: WsDeps) {}

  connect(sessionId: string) {
    this.disconnect();
    const url = platformSocketUrl(
      this.deps.backendUrl,
      sessionId,
      this.deps.getViewerToken(),
    );
    const ws = new WebSocket(url);
    this.ws = ws;
    ws.addEventListener("open", () => this.deps.onStatus("Platform WebSocket đã kết nối.", "success"));
    ws.addEventListener("message", (event) => {
      try {
        const data = JSON.parse(event.data as string) as { type: string; comment_id?: string; detail?: string };
        if (data.type === "platform.accepted") {
          this.deps.onStatus(`Viewer comment accepted: ${data.comment_id ?? ""}`, "success");
        } else if (data.type === "platform.stored") {
          this.deps.onStatus("Viewer comment stored pending.", "warning");
        } else if (data.type === "error") {
          this.deps.onStatus(`Platform WS error: ${data.detail ?? "unknown"}`, "danger");
        }
      } catch {
        this.deps.onStatus("Không đọc được platform event.", "danger");
      }
    });
    ws.addEventListener("error", () => this.deps.onStatus("Platform WebSocket gặp lỗi.", "danger"));
    ws.addEventListener("close", () => this.deps.onStatus("Platform WebSocket đã đóng.", "warning"));
  }

  send(message: PlatformWsMessage): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(message));
    return true;
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  get ready(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }
}