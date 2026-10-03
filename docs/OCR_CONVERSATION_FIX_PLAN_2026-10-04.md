# OCR and conversation response time repair

## Requested outcome

Restore prescription OCR and shorten the wait for conversation replies. Implementation is delegated to Luna agents, with integration and deployment owned by the main agent.

## Plan

1. Establish the failures: inspect OCR provider responses using synthetic inputs, review aggregate conversation timings without reading patient transcripts, and trace the request deadline.
2. **Luna OCR agent:** repair provider integration and extraction failures; return actionable errors; add focused regression checks. Keep credentials in server environment configuration.
3. **Luna conversation agent:** remove avoidable waits and ensure bounded model calls and fallbacks while preserving risk checks and memory behavior; add focused regression checks.
4. **Main agent:** review both changes, apply shared configuration updates, and run focused integration checks. Rebuild the running app and confirm readiness, OCR behavior, conversation timing, and existing LINE connectivity.
5. Record measured results and any remaining external provider or robot limits.

## Acceptance

- A synthetic prescription can be extracted, or the caller receives a specific provider/configuration error rather than blank success data.
- Conversation replies obey the configured time budget; risk-related behavior stays covered by regression tests.
- Credentials and patient data are absent from logs and reports.
- The deployed app remains healthy and the LINE tunnel remains usable.

## Status (4 Oct 2026)

**OCR: fixed for every account.**
- Cause: the code default `OCR_MODEL=gemini-3.8-flash` answered every request with 503 "high demand", and the old code swallowed the error and returned every field as `N/A`.
- Fix: defaults `gemini-3.5-flash-lite`, then once `gemini-3.5-flash`. 429/5xx, a timeout or a 404 gets one try on the fallback, within 15 s per request and 45 s per scan. Every failure is a non-2xx `{error, code}`, shown in the page's language. A prescription without a readable icon row is read from its instructions text. An installation needs only `GEMINI_API_KEY` in `.env` ([OCR.md](OCR.md)).
- Measured on this laptop: all three accounts (u1, u34, u35) got a real result through `POST /api/ocr/parse` in about 2 s; a live call on a synthetic prescription took 2.8 s.
- Not yet tested: a real photo through the Scan page camera, which now asks for up to 1920×1080.
- **Follow-up (4 Oct, later):** a real three-medicine pharmacy receipt failed with `ocr_incomplete_schedule`. A scan now returns every medicine on the paper (`medications`), works out each one's dose times, dose form and amounts on the server, normalises ROC dates, strips ID numbers and birth dates, and stops the model inventing a hospital, physician or use-before date; the Scan page shows one editable card per medicine. A comparison on the receipt and a synthetic slip made `gemini-3.5-flash` the primary and `gemini-3.5-flash-lite` the fallback, with 25 s per request ([OCR.md](OCR.md#which-model-and-why)). Not deployed yet.
- **After review (4 Oct):** the parser no longer schedules 每晚睡前 twice, reads 飯前半小時 as half a tablet, or corrupts medicine names while removing IDs ("Calcium Dobesilate"); it reads dotted codes, ranges, 每天1粒 and QOD, and OPH / vaginal forms are not oral. On the Scan page a course that already ended starts unticked (and the server generates no doses for it), the stock is never a count of bottles, as-needed cards edit a daily maximum only, duplicates of active medicines start unticked, and failed saves are worded in the page's language. The legacy `/ocr/save` stores the scanned dose form.

**Conversation replies: a small keep-alive change only.** Calls to OpenRouter now reuse a connection per executor thread (replaced after 60 s idle). A new connection costs about 0.1 s against replies of about 1.5 s or more, so this is not the reply-speed fix. Its effect is **not measured**: compare `server.llm_ms` (median and p90 from `/api/conversations/metrics/summary`) over several check-ins before and after it is deployed. Deadlines, fallbacks and risk checks are unchanged.

**Robot speech (app 0.5.4, not deployed):** an opening comma clause is spoken while the rest is synthesised, so the first sound comes sooner. A first version cut every comma-free clause at 16 characters, which split the spoken dose refusals mid-word (明天早|上6点, 吃满 4|次了); the committed version never cuts inside a clause.

**Credentials and patient data:** the Gemini key is sent only in a request header and is in no log, test or committed file; tests use synthetic images only.
