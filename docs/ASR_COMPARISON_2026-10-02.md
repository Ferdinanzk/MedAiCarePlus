# Voxtral vs. our current ASR (SenseVoice-Small): comparison for the Reachy Mini medication robot

*Research date: 2026-10-02. Every number below was checked against its source. Numbers we measured ourselves are labelled "measured". Our own calculations are labelled "estimate".*

*Units: file sizes use GB = 10^9 bytes. GPU memory (VRAM) is given in MiB, as the sources publish it (1 MiB = 1.048576 MB). For comparison, the laptop GPU has 8,151 MiB in total and 7,899 MiB free.*

---

## 1. Bottom line

**Don't switch to Voxtral, in any form: realtime, offline, full precision or GGUF.**

- **Only one Voxtral model handles Chinese.** Mistral has four Voxtral repos on Hugging Face. Only one is a speech-to-text model that supports Chinese: [`Voxtral-Mini-4B-Realtime-2602`](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602).
  - The offline Mini 3B and Small 24B models [have no Chinese](https://huggingface.co/mistralai/Voxtral-Mini-3B-2507/raw/main/README.md).
  - The best Chinese Voxtral, Transcribe V2, is only available through Mistral's cloud API.
- **It can't run on the robot.**
  - The smallest usable file (Q4, about 2.5–3.1 GB) nearly fills the CM4's free RAM and free disk.
  - It is already slower than realtime on 8-core x86 laptop CPUs: [0.67–0.77× realtime](https://raw.githubusercontent.com/handy-computer/transcribe.cpp/main/docs/models/voxtral-realtime.md). Those were offline one-shot runs. Live streaming would be harder still.
  - Our estimate for the CM4 is about 10× slower than realtime.
- **On the laptop, a GGUF build could fit the 8 GB RTX 5060, but nobody has measured it on this card. VRAM is the limit, not speed.**
  - The official vLLM setup needs a GPU with at least 16 GB, so it is ruled out.
  - Q8_0 is not ruled out for short sessions or for transcribing files:
    - In a llama.cpp fork, Q8_0 ran in file mode on an RTX 5090 Laptop (also Blackwell) using [about 5.6 GB of VRAM](https://huggingface.co/acceldium/Voxtral-Mini-4B-Realtime-2602_GGUF): text model 3.5 + audio encoder 1.9 + cache 0.2 GB, at context length 2048. Counting compute buffers, the total is about 6.2 GB. It ran at RTF 0.06.
    - In audio.cpp, Q8_0 streaming of a short clip peaked at [6,495 MiB on an RTX 5090](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602/discussions/48).
  - Peak streaming VRAM depends on the GPU and on how long the session runs. audio.cpp's validation GPU (not named) reached [8,972 MiB](https://huggingface.co/mistral-experimental/AudioCPP-Voxtral-Mini-4B-Realtime-2602-GGUF/raw/main/README.md), and a 30-minute stream on an RTX 5090 reached 8,990 MiB. **Long Q8_0 streaming sessions will exceed 8 GB.** Q4 should use less, but its VRAM has not been published.
  - Speed is unlikely to be the problem. Our estimate is about 7 ms (Q4) to 9–10 ms (Q8) per decoder step, against an 80 ms budget per step.
- **Its Mandarin is weak.** In Mistral's own paper, at the recommended 480 ms delay it scores [Common Voice zh CER 32.92%, against Whisper's 14.88%](https://arxiv.org/html/2602.11298v3).
  - Comparing across papers is rough and is not a direct test. Even so, it is very likely worse than SenseVoice-Small on Mandarin.
  - It has no language hint.
  - When the speaker switches language, it sometimes translates instead of transcribing.
  - Nothing documents whether it outputs Traditional Chinese.
- **Moving speech recognition to the laptop also breaks our consent wording** ("turned into text on the robot itself").

**What to do instead:**
1. Fix the audio input first (section 6).
2. Record a real test set from the robot's microphone.
3. If a better model is still needed, test Mandarin-focused models rather than Voxtral. The best candidates are Qwen3-ASR-0.6B ([Common Voice zh-TW CER 5.59](https://huggingface.co/Qwen/Qwen3-ASR-0.6B)) and Fun-ASR-Nano. Both have sherpa-onnx int8 exports of about 1 GB.

---

## 2. Comparison table

| Option | Params | Download | Chinese? | Streaming? | Runtime | RAM needed | GPU / VRAM needed | Runs on robot (CM4, 4 GB)? | Runs on laptop (RTX 5060, 8 GB)? | Expected speed (see the note on speed figures below) | License |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **SenseVoice-Small int8 (current)** | 234M | [239 MB](https://huggingface.co/api/models/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/tree/main) + silero VAD | Yes: zh, yue, en, ja, ko; explicit `language` option | No (VAD cuts, then decodes) | sherpa-onnx 1.13.8 | **363 MB peak (measured)** | None | **Yes (measured)** | Yes, easily | **RTF 0.48 on 2 threads (measured)**: 4.8 s of speech in 2.34 s | [FunASR Model License v1.1](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE) |
| **Voxtral Mini 4B Realtime 2602, BF16** | 4.43B | [8.86 GB (only one copy needed; the full repo is 17.7 GB)](https://huggingface.co/api/models/mistralai/Voxtral-Mini-4B-Realtime-2602/tree/main) | Yes (13 languages incl. zh); no language hint; script not documented | **Yes**, delay 80–1200 ms in 80 ms steps, or 2400 ms | vLLM ≥0.20 (Linux/WSL2), Transformers ≥5.2 | Not published | **≥16 GB GPU** ([card](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602/raw/main/README.md)) | No | **No** (the weights alone exceed 8 GB) | n/a on our hardware | Apache-2.0 |
| Voxtral Mini 3B 2507 (offline) | 4.68B | 9.35 GB | **No** (8 languages, no zh) | No | vLLM, Transformers, llama.cpp | — | ~9.5 GB | No | No (and no Chinese) | — | Apache-2.0 |
| Voxtral Small 24B 2507 (offline) | 24.26B | 48.5 GB | **No** | No | vLLM, Transformers | — | ~55 GB | No | No | — | Apache-2.0 |
| Voxtral Mini Transcribe V2 | n/a | **API only, no weights** | Yes ([FLEURS zh 7.30%](https://arxiv.org/html/2602.11298v3)) | No (batch) | Mistral cloud, $0.003/min | — | — | — | — | — | Proprietary API. **Audio leaves the home.** |
| **Realtime GGUF Q2_K** ([andrijdavid](https://huggingface.co/api/models/andrijdavid/Voxtral-Mini-4B-Realtime-2602-GGUF/tree/main)) | 4.43B | 1.47 GB | Yes (quality at Q2 not published) | Yes | voxtral.cpp only | Not published | Not published | No (CPU far too slow) | Unknown, untested | No data | Apache-2.0 |
| **Realtime GGUF Q4_K / Q4_K_M (single file)** | 4.43B | **2.52 GB** ([cstr](https://huggingface.co/cstr/voxtral-mini-4b-realtime-GGUF/raw/main/README.md)) · **2.83 GB** ([handy-computer](https://huggingface.co/handy-computer/Voxtral-Mini-4B-Realtime-2602-gguf/raw/main/README.md)) · 2.90 GB (andrijdavid) · **3.10 GB** ([audio.cpp, mistral-experimental](https://huggingface.co/api/models/mistral-experimental/AudioCPP-Voxtral-Mini-4B-Realtime-2602-GGUF/tree/main)) | Yes; Chinese accuracy at Q4 **not measured** (English shows no loss: LibriSpeech WER 2.08%) | Yes | Each file works only with its own runtime: audio.cpp, transcribe.cpp, CrispASR or voxtral.cpp. **Not llama.cpp, not Ollama.** | Not published (estimate 3–4.5 GB in total) | **Not published.** Estimate about 7 GB when streaming (Q8_0's streaming peak minus about 2 GB of weights); must be measured | No (fills about 83% of free disk; estimated RTF ~10) | **Maybe.** Measure first | x86 CPU, offline file runs: 0.22–0.77× realtime (slower than realtime). audio.cpp CUDA streaming: RTF 0.090 (GPU not named). RTX 5060 decoder step: **~7 ms against an 80 ms budget (estimate)** | Apache-2.0 |
| Realtime GGUF Q5_K_M / Q6_K | 4.43B | 3.28 / 3.66 GB (handy-computer) | Yes | Yes | transcribe.cpp | Not published | Not published | No | Unknown | No GPU data | Apache-2.0 |
| **Realtime GGUF Q8_0 (single file)** | 4.43B | **4.72–5.10 GB** (4.73 handy-computer / cstr, 5.10 audio.cpp) | Yes. **FLEURS zh CER 10.41%**, the same as BF16 | Yes | audio.cpp, transcribe.cpp, CrispASR, voxtral.cpp | Not published | audio.cpp, validation GPU (not named): **7,754 MiB offline / 8,972 MiB streaming**. RTX 5090: [6,495 MiB on a short stream, 8,990 MiB on a 30-minute stream](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602/discussions/48). Peak grows with session length | No | **Borderline. Long sessions exceed 8 GB.** Offline: 7,754 MiB leaves about 145 MiB of the 7,899 MiB free. Short streams are plausible (6,495 MiB on an RTX 5090) | GPU (audio.cpp): 14.7–16.7× realtime offline, 5.4× streaming. CPU (transcribe.cpp, offline one-shot runs): 0.56–0.61× realtime. RTX 5060 decoder step: **~9–10 ms against an 80 ms budget (estimate)** | Apache-2.0 |
| **Realtime GGUF, llama.cpp-fork split format (decoder + mmproj)** | 4.43B | [Shankara-A-S](https://huggingface.co/Shankara-A-S/voxtral-mini-4b-realtime-gguf) Q4_K_M **2.15 GB** + mmproj BF16 1.99 GB (4.14 GB) · chris0173 Q8_0 **3.65 GB** + mmproj F16 1.99 GB or Q8_0 1.06 GB · [acceldium](https://huggingface.co/acceldium/Voxtral-Mini-4B-Realtime-2602_GGUF) Q8_0 3.65 GB + mmproj F16 1.99 GB | Yes (same model); Chinese accuracy at these quants not measured | **No in these servers.** They take file uploads (`/v1/audio/transcriptions`), not live streams | **Only the unmerged llama.cpp forks**: didlawowo/llama.cpp `feat/voxtral-realtime-clean` (the head of closed PR #20638; named on the Shankara card) or acceldium's fork (closed PR #19698). The chris0173 README names no runtime. Not mainline llama.cpp | Not published | **acceldium Q8_0 on an RTX 5090 Laptop (Blackwell): ~5.6 GB** (text 3.5 + encoder 1.9 + cache 0.2, at context 2048); **about 6.2 GB with compute buffers**. Q4_K_M: no VRAM published (tested on an RTX 5080 with `-c 2048 --parallel 1`). Estimate for Q4_K_M by the same breakdown: about 4.8 GB | No | **Plausible for file mode.** About 6.2 GB of the 7.9 GB free, the only VRAM figure measured on a Blackwell laptop GPU. Unmeasured on the 5060; not live streaming | acceldium Q8_0, RTX 5090 Laptop: **RTF 0.06** (24 s of audio in 1.325 s, 95.5 tokens/s, file mode, 100 tokens generated) | Apache-2.0 (base model) |
| **Realtime GGUF F16 / BF16** | 4.43B | **8.87–8.88 GB** | Yes | Yes | same as Q8_0 | — | BF16: 10,909 / 12,616 MiB (offline / streaming) | No | No | — | Apache-2.0 |
| 3B 2507 GGUF Q4_K_M + mmproj ([ggml-org](https://huggingface.co/api/models/ggml-org/Voxtral-Mini-3B-2507-GGUF/tree/main)) | 4.68B | 2.47 + 0.72 GB | **No** | No (pads audio into 30 s chunks) | Mainline llama.cpp (b6014 and later) | ~3.8–4 GB (estimate) | — | No | Would fit, but **no Chinese** | CPU 0.53–0.71× realtime (Ryzen 4750U, offline) | Apache-2.0 |

Notes:
- **Speed figures.**
  - The transcribe.cpp CPU figures (0.56–0.77×) are offline one-shot runs on 11 s and 35 s clips. They use transcribe.cpp's default offline delay of 2.4 s, not live streaming at 480 ms. The CrispASR figure (0.22×) is also a single file run (an 11 s clip).
  - "Speedup over realtime" above 1× is faster than realtime. RTF below 1 is faster than realtime.
  - The RTX 5060 per-step times are our estimate: the time to read the decoder weights once per step at the laptop GPU's 384 GB/s memory bandwidth. They leave out the audio encoder and real-world inefficiency, so treat them as a lower bound. Even so, they sit far under the 80 ms budget. That is why VRAM, not speed, is the binding constraint on the laptop.
- Voxtral-4B-TTS is a text-to-speech model, not speech recognition. It is non-commercial (CC-BY-NC) and has no Chinese.
- **ONNX:** there is an ONNX q4 build of about 3.18 GB ([onnx-community](https://huggingface.co/api/models/onnx-community/Voxtral-Mini-4B-Realtime-2602-ONNX/tree/main)).
- **ExecuTorch is an untested Windows option that doesn't use GGUF.**
  - Mistral's own pre-exported file (`mistral-experimental/...-ExecuTorch`, `model-metal-int4.pte`, 4.42 GB) runs only on macOS with Apple M-series chips.
  - ExecuTorch itself has an official `cuda-windows` backend that can export int4 or int8 weights ([ExecuTorch Voxtral Realtime example](https://github.com/pytorch/executorch/blob/main/examples/models/voxtral_realtime/README.md)), plus streaming in 80 ms chunks.
  - A community CUDA-Windows build exists: `younghan-meta/Voxtral-Mini-4B-Realtime-2602-ExecuTorch-CUDA-Windows`.
  - LM Studio Bionic already runs Voxtral Realtime through ExecuTorch for voice input. It uses a 4.71 GB CUDA blob ([lmstudio #2432](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/2432)).
  - So this is a plausible route on 8 GB under Windows, but no VRAM or accuracy figures are published.
  - Its ARM CPU path (XNNPACK 8da4w) would still be limited by memory bandwidth on the CM4.
- **Don't mix GGUF files and runtimes.** A GGUF made for one runtime will not load in another. The split llama.cpp-fork files load neither in mainline llama.cpp nor in audio.cpp or transcribe.cpp.

---

## 3. Spec to run

**Our machines:**
- **Robot:** CM4, 4× A72, about 3.3 GB RAM available, 3.4 GB free disk, no GPU.
- **Laptop:** RTX 5060 Laptop GPU with 8,151 MiB VRAM (7,899 MiB free), CUDA 13.2, compute capability 12.0, 384 GB/s memory bandwidth. 15.2 GB RAM with **only about 2.7 GB free**. Measured locally on 2026-10-02 (the bandwidth figure is from the published spec).

| Option | Minimum | Recommended | Robot check | Laptop check |
|---|---|---|---|---|
| SenseVoice int8 (sherpa-onnx) | Any 4-core ARMv8 or x86 CPU, ~400 MB RAM, 250 MB disk | Same. A CPU with dotprod (A76) is much faster: published RTF 0.065 on 2 threads ([sherpa](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)) | **Pass** | Pass |
| Voxtral Realtime BF16 (vLLM) | GPU ≥16 GB; 8.86 GB disk; Linux or WSL2; vLLM ≥0.20; CUDA ≥12.8 for Blackwell | 24 GB GPU (one RTX 4090 stream at default settings [filled the GPU](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602/discussions/25)); lower `--max-model-len` from its default of 131072 | Fail | **Fail.** 8 GB VRAM, and WSL2 gets only 50% of host RAM by default |
| Voxtral Realtime GGUF Q8_0 (audio.cpp CUDA) | VRAM: 7,754 MiB offline; 6,495 MiB (RTX 5090) to 8,972 MiB (validation GPU) on short streams; 8,990 MiB on a 30-minute stream. 5.1 GB disk | ≥9,500 MiB (about 10 GB) of VRAM for long streaming sessions, or lower `stream_decode_cache_steps` | Fail | **Borderline.** Offline: about 145 MiB left of the free VRAM. Short streams plausible. Long streams fail |
| Voxtral Realtime GGUF Q8_0 (acceldium llama.cpp fork, CUDA) | About 5.6 GB VRAM (text 3.5 + encoder 1.9 + cache 0.2 GB), **about 6.2 GB with compute buffers**, measured on an RTX 5090 Laptop. 5.64 GB disk (3.65 GB decoder + 1.99 GB mmproj). The fork built with CUDA ≥12.8 (Blackwell). Run with `-ngl 99`, context 2048 | Same. File mode only (whole utterance in, text out); no live streaming | Fail | **Plausible in file mode** (about 6.2 of 7.9 GB free). Unmeasured on the 5060; the fork's Windows build is unverified |
| Voxtral Realtime GGUF Q4_K_M (Shankara llama.cpp fork) | VRAM not published (tested on an RTX 5080). Estimate about 4.8 GB by the acceldium breakdown with a 2.15 GB decoder. 4.14 GB disk. didlawowo fork; `-c 2048 --parallel 1` | Same; file upload to `/v1/audio/transcriptions` | Fail | **Unknown; probably fits.** Must be measured |
| Voxtral Realtime GGUF Q4_K (audio.cpp CUDA, Windows build) | VRAM not published (estimate about 7 GB when streaming). 3.1 GB disk. CUDA build with sm_120 (Blackwell) kernels | Lower `stream_decode_cache_steps` (default 1024, about 82 s of context) to limit VRAM growth | Fail | **Unknown; must be measured.** Free host RAM first |
| Voxtral Realtime, ExecuTorch `cuda-windows` int4 | No published VRAM or accuracy. The LM Studio CUDA blob is 4.71 GB | Untested | Fail | **Unknown; untested option** |
| Voxtral Realtime GGUF, CPU only | No published configuration runs in realtime | Apple M4 Max CPU manages 2.2–2.7× realtime (offline) | **Fail** (no ARM data; estimated RTF ~10) | Ryzen 9 8940HX unmeasured. The older Ryzen 4750U reached only 0.67–0.77× (offline one-shot runs at 2.4 s delay) |

**Expected GPU speed on the RTX 5060 Laptop (estimate, not measured).** At 384 GB/s, reading the decoder weights once per 80 ms step takes about 7 ms for Q4 (~2.5 GB) and about 9–10 ms for Q8 (~3.5 GB of text model). The audio encoder is not included, and audio.cpp notes the encoder is limited by compute rather than bandwidth when streaming. Even with a large margin for that, the budget looks comfortable. **On this laptop the binding constraint is VRAM (and the ~2.7 GB of free host RAM), not decoding speed.** It still has to be measured.

**What "realtime" means for Voxtral.** The decoder must finish one step every 80 ms, even during silence. [If it falls behind, it never catches up](https://raw.githubusercontent.com/0xShug0/audio.cpp/main/docs/models/voxtral_realtime.md).

---

## 4. Accuracy: Mandarin, Taiwan accent, noise, code-switching

**Chinese error rates (CER, lower is better) from [Mistral's paper](https://arxiv.org/html/2602.11298v3):**

| | FLEURS zh | Common Voice zh |
|---|---|---|
| Voxtral Realtime, 240 ms delay | 13.84 | 43.93 |
| **Voxtral Realtime, 480 ms delay (recommended)** | **10.45** | **32.92** |
| Voxtral Realtime, 960 ms delay | 8.99 | 21.51 |
| Voxtral Realtime, 2400 ms delay | 8.48 | 17.00 |
| Whisper (large-v3) | 7.94 | 14.88 |
| Transcribe V2 (API only) | 7.30 | 9.04 |

**SenseVoice-Small, from [its own paper](https://arxiv.org/html/2407.04051):**
- Common Voice zh-CN: 10.78, against Whisper-L-V3's 12.55.
- WenetSpeech meeting: 7.44, against Whisper-L-V3's 18.87.

**How the two compare.**
- Each paper normalizes text differently, so numbers from different papers can't be compared directly. Example: Whisper scores 7.94 on FLEURS zh in Mistral's paper but [4.09 in the Qwen3-ASR paper](https://arxiv.org/html/2601.21337).
- Still, the pattern is consistent. SenseVoice beats Whisper on Mandarin, and Whisper beats Voxtral Realtime on Mandarin.
- No head-to-head test of Voxtral against SenseVoice exists.

| Topic | Voxtral Realtime | SenseVoice-Small |
|---|---|---|
| Taiwan accent / zh-TW | No data. FLEURS zh is mainland Simplified (`cmn_hans_cn`) | No data |
| Traditional characters | Not documented | Outputs Simplified, so it needs OpenCC `s2twp` |
| Far-field / noise | No Mandarin data. English CHiME-4: 15.00% at 480 ms vs Whisper 10.88% (paper; the model card's figures look mislabeled) | AISHELL-4 22.52%, AliMeeting 38.75% CER ([2607.21075](https://arxiv.org/html/2607.21075)). Every model degrades badly far-field |
| Code-switching | **No language hint** ([#19](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602/discussions/19)); leading silence flipped detection to Arabic, French or Russian. **Translates on its own** when the language switches, more with noise; one user reported Taigi translated into Mandarin ([#21](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602/discussions/21)) | Explicit `language=zh/en/auto`. Code-switch accuracy not published |
| Punctuation | One user reports no punctuation in Chinese output ([#28](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602/discussions/28)) | `use_itn` adds punctuation |
| Effect of quantization | Q8_0 zh CER 10.41 (same as BF16). Q4 zh not measured | int8 is what we run |

**Most relevant published Mandarin alternatives (not Voxtral).**
- **Qwen3-ASR-0.6B:** Common Voice zh-TW 5.59, Elders&Kids 4.48, ExtremeNoise 17.88. The last two are Qwen's internal test sets.
- **Fun-ASR-Nano-2512:** industry far-field WER 5.79, on an internal test set ([card](https://huggingface.co/FunAudioLLM/Fun-ASR-Nano-2512)).

---

## 5. What switching would involve

| | Voxtral on the robot | Voxtral GGUF on the laptop, streaming (audio.cpp Q4_K) | Voxtral GGUF on the laptop, file mode (llama.cpp fork) |
|---|---|---|---|
| Runtime change | Not viable | Install audio.cpp's Windows CUDA build (or its Docker CUDA image). Run its live endpoint `POST /v1/audio/transcriptions/live`: raw PCM in, SSE transcript out. The robot streams microphone PCM over Wi-Fi. Download the **published q4_k file**; re-quantizing Q8 at load takes about 3 min. The client must buffer token ids, or Chinese prints as U+FFFD | Build the unmerged didlawowo or acceldium llama.cpp fork with CUDA ≥12.8. Run its server with `-c 2048 --parallel 1`. The robot keeps its VAD and uploads each utterance to `/v1/audio/transcriptions`. This matches today's VAD-then-transcribe design |
| Resources | — | About 7 GB of VRAM (estimate) taken from other GPU services. Host RAM is already tight, at about 2.7 GB free | Q8_0 about 5.6–6.2 GB of VRAM, measured on an RTX 5090 Laptop. Q4_K_M about 4.8 GB (estimate). Same host-RAM concern |
| Latency | Today: VAD end-of-speech plus decode (2.34 s for 4.8 s of speech, measured) | About 480 ms delay plus Wi-Fi plus server time. audio.cpp's validation run reports 180 ms server-side and 531 ms client-observed time to first token, on a different GPU. Faster than today **if** it keeps up with every 80 ms step | VAD end-of-speech plus upload plus decode. At the measured RTF of 0.06 on an RTX 5090 Laptop, 4.8 s of speech would take about 0.3 s. The 5060 is slower; not measured |
| Accuracy risk | — | Worse published Mandarin, no language hint, possible translation of mixed-language speech, Traditional output unknown | The same risks, plus an unmaintained fork |
| Privacy notice | Unchanged | **Must change.** "Turned into text on the robot itself" becomes untrue. "Audio never leaves your home" still holds on home Wi-Fi | **Must change** (same reason) |
| Mistral cloud API | — | **Breaks both promises.** Audio leaves the home ($0.006/min realtime, $0.003/min V2) | — |

The same privacy-wording change would apply to any model moved to the laptop, including SenseVoice itself.

---

## 6. Cheaper fixes for the garbled transcripts (no new model)

1. **Build a test set first.** Record 30–50 real far-field utterances from the robot's microphone and write the correct transcript for each. Measure CER before and after every change below. Any future model comparison also needs this set.
2. **Mic distance and noise.**
   - Keep the patient within about 1 m of the robot, facing it.
   - Pause or ignore speech recognition while the motors or fan are loud, for example during head movement.
   - Check that the input gain doesn't clip.
3. **Audio format.** Feed the model 16 kHz mono float audio. If the microphone delivers another sample rate or stereo, downmix and resample properly rather than by naive decimation. Check that VAD and ASR receive the same rate.
4. **Language setting.** The robot already forces `language="zh"` (`reachy_app/medcare_reachy/bridge/voice.py`). Compare `zh` against `auto` on the code-switched clips in the test set: `auto` may handle mixed English better, at some cost to pure Mandarin. Keep `use_itn=True`.
5. **VAD settings (silero).**
   - Add about 200–300 ms of pre-roll padding so the first syllable isn't cut.
   - Lengthen the minimum silence so pauses don't split sentences.
   - Raise the speech threshold so fan noise isn't segmented as speech.
   - Drop very short segments.
6. **Try the fp32 `model.onnx` (937 MB).**
   - Our A72 runs at RTF 0.48. The published A55 figure is faster: 0.260 on 2 threads.
   - One possible cause: the int8 kernels may run slowly on a CPU without dotprod.
   - This is unmeasured. Test speed, RAM and CER on the test set.
7. **Traditional output.** Add OpenCC `s2twp` after recognition.

---

## 7. Unconfirmed / could not verify

- **VRAM and speed on our own GPU.** No Voxtral GGUF has been measured on the RTX 5060 Laptop (8 GB): not the Q4 or Q8 audio.cpp files, and not the llama.cpp-fork files.
  - The only Blackwell-laptop figure (about 5.6–6.2 GB, RTF 0.06) comes from an RTX 5090 Laptop with 24 GB, in file mode, with only 100 tokens generated.
  - That card does not say whether "GB" means 10^9 or 2^30 bytes. Either way, it would fit in the 7,899 MiB free.
- **The ~7 ms / 9–10 ms per-step speeds on the RTX 5060** are a bandwidth-only estimate that leaves out the encoder.
- **The audio.cpp validation GPU is not named.** Its 8,972 MiB streaming peak may not carry over to other GPUs.
- **Blackwell support in the Windows builds.** Unverified for:
  - the prebuilt audio.cpp CUDA packages (sm_120 kernels)
  - the didlawowo and acceldium llama.cpp forks, which need a Windows build
  - ExecuTorch `cuda-windows` exports. There are also no VRAM or accuracy figures for those.
- **chris0173's README does not name a runtime.** Its split format matches the fork files, which is why we listed it with them.
- **Voxtral on the robot's CPU.** There is no measurement on a Raspberry Pi or Cortex-A72. RTF ~10 is a derived estimate.
- **The laptop itself.** Host-RAM use while loading the GGUF, and CPU-only speed on the Ryzen 9 8940HX, are both unmeasured.
- **Traditional or Simplified output.** Nothing official says which script Voxtral outputs. A Taiwanese blog claims Traditional support; it was not verified.
- **Accuracy on our kind of speech.** Neither Voxtral nor SenseVoice has published results for Taiwan-accented, elderly, far-field or code-switched Mandarin. Chinese accuracy of the Q4 quantizations is also unmeasured.
- **Details of Mistral's paper.** Which Common Voice Chinese locale the 32.92% figure used, and which Whisper variant the paper compared against (large-v3 is likely).
- **CrispASR's `-l` language flag.** Whether it actually constrains Voxtral Realtime's output language.
- **SenseVoice code-switch CER.** A community figure of 14.22% on ASCEND (n=60) was not fact-checked.
- **Our A72 int8 speed.** Why it trails the published A55 figure. The missing-dotprod explanation is a hypothesis.
- **A newer Voxtral Realtime.** Mistral staff said a "next version" might add language hints. As of 2026-10-02 only the 2602 version exists.