# llm-tts — ECS Task family (not a single image)

Confirmed design: **2 containers / 1 ECS Task / 1 GPU** on `g6.xlarge`.

| Container | Dockerfile | Port | GPU util |
|-----------|------------|------|----------|
| llm | `services/llm/Dockerfile` | 8001 | 0.6 (declares GPU) |
| tts | `services/tts/Dockerfile` | 8002 | 0.25 (shares device) |

Build images separately:

```bash
docker build -f services/llm/Dockerfile -t imjusthman/ai-live-llm:dev .
docker build -f services/tts/Dockerfile -t imjusthman/ai-live-tts:dev .
```

Task definition wiring lives under `infra/modules/compute` (not this domain).
