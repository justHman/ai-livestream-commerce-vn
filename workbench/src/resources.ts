/** Resources — avatar, LLM, TTS, voice discovery and selection. */

import type { Api } from "./api";
import type { EnginePreset, VoiceInfo } from "./api_types";

export interface ResourceDeps {
  api: Api;
  viewerToken: () => string;
  adminToken: () => string;
}

export async function discoverResources(
  deps: ResourceDeps,
): Promise<{
  engines: {
    llm: { id: string; name: string };
    tts: { id: string; name: string };
    available_llm_presets?: EnginePreset[];
    available_tts_presets?: EnginePreset[];
    voices?: VoiceInfo[];
  } | null;
  avatars: Array<{ id: string; label: string; ready?: boolean }>;
  llmId: string;
  ttsId: string;
  voiceId: string;
}> {
  const [enginesResult, avatarsResult] = await Promise.allSettled([
    deps.adminToken() ? deps.api.engines() : Promise.resolve(null),
    deps.viewerToken() ? deps.api.avatars() : Promise.resolve(null),
  ]);
  const engines = enginesResult.status === "fulfilled" ? enginesResult.value : null;
  const avatars =
    avatarsResult.status === "fulfilled" && avatarsResult.value !== null
      ? avatarsResult.value.avatars
      : [];
  const llmId = engines?.llm?.id || engines?.available_llm_presets?.[0]?.id || "";
  const ttsId = engines?.tts?.id || engines?.available_tts_presets?.[0]?.id || "";
  const voiceId =
    engines?.voices?.find((v) => v.active)?.id || engines?.voices?.[0]?.id || "default";
  return { engines, avatars, llmId, ttsId, voiceId };
}

export function enginePayload(preset: EnginePreset, kind: "llm" | "tts"): Record<string, unknown> {
  const known: Record<string, unknown> = {
    engine: preset.engine,
    model: kind === "tts" ? (preset.model || preset.weights_path || "") : (preset.model || ""),
    device: preset.device || "auto",
    sample_rate: preset.sample_rate || 24000,
  };
  return {
    ...known,
    extra: Object.fromEntries(
      Object.entries(preset).filter(
        ([key]) => !["id", "label", "ready", "capabilities", "engine", "model", "weights_path", "device", "sample_rate", "notes"].includes(key),
      ),
    ),
  };
}