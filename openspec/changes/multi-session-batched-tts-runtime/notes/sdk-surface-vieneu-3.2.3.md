# VieNeu SDK surface (vieneu 3.2.3 — verified 2026-08-12)

Verified against the installed wheel in the tts_service venv
(`.venv/Lib/site-packages/vieneu/`). This is the pinned surface the
`VieNeuV3TurboProvider` adapter (tasks 6/7) must use.

## Entrypoint

- `vieneu.Vieneu` is a **function** (factory), signature `(mode='v3turbo', **kwargs)`.
  `mode='v3turbo'` returns `V3TurboVieNeuTTS` (module `vieneu/v3turbo.py`).
- Auto backend: `backend='pytorch'` on CUDA-capable hosts, `backend='onnx'` on CPU
  (v3turbo.py line ~80: OnnxV3LiteEngine vs VieNeuTTSv3Turbo).
- Backend exposed as `tts.backend` ("pytorch" | "onnx").

## Single synthesis (public API)

`tts.infer(text, ref_audio=None, voice=None, style='tu_nhien', denoise=True,
use_ref_codes=True, temperature=0.8, top_k=25, top_p=0.95, max_new_frames=300,
repetition_penalty=1.2, max_chars=256, silence_p=0.15, crossfade_p=0.0,
apply_watermark=True, batch_size=None, **kwargs) -> np.ndarray` (float32, 48 kHz).

- `voice` = preset voice name (str) or a full preset dict (containing speaker_emb/codes).
- `ref_audio` = path to reference WAV (zero-shot clone).
- Internally: `_resolve_ref` -> speaker_emb + ref_codes; text chunked
  (`normalize_to_chunks_v3_with_gaps`, max_chars), then `_infer_chunks` runs
  sequential engine infer per chunk, or the batch engine when GPU + batch_size > 1.
- `infer_stream` exists and yields per-chunk waveforms.

## Enrollment (voice cloning)

- `tts.encode_reference(ref_audio, denoise=True) -> (speaker_emb, ref_codes)` — the
  canonical one-time enrollment surface. `prepare_reference` trims to
  `_MAX_REF_SECONDS`, optionally denoises, returns `(192-d speaker_emb, ref_codes)`.
- `tts.add_voice(name, ref_audio, ...)` — process-global voice-name registry;
  Change T must NOT mutate this per request (task 6.4). Profiles persist encoded
  payloads instead.
- `tts.denoise(ref_audio, out_path=None, max_seconds=None) -> (wav, 44100)` — denoiser
  path for enrollment validation (may be absent on some backends: `denoiser` attr None).

## Batch engine (isolated internal surface — task 7)

- `vieneu.v3_turbo_serve.V3TurboBatchEngine` (module `vieneu/v3_turbo_serve/engine.py`).
- Constructed with `V3TurboBatchEngine(tts_instance)` (tts = the v3turbo instance's
  `.engine`, i.e. `VieNeuTTSv3Turbo`); lazily created via `tts._get_batch_engine()`,
  which returns **None on the ONNX/CPU backend** — the batch engine is PyTorch/CUDA only.
- `generate_batch(requests: List[dict], *, temperature=0.8, top_k=25, top_p=0.95,
  repetition_penalty=1.2, max_new_frames=300, use_cudagraph=False) -> List[np.ndarray]`.
- Per-request dict keys: `phonemes` (or `text` — phonemized internally when missing),
  `speaker_emb`, `ref_codes`, `style` (string style name, resolved per row via
  `_resolve_style_id`), `use_ref_codes` (default True).
- Batch-wide scalar params: temperature/top_k/top_p/repetition_penalty/max_new_frames
  — these define the provider `batch_key()` (task 7.3). Output order == request order.
- CUDA graph cache keyed by (B, temp, top_k, top_p); ignored when
  repetition_penalty != 1.0.
- Import path `vieneu.v3_turbo_serve` is the internal surface the import audit
  (task 7.11) must confine to `providers/vieneu_v3.py`.

## Presets / styles / cues

- Preset voices: `vieneu/assets/voices_v3_turbo.json` — 14 curated voices,
  default "Phạm Tuyên". Each: `{description, gender, region, style, speaker_emb
  (192-d list), codes (62-long list)}`. Loaded into `tts._preset_voices` at init.
- `tts.get_preset_voice(name) -> dict` — full payload incl. speaker_emb/codes.
- Styles (style_labels in model config): `tu_nhien`=16, `tin_tuc`=17, `doc_truyen`=18.
  Unknown style falls back to `default_style_token_id`.
- Expressive cues: inline `[cười]`/`[thở dài]`/`[hắng giọng]` (or English
  `[chuckle]`/`[sigh]`/`[clear throat]`, or `<|emotion_k|>`) phonemized to
  `<|emotion_1|>`/`<|emotion_2|>`/`<|emotion_3|>` by
  `vieneu_utils.phonemize_text.phonemize_text_with_emotions`. These are TEXT
  markers, not API parameters — cue support is text-level, capabilities should
  advertise `[cười]`, `[thở dài]`, `[hắng giọng]` as supported cue identifiers.

## Sampling / format

- Output: float32 mono waveform @ **48 kHz** (v3 Turbo). `audio_sample_rate` from
  model config. v2 = 24 kHz — stale v2 assumptions must not leak (task 13.2).
- Watermark applied by default (`apply_watermark=True`); provider may disable.

## Notes for adapter design

- Adapter must wrap: init (`Vieneu(mode='v3turbo', backbone_repo=..., device=...)`),
  backend detection (`tts.backend`), enrollment (`encode_reference`), single
  (`infer`), batch (`tts._get_batch_engine()` + `generate_batch`) — all inside
  `providers/vieneu_v3.py`.
- CPU/ONNX: `_get_batch_engine()` returns None -> provider must advertise
  `supports_native_batch=False` on onnx backend; scheduler effective batch size 1.
- `Vieneu` factory kwargs for device: `device="auto"` accepted (used by existing
  `engines/vieneu.py`).
