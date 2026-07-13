# Realtime & Self-Hosted Avatar Model Survey

## Selection summary

Near-term production demo:

```text
Portrait/head realtime     LLIA, Ditto TalkingHead, MuseTalk
Face/lip fallback          MuseTalk, LatentSync, Wav2Lip research-only
System shell               OpenAvatarChat
```

Half/full-body higher quality with chunk queue:

```text
EchoMimic v3
InfiniteTalk
HunyuanVideo-Avatar
LongCat-Video-Avatar 1.5
```

True realtime upper/full-body candidates:

```text
StreamAvatar      best public claim, but code/weights/license unclear
LiveAvatar        strong realtime claim, but multi-H800/H100-class requirement
```

Important distinction:

```text
Realtime direct: model generates frames at or above playback FPS.
Chunked realtime: model generates the next audio-aligned video windows while current windows play.
Offline/batch: generation is much slower than playback and only usable with long delay.
```

## AWS GPU guide

### Low-cost offline/testing

```text
Instance      g4dn.xlarge
GPU           T4 16GB
Use           Wav2Lip, SadTalker, LatentSync v1.5, light tests
Avoid         14B video diffusion, full-body, realtime diffusion
```

### Realtime face/lip, one stream

```text
Instance      g5.xlarge or g6.xlarge
GPU           A10G 24GB or L4 24GB
Use           MuseTalk, Ditto, LLIA portrait, LivePortrait, LatentSync v1.6
```

### Multi-stream face/lip

```text
Instance      g5.12xlarge / g6e.12xlarge / g6e.24xlarge
GPU           multi A10G or L40S 48GB
Pattern       one avatar worker per GPU
```

### Half/full-body quality, chunked/non-realtime

```text
Instance      g6e.xlarge/2xlarge/4xlarge
GPU           L40S 48GB
Use           EchoMimic v3, InfiniteTalk fp8, Hunyuan low-memory, LongCat experiments
```

### Heavy full-body / production batch

```text
Instance      p4de.24xlarge
GPU           8×A100 80GB
Use           HunyuanVideo-Avatar, InfiniteTalk long/full-body, high-quality batch
```

### Realtime diffusion avatar above 25 FPS

```text
Instance      p5.48xlarge
GPU           8×H100 80GB
Use           LiveAvatar/StreamAvatar-class systems if code is available
Reason        Their published claims use H800/H100-class multi-GPU setups
```

## Required candidates

### JoyStream / Joystream

Source: https://github.com/JoyStream/JoyStream

Finding: this URL is not an AI avatar model. It is a Joystream blockchain/media platform.

```text
Avatar capability     none
Mouth/eyes/head/body  none
Half/full-body        not applicable
Realtime avatar       no
Self-host             blockchain/media stack only
Model params/GB       none
AWS                   CPU instance only if running blockchain nodes; no GPU needed
Fit                   not suitable for avatar backend
```

### EchoMimic v3

Sources:

- https://antgroup.github.io/ai/echomimic_v3/
- https://github.com/antgroup/echomimic_v3

```text
Type              multi-modal human animation / talking body
Params            1.3B
Base              Wan2.1-Fun-V1.1-1.3B-InP
Body support      portrait + semi-body/talking body
Controls          audio, reference image, masks, prompts, landmarks/multimodal conditions
Motion            mouth, head, expression, semi-body/body motion
Hands             limited; not precise hand rig control
Realtime          no direct 20–30 FPS claim
Acceleration      8-step flash variant, TeaCache
Self-host         yes, repo + weights
License           Apache-2.0 noted by project/repo
VRAM              flash path around 12GB claimed; tested A100 80GB, RTX 4090D 24GB, V100 16GB
AWS minimum       g5.xlarge/g6.xlarge 24GB for light/flash trials
AWS better        g6e.xlarge/4xlarge L40S 48GB
AWS production    p4de or p5 for longer/high-quality runs
```

Fit:

```text
Good for self-host semi-body quality.
Use chunk queue for interactive demos.
Not ideal for direct strict 25 FPS live rendering.
```

### InfiniteTalk

Sources:

- https://meigen-ai.github.io/InfiniteTalk/
- https://github.com/MeiGen-AI/InfiniteTalk

```text
Type              audio-driven unlimited-length talking video
Params            base Wan2.1-I2V-14B-480P, 14B
Body support      full-body / holistic video
Controls          source image/video, target audio, key/context frames
Motion            lips, head, body posture, expressions
Multi-person      yes via weights/options
Realtime          no direct realtime claim
Acceleration      TeaCache, int8/fp8, LCM/FusionX/Lightx2v
Self-host         yes
License           Apache-2.0
AWS minimum       g6e 48GB for fp8/low-VRAM trials
AWS production    p4d/p4de/p5 for 720p/long/multi-person
```

Fit:

```text
Good for long-form full-body dubbing/video-to-video.
Use for chunked or delayed stream, not direct live 20–30 FPS.
```

### HunyuanVideo-Avatar

Sources:

- https://hunyuanvideo-avatar.com/
- https://github.com/Tencent-Hunyuan/HunyuanVideo-Avatar

```text
Type              audio-driven avatar video generation
Params            not clearly exposed on project page; HunyuanVideo-class heavy model
Body support      portrait, upper-body, full-body
Controls          avatar image, audio, emotion, multi-character audio assignment
Motion            mouth, expression, body/full-body, multi-character dialogue
Realtime          no
Observed latency  hosted/page examples around minutes, not frame-realtime
Self-host         yes, repo + weights + Docker
VRAM recommended  96GB
VRAM minimum      24GB but very slow; low-memory/TeaCache paths exist
AWS minimum       g5/g6 24GB for slow experiment only
AWS better        g6e 48GB
AWS production    p4de.24xlarge 8×A100 80GB or p5.48xlarge 8×H100
```

Fit:

```text
High-quality full-body/multi-character candidate.
Production live use requires chunking/buffering and high-end GPUs.
```

### LiveAvatar

Sources:

- https://liveavatar.github.io/
- https://github.com/Alibaba-Quark/LiveAvatar
- https://huggingface.co/Quark-Vision/Live-Avatar

```text
Type              streaming interactive avatar diffusion
Params            14B
Body support      project page emphasizes portrait/cartoon/long videos; full-body unclear
Controls          realtime audio / interactive conversation
Motion            mouth, face/head; body scope unclear
Realtime          yes
Claim             above 45 FPS
Hardware claim    5×H800
Optimizations     FP8, FlashAttention-3, cuDNN fused kernels, torch.compile, VAE cache, LoRA merge
Self-host         repo + HF linked
License           unclear from project page; verify before commercial use
AWS equivalent    p5.48xlarge 8×H100 closest
AWS fallback      p4de/p5 for experiments
Single GPU        unlikely to reproduce claimed realtime
```

Fit:

```text
Strong future high-end realtime candidate.
Not a cheap single-GPU self-host option.
```

### StreamAvatar

Source: https://streamavatar.github.io/

```text
Type              streaming interactive human avatar
Body support      full upper-body
Controls          reference image, user/agent audio streams, listening/speaking state
Motion            mouth, head, upper-body gestures/body motion
Realtime          yes
Claim             25 FPS at 928×704
Latency           around 1.20s
Hardware claim    2×H800
Self-host         code/weights not clearly public from project page
License           unknown
Params            unknown
AWS equivalent    p5.48xlarge for robust reproduction if code appears
```

Fit:

```text
Best public claim for realtime upper-body avatar.
Current blocker is code/weights/license availability.
```

### LLIA

Sources:

- https://meigen-ai.github.io/llia/
- https://github.com/MeiGen-AI/llia

```text
Type              low-latency interactive avatar diffusion
Body support      portrait/head
Controls          audio, reference portrait, class/state labels
States            speaking/listening/idle
Motion            mouth, expression, head/face
Realtime          yes
Claim             RTX 4090D: 78 FPS at 384×384, 45 FPS at 512×512
Initial latency   140–215 ms
Self-host         repo linked
License           unclear / likely research caveat; verify before commercial use
AWS minimum       g5.xlarge/g6.xlarge 24GB
AWS better        g6e.xlarge L40S 48GB for concurrency
```

Fit:

```text
Best near-term realtime portrait candidate.
Not half/full-body.
```

### LongCat-Video-Avatar 1.5

Source: https://meigen-ai.github.io/LongCat-Video-Avatar-1.5-Page/

```text
Type              audio-driven video avatar
Body support      upper-body + full-body
Controls          audio-driven; exact public details limited
Motion            mouth, expression, full-body, hand-object interactions, singing, multi-person turn-taking
Realtime          no clear realtime claim
Acceleration      faster 8-step generation
Self-host         repo/HF links present
License           page states generated content is academic-only; commercial use not permitted
Params            not disclosed on project page
AWS minimum       assume g6e 48GB for experiments
AWS production    p4de/p5
```

Fit:

```text
High-quality research candidate.
Commercial/product risk due license note.
```

## Additional candidates

### MuseTalk

Source: https://github.com/TMElyralab/MuseTalk

```text
Type              realtime lip-sync
Body support      face/mouth region only
Controls          input video/image + audio
Motion            mouth/lips only
Realtime          30 FPS+ on NVIDIA Tesla V100
Self-host         excellent
License           code MIT; trained model commercial usable per repo note
AWS minimum       g5.xlarge/g6.xlarge 24GB
```

Fit:

```text
Best MVP realtime lip-sync option.
Pair with static/half-body avatar renderer or composited body animation.
```

### Ditto TalkingHead

Source: https://github.com/antgroup/ditto-talkinghead

```text
Type              realtime talking head
Body support      portrait/head
Controls          source image + audio
Runtime           PyTorch / ONNX / TensorRT
Realtime          yes, online streaming pipeline
License           Apache-2.0
AWS minimum       g5/g6 24GB
AWS better        g6e L40S 48GB or p4d A100
```

Fit:

```text
Good production-ish talking-head fallback with TensorRT path.
```

### LatentSync

Source: https://github.com/bytedance/LatentSync

```text
Type              lip-sync diffusion
Body support      face-only
Controls          video + audio
Realtime          no clear realtime claim
VRAM              v1.5 around 8GB, v1.6 around 18GB
License           Apache-2.0
AWS               g4dn for v1.5, g5/g6 for v1.6
```

Fit:

```text
Good quality face/lip baseline, not primary realtime backend.
```

### Wav2Lip

Source: https://github.com/Rudrabha/Wav2Lip

```text
Type              classic lip-sync
Body support      mouth only
Controls          face video + speech audio
Realtime          not official
License           non-commercial/personal research due LRS2-trained model
AWS               g4dn/g5 enough
```

Fit:

```text
Research baseline only, avoid commercial production.
```

### SadTalker

Source: https://github.com/OpenTalker/SadTalker

```text
Type              talking head from image + audio
Body support      limited full-image/full-body mode, not true full-body motion
Realtime          no
License           Apache-2.0
AWS               g5/g6
```

Fit:

```text
Useful baseline, not realtime production target.
```

### LivePortrait

Source: https://github.com/KlingAIResearch/LivePortrait

```text
Type              portrait animation from driving video/motion template
Body support      head/portrait
Audio-driven      not by itself
Realtime          no official claim, but lightweight enough for interactive variants
AWS               g5/g6
```

Fit:

```text
Useful visual reenactment component, not full audio-to-video stack alone.
```

### OpenAvatarChat

Source: https://github.com/HumanAIGC-Engineering/OpenAvatarChat

```text
Type              full interactive avatar system
Components        ASR + LLM + TTS + avatar backend
Backends          LiteAvatar, LAM, MuseTalk, FlashHead
Realtime          system-level response around seconds, not a single model FPS benchmark
License           Apache-2.0
AWS               g5/g6 for demo, g6e for local LLM + avatar
```

Fit:

```text
Good architecture/reference shell for self-host stack.
```

### EchoMimic v2

Source: https://github.com/antgroup/echomimic_v2

```text
Type              audio-driven semi-body human animation
Body support      semi-body/upper torso + arms/head
Controls          reference image, audio, pose/video conditions
Realtime          no; accelerated examples still far below realtime
Self-host         yes
License           Apache-2.0
Hardware          tested A100 80GB, RTX4090D 24GB, V100 16GB
AWS               g5/g6 for experiments; p4d/p5 if speed matters
```

### Hallo3

Source: https://github.com/fudan-generative-vision/hallo3

```text
Type              portrait animation with video diffusion transformer
Body support      portrait/talking head
Base              CogVideo-5B I2V style stack
Realtime          no
License           repo MIT; base model license must be checked
Hardware          H100 tested
AWS               p5 H100 preferred
```

### AniPortrait

Source: https://github.com/X-LANCE/AniPortrait

```text
Type              pose/landmark-driven character animation
Body support      full-body pose animation possible; face reenactment
Audio-driven      not pure audio-to-full-body unless paired with audio-to-pose
Realtime          no
License           Apache-2.0
VRAM              at least 16GB noted
AWS               g5/g6 24GB enough for experiments
```

### AnimateAnyone

Source: https://github.com/HumanAIGC/AnimateAnyone

```text
Type              image-to-video character animation
Body support      character/full-body via pose-driven animation
Audio-driven      no, requires pose/control pipeline
Realtime          no
License           Apache-2.0
AWS               g6e/p4d class for practical experiments depending implementation
```

## Recommended roadmap

### MVP self-host path

```text
1. Mock-frame renderer for CI/frontend/backend tests.
2. MuseTalk or Ditto for realtime face/lip backend on g5/g6 24GB.
3. LLIA if license permits and repo works reliably.
4. Add chunk queue abstraction shared by mock and self-host renderers.
```

### Quality body-avatar path

```text
1. EchoMimic v3 for semi-body 1.3B manageable experiments.
2. InfiniteTalk for long/full-body dubbing with chunk queue.
3. HunyuanVideo-Avatar for full-body/multi-character quality on p4de/p5.
4. Track StreamAvatar/LiveAvatar release status for true realtime upper-body/high-end.
```

### Production decision rule

```text
If render_rtf < 1.0:
  direct realtime possible.

If 1.0 <= render_rtf <= 2.0:
  chunked realtime possible with lookahead buffer.

If 2.0 < render_rtf <= 5.0:
  delayed interactive demo only.

If render_rtf > 5.0:
  offline/batch only.
```

## Validation notes

Validated via public project/repo pages where available. GitHub authenticated API was unavailable during research, so some repo/license/weight availability details should be rechecked before procurement or commercial use.

Known unknowns:

```text
StreamAvatar      code/weights/license availability unclear
LiveAvatar        commercial license unclear from project page
LLIA              license/commercial terms need repo verification
LongCat 1.5       page indicates academic-only generated content
JoyStream         provided URL is not an avatar model
```
