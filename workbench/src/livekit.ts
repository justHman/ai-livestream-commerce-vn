/** LiveKit — direct browser media join.
 *
 * Workbench joins the authorized LiveKit room and attaches tracks to the
 * local <video>/<audio> elements. Media never transits the backend HTTP plane.
 */

import { Room, RoomEvent, type RemoteTrack } from "livekit-client";

export interface LiveKitDeps {
  onStatus: (message: string, tone?: string) => void;
  videoEl: () => HTMLVideoElement | null;
}

export async function connectLiveKit(
  url: string,
  token: string,
  deps: LiveKitDeps,
): Promise<Room | null> {
  try {
    const room = new Room({ adaptiveStream: true, dynacast: true });
    room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
      const video = deps.videoEl();
      if (track.kind === "video" && video) track.attach(video);
      if (track.kind === "audio") {
        const audio = document.createElement("audio");
        audio.autoplay = true;
        audio.className = "sr-only";
        document.body.appendChild(audio);
        track.attach(audio);
      }
    });
    await room.connect(url, token);
    deps.onStatus("LiveKit đã kết nối.", "success");
    return room;
  } catch (error) {
    deps.onStatus(`LiveKit kết nối thất bại: ${safeMessage(error)}`, "danger");
    return null;
  }
}

export async function disconnectRoom(room: Room | null): Promise<void> {
  if (room) await room.disconnect();
}

function safeMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Lỗi không xác định";
}

export async function requestLiveKitJoin(
  api: { livekitRoom: (sessionId: string) => Promise<{ livekit_url: string; token: string }> },
  sessionId: string,
) {
  return api.livekitRoom(sessionId);
}