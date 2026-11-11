# Models

PlexMind uses two local models: a language model in llama.cpp for
recommendations and subtitle translation, and Whisper for transcription.

## Language model (llama.cpp)

**PlexMind is model-agnostic.** It talks to llama.cpp's OpenAI-compatible API,
so any GGUF that llama.cpp can load and that follows a JSON schema works. Put
GGUF files in `LLAMA_CPP_MODELS_DIR` and choose one on the dashboard's **Model
Advisor** card; PlexMind remembers the choice. `LLAMA_CPP_MODEL_ALIAS` only sets
the model used before one is chosen. Choosing between models uses llama.cpp's
router mode (`--models-dir`), which the Compose file turns on. In router mode a
model's name is its file name without `.gguf`. If you run llama.cpp with a single
`--model` instead, set `LLAMA_CPP_MODEL_ALIAS` to its `--alias`.

### Which model

`setup.sh` picks one by GPU memory and downloads it with a checksum check
(`./setup.sh --model DIR` does only that):

| GPU memory | Model | Download |
|---|---|---|
| 12 GB | Gemma 4 12B Q5_K_M (default) | 8.4 GB |
| 10 GB | Qwen 3.5 9B Q5_K_M | 6.6 GB |
| 8 GB | Qwen3 8B Q4_K_M | 5.0 GB |

Gemma 4 12B is the default because, in blind Chinese and Spanish subtitle
comparisons on real episodes, it made about a third of the meaning errors of
Qwen 3.5 9B, at roughly 25% lower speed. It uses about 9 GB of GPU memory with
an 8K context.

### Try a model before switching: Model Trial

On the Model Advisor card, pick a model and click **Try on this GPU**. PlexMind
pauses translation, then runs the same work on that model and on the one in use:

- the subtitle batches translation really sends (same prompts, same JSON schema,
  same checks that decide whether a translated line is kept), in up to two of
  your `TARGET_LANGUAGES`
- one recommendation request built by the real recommender prompt

and shows, side by side: GPU memory with the model loaded, tokens per second,
unusable batches, rejected lines, idioms translated word for word, Simplified
characters in Traditional Chinese, and invented or malformed picks. A model that
uses more than 92% of GPU memory, generates fewer than 4 tokens per second, or
breaks too many replies **fails**, and cannot be selected. A trial takes a few
minutes; translation resumes afterwards.

A trial does not score naturalness. Read a few lines of real output before
switching.

### Settings

| Setting | Default | What it does |
|---|---|---|
| `LLAMA_ON_DEMAND` | `0` | `1` starts llama.cpp when needed and stops it after `LLAMA_IDLE_STOP_MINUTES` (15) idle, to free the GPU. While it is stopped it cannot answer the GPU-busy check, so add an always-running GPU container (e.g. Plex) to `GPU_PROBE_CONTAINERS` and `PLEXMIND_GPU_PROBE_CONTAINERS`, or scheduled jobs wait until llama.cpp runs. |
| `LLAMA_CPP_PARALLEL` | `1` | llama.cpp request slots. PlexMind sends one request at a time; each extra slot reserves more GPU memory. |
| `LLAMA_CPP_CTX_SIZE` | `8192` | Context size. Translation batches must fit in it. |

## Whisper (transcription)

Compose includes a digest-pinned `onerahmet/openai-whisper-asr-webservice`
container. PlexMind creates it, starts it only while transcribing, and stops it
afterwards. `setup.sh` picks the Whisper model by GPU memory (`turbo` at 10 GB
and up). Without an NVIDIA GPU, set:

```bash
WHISPER_DEVICE=cpu
WHISPER_MODEL=small
```

The dashboard lists only Whisper models already downloaded into the cache
mounted at `/whisper-cache`; transcription stays off while `WHISPER_MODEL` is
not among them.
