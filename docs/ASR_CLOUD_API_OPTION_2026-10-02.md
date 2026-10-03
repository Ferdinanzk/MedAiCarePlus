# Would the Mistral Voxtral realtime API make Reachy reply faster?

## 1. Short answer

**Only a little, and it isn't worth it now.** Cloud speech recognition could have the text ready about **0.4 to 1.1 s sooner** per turn, with your current pause settings (estimate). That is a small part of a turn that takes about 6 to 19 s. Most of the time goes to the LLM reply: **2 to 15 s on the OpenRouter free tier**, measured in a real session.

Four other points matter as much as speed:

- **Your own waiting rules cost more than the recognition step.** Of the ~2.7 s before text is ready, 1.3 s is fixed waiting: 0.6 s of silence detection plus the 0.7 s hand-over. You can shorten those on the robot today, without the cloud.
- **The realtime model is weak on Chinese.** At the recommended 480 ms delay its Common Voice zh character error rate is [32.92%](https://arxiv.org/html/2602.11298) (wrong characters per 100). The batch model, Transcribe V2, is far better at 9.04%.
- **There is no language setting on the realtime API.** Only the batch API lets you say the audio is Chinese.
- **Either API breaks the signed consent notice**, which promises the audio never leaves the home.

There is no separate "realtime key". It is a normal Mistral API key used on the [realtime WebSocket](https://docs.mistral.ai/studio/audio/speech_to_text/realtime_transcription).

## 2. Time per turn (3 s utterance, seconds after the patient stops talking)

Each row is the time that step adds. The last row is the sum.

| Step | Today (SenseVoice on the robot) | Mistral realtime API (streaming) | Mistral batch Transcribe V2 (upload each utterance) |
|---|---|---|---|
| End-of-speech detection | 0.6 (measured) | 0.6 with today's setting. Could be 0.2 (the value [Pipecat](https://github.com/pipecat-ai/stt-benchmark/blob/main/README.md) uses), but slow elderly speakers may get cut off | 0.6 (it needs the whole clip) |
| Speech to text | 1.44 (0.48 × 3 s) | **0.33 to 0.97** from independent benchmarks. Coval median [332 ms, p95 650 ms](https://benchmarks.coval.ai/models/voxtral-mini-transcribe-realtime-2602) (EU endpoint). Pipecat median [525 ms, p95 973 ms, p99 1913 ms](https://github.com/pipecat-ai/stt-benchmark/blob/main/README.md) (English). Extra time from Taipei is not measured. The robot still has to detect end of speech itself and send a "flush" message ([Pipecat code](https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/services/mistral/stt.py)) | **0.6 to 1.0 (estimate).** The network alone is 0.38 to 0.45 s (our test today). On top come the upload (about 96 KB of raw audio per 3 s, estimate) and processing time, which nobody publishes ([confirmed](https://github.com/pipecat-ai/stt-benchmark)) |
| Turn hand-over wait | 0.7 | 0.7 (could be 0 if it overlaps with the API call) | 0.7 |
| **Transcript ready** | **~2.7** | **est. 1.6 to 2.3** with today's waits; **0.5 to 1.2** with 0.2 s detection and no hand-over wait | **est. 1.9 to 2.3** |
| LLM reply (OpenRouter free) | 2 to 15 (measured) | 2 to 15 | 2 to 15 |
| TTS first audio | 1.5 | 1.5 | 1.5 |
| **TOTAL** | **~6.2 to 19.2** | **est. 5.1 to 18.8** (4.0 to 17.7 with the shorter waits) | **est. 5.4 to 18.8** |

Notes:
- **The waits are a setting, not a recognition cost.** Today with 0.2 s detection and no hand-over wait would be about 0.2 + 1.44 ≈ **1.6 s** (estimate). That gets most of the gain with no cloud at all.
- **Opening the connection costs time once.** Opening the realtime connection from Taipei took **0.82 to 0.98 s** (our test today). Keep the connection open so you pay it only once.
- **The processing servers are probably not in Hong Kong.** The connection goes through Cloudflare's Hong Kong site (the earlier "San Francisco" lookup was wrong). But the reply comes 0.27 to 0.34 s after the connection is set up, which suggests the actual servers are farther away (inference). Mistral's docs say the API is ["served from EU data centers by default"](https://docs.mistral.ai/resources/known-limitations).
- **Batch has no speed promise.** Mistral's standard service is ["Seconds to minutes", best-effort](https://docs.mistral.ai/inference/priority-tier), with no uptime guarantee.

## 3. Trade-offs

| | Today (SenseVoice on the robot) | Realtime API | Batch Transcribe V2 API |
|---|---|---|---|
| **Speed (text ready)** | ~2.7 s (measured) | est. 1.6 to 2.3 s; slowest 1% about 1.9 s ([Pipecat](https://github.com/pipecat-ai/stt-benchmark/blob/main/README.md), [Coval](https://benchmarks.coval.ai/models/voxtral-mini-transcribe-realtime-2602)) | est. 1.9 to 2.3 s; not published |
| **Mandarin accuracy (% wrong characters)** | Not measured on our audio. Good on clean audio, garbled far-field (our observation) | Common Voice zh **32.92%** at 480 ms delay, 17.00% at 2400 ms. FLEURS zh 10.45% / 8.48% ([report](https://arxiv.org/html/2602.11298)) | FLEURS zh **7.30%**, Common Voice zh **9.04%**. For comparison, Whisper scores 7.94% / 14.88% |
| **Taiwan accent, far-field, noise** | — | No published data. Mistral says ["heavy background noise reduces accuracy"](https://docs.mistral.ai/resources/known-limitations) | Same. The launch post claims noise robustness, so Mistral's own pages conflict |
| **Can you tell it "this is Chinese"?** | Local model, under our control | **No.** It only [auto-detects](https://github.com/mistralai/client-python/blob/main/src/mistralai/client/models/realtimetranscriptionsessionupdatepayload.py). The open weights sometimes translate mixed Mandarin/English | **Yes:** `language="zh"` ["can boost accuracy"](https://docs.mistral.ai/api/endpoint/audio/transcriptions). It can't be combined with timestamps |
| **Traditional characters** | Unchanged | Not documented | Not documented. One user [reported](https://news.ycombinator.com/item?id=46886735) mixed Traditional/Simplified and spaces between characters. Plan a conversion step (e.g. OpenCC) and strip the spaces |
| **Robot CPU/RAM** | About 1.44 s of 2 cores busy per 3 s utterance | Frees the recognition work. Detection still runs locally, and the robot streams about 32 KB/s of raw audio (estimate) | Frees the recognition work. About 96 KB upload per 3 s clip (estimate) |
| **Cost, 270 min/month** (3 check-ins × 3 min speech, est.) | $0 | **$1.62** at [$0.006/min](https://mistral.ai/pricing/api/) | **$0.81** at $0.003/min |
| **Cost, 900 min/month** (30 min/day, est.) | $0 | **$5.40**. Possibly more if silence on an open connection is billed (not published) | **$2.70**. How short clips are rounded is not published; usage is counted in [whole seconds](https://docs.mistral.ai/admin/billing-usage/usage-limits) |
| **When the internet or Mistral fails** | Recognition still works (the LLM already needs internet) | The robot cannot understand anything. There was an "Audio speech to text degradation" on 2026-09-29: [40 min per StatusGator](https://statusgator.com/services/mistral-ai), [21 min per aiwatch](https://github.com/bentleypark/aiwatch/issues/1557). Rate limits return HTTP 429; the audio limit numbers are not published | Same, but each clip is a separate request, so retrying or falling back to the robot is easier |
| **Privacy, consent, Taiwan PDPA** | Matches the signed notice | **Breaks the notice.** Mistral keeps data [30 rolling days](https://legal.mistral.ai/terms/privacy-policy) by default. ZDR (Mistral's "zero data retention" option) is [not documented](https://docs.mistral.ai/admin/monitor-comply/zero-data-retention) for the realtime path. The global endpoint [does not commit to a processing location](https://docs.mistral.ai/inference/regional-inference) | **Breaks the notice.** 30 days by default. ZDR does cover `/v1/audio/transcriptions`, but only on [pay-as-you-go, on request, at Mistral's discretion](https://help.mistral.ai/en/articles/347612-can-i-activate-zero-data-retention-zdr). Send the audio in the request itself: ZDR excludes `/v1/files` |
| **Training on your data** | — | Free mode [may train on inputs by default](https://help.mistral.ai/en/articles/347617-do-you-use-my-user-data-to-train-your-artificial-intelligence-models). Opt out under Admin › Privacy › "Anonymous improvement data". The pay-as-you-go default is unclear, so check the switch | Same |
| **API key handling** | None | Don't put the long-lived key on the robot. Use [short-lived `rt_*` tokens](https://docs.mistral.ai/studio/audio/speech_to_text/realtime_transcription/client_auth.md) (about 900 s) issued by the home server | The key stays on the home server, which forwards the audio |

## 4. If you still want to try it

**Safest setup:** use the **batch Transcribe V2 API with `language="zh"`**, sent through the home server.

1. The robot detects end of speech and sends the clip (FLAC or WAV) over the LAN to the laptop. The laptop calls `POST /v1/audio/transcriptions` with the audio in the request itself, not through `/v1/files`. The key never leaves the laptop. The extra LAN hop has not been measured.
2. **Fallback:** if Mistral has not answered within a set limit (e.g. 2 s), or returns an error, the robot decodes the clip locally with SenseVoice as it does today.
3. Only consider **realtime** if batch is too slow. In that case the robot connects directly, using a short-lived `rt_*` token from the home server. Keep the connection open and send the flush message when your own detection says the patient stopped.
4. Account setup:
   - Use pay-as-you-go, not Free mode.
   - Request ZDR.
   - Turn off "Anonymous improvement data".
   - Choose `api.eu.mistral.ai` (+10% price, about +0.35 to 0.5 s per new connection) if you want the notice to name one region honestly. Whether the audio models are offered there is not published, so check the model list with your key.

**What must change in the consent notice.** This is a separate written or electronic consent. Taiwan PDPA [Art. 6](https://law.moj.gov.tw/ENG/LawClass/LawAll.aspx?pcode=I0050021) requires written consent for healthcare data. [Enforcement Rules](https://law.moj.gov.tw/ENG/LawClass/LawAll.aspx?pcode=I0050022) Art. 14 allows it to be electronic, Art. 15 requires it to be confirmed separately, and Art. 16 allows the notice to be read aloud in Mandarin.

| Art. 8 item | New wording must say |
|---|---|
| Withdraw the old promise | "Your voice recording is sent over the internet to Mistral AI (France) to be turned into text." Remove "audio never leaves your home" and "never stored". |
| Recipients and territory | Mistral AI and its [subprocessors](https://trust.mistral.ai/subprocessors), including Cloudflare. The path is Taiwan → Cloudflare in Hong Kong (observed) → EU, or wherever the chosen endpoint processes it. |
| Data types | An identifiable voice recording, plus what is said, which may include medicines and health. |
| Purpose | Speech-to-text only. |
| Retention | Up to 30 days for abuse checks, unless ZDR is approved and confirmed for the endpoint used. Until then, Mistral uses the audio for its own abuse monitoring as a separate data controller ([DPA §2.3](https://legal.mistral.ai/terms/data-processing-addendum)), so name it in that role. |
| Training | Switched off (after checking the switch). |
| Rights | View, copy, correct, stop and delete, and how to ask. Note that deletion at Mistral is limited. |
| If they refuse | Recognition stays on the robot. Consent can be withdrawn at any time. |

Also note:
- Mistral's DPA says its expected data includes no health or other "special category" data (["None"](https://legal.mistral.ai/terms/data-processing-addendum)).
- The current notice should already name OpenRouter and its countries, because health-related **text** already leaves the country.

**A/B test before switching.** Use only volunteer recordings, not patient audio.

1. **Test set:** 100 to 200 utterances recorded through the robot's microphone at real distances. Include fan noise and room echo, Taiwan-accented Mandarin, medicine names, mixed English, and about 20 silence or noise-only clips. Write the reference transcripts in Traditional characters.
2. **What to compare:**
   - SenseVoice (today's settings)
   - SenseVoice with 0.2 to 0.4 s detection and no hand-over wait
   - V2 with `language="zh"`
   - V2 with automatic language
   - Realtime at 480, 960 and 2400 ms delay. Read the server's default delay from the `session.created` message.
3. **What to measure:**
   - Character error rate after converting to Traditional and removing spaces and punctuation.
   - Medicine-name accuracy.
   - Clips where English came back translated, and text made up from silence.
   - Median and 95th-percentile time from end of speech to final text, run **from the robot in Taipei** over a persistent connection.
4. **When to switch:** the error rate is no worse than SenseVoice, the 95th-percentile time saves at least 1 s compared with tuned local settings, there were no failures during the test, **and** the new consent is signed.

**The bigger win is the LLM (2 to 15 s measured).** This research did not test LLM providers. These are cheap things to measure first:
1. For each turn, log when the first piece of the reply arrives versus when the full reply arrives. That shows whether the time is spent waiting in a free-tier queue or generating a long reply.
2. Request the reply as a stream and pass the first sentence to Matcha straight away. Matcha already speaks chunk by chunk (untested).
3. Cap the reply length.
4. Compare the same prompts on a paid route before deciding.

If a Mistral model writes the replies, note that its [Usage Policy](https://legal.mistral.ai/terms/usage-policy) bans "any form of health-related guidance" given without proper qualification.

## 5. What could not be verified

- How fast either API actually is from Taipei with a real key and Mandarin speech. Nobody publishes batch timings for short clips.
- Which delay values the realtime API accepts, its default, and whether a longer delay slows the final text after a flush.
- Whether the output is Traditional or Simplified, and whether the API translates mixed Mandarin/English the way the open weights do.
- How either model performs on far-field, echoey, noisy, Taiwan-accented elderly speech. The published scores are not for this kind of audio. Also unknown: which Common Voice zh variant (mainland or Taiwan) the 9.04% refers to.
- Whether flush reliably ends an utterance while keeping the connection open. This is inferred from Pipecat's code, not from Mistral's docs. Session length and idle timeout are also unknown.
- Whether ZDR covers the realtime path, and whether Mistral will accept health data under its DPA.
- Whether realtime works in Free mode, the actual audio rate limits, how short clips are billed, and whether silence on an open connection is billed.
- Whether the audio models are offered on `api.eu` or `api.us`, and where the global endpoint actually processes audio. The Hong Kong routing was measured from the laptop only, not from the robot.
- Mistral's official status page and uptime history (blocked by Cloudflare).
- How Taiwan PDPA treats a patient's spoken talk about medicines and voice recordings, who the data controller is (the family or our team), and when the 2025 PDPA amendment takes effect (the official database says "undetermined").
- SenseVoice's own error rate on our audio. The A/B test is meant to measure it.