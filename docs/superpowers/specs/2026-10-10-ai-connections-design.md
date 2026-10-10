# AI connections: choose the AI service and model for each job (design)

Date: 2026-10-10. Status: design approved in chat (Telegram), spec waiting for review.

This is part 1 of 3:

1. **AI connections** (this spec): add AI services and their keys, add models, and choose the model for each job.
2. **Calling the patient by name**: Reachy uses the person's registered name. Its own short design comes next.
3. **Live voice check-ins with GPT-Live**: uses a connection from part 1. It needs its own spec, because the robot app,
   the safety layers and the consent notice all change.

## 1. Goal

Today every model call (Reachy's reply, the safety check, the after-chat summary and memory) goes to OpenRouter. The
key and the two models are set only in `.env` (`OPENROUTER_API_KEY`, `LLM_MODEL`, `LLM_FALLBACK_MODEL`), and changing
them means editing the file and restarting the container.

After this change:

- A card on the Settings page lists **connections** (an AI service plus its API key), **models** (a model of one
  connection), and **jobs** (which model does what).
- OpenRouter stays the default service for general work. OpenAI can be added as an extra connection. Any other
  service that speaks the OpenAI chat-completions format can be added by its web address, with no code change.
- No model name or service is fixed in code. Each model's request settings are found by the Test button and saved.
- Changes take effect on the next model call, without a restart.

## 2. Decisions taken with the user (2026-10-10)

| # | Question | Decision |
|---|---|---|
| 1 | What "more live" means | GPT-Live voice, but **after** this part (part 3). This part makes the services and models configurable first. |
| 2 | Order | Part 1 (this) first, then the name feature, then GPT-Live. |
| 3 | Who may open the card | **Any logged-in account** (consented user). Saved keys are never sent back to the browser, only their last 4 characters, so a key can be replaced but not copied from the screen. |
| 4 | Consent when the receiving company changes | **The notice text updates itself; patients are not asked again.** Recorded as the user's decision. Known risk: patients who agreed to a notice naming OpenRouter and Google count as agreeing to the new company too, which is weak under Taiwan's PDPA. |
| 5 | Where settings live | **In the database**, keys encrypted. `.env` values are copied in once, on the first start. |

## 3. What the user sees

A new card, **AI connections**, on the Settings page under the Reachy card (`components/AiConnectionsCard.tsx`,
used by `pages/Settings.tsx`). All text is in `i18n.ts` under `aiConnections`, in `en` and `zh-TW`.

**Connections.**
- "+ Add connection" asks for the type, a name, the API key and the service name used in the notice.
- Types:
  - **OpenRouter** (address fixed: `https://openrouter.ai/api/v1`)
  - **OpenAI** (address fixed: `https://api.openai.com/v1`)
  - **Other (OpenAI-compatible)**, which asks for its address (`http://` or `https://`, ending before `/chat/completions`)
- A saved key shows as `••••abcd`. "Replace key" takes a new one. A key that can no longer be decrypted (see §5)
  shows "Enter the key again".

**Models.**
- "+ Add model" on a connection offers the service's own model list (`GET {address}/models`), or a typed model
  name.
- Three short fields fill the consent notice: **company** (who runs the model, e.g. Google, OpenAI), **region**,
  and **data retention**.
- **Test** sends one short question and shows the result, e.g. `✓ 0.8 s` plus the first words of the answer. It also
  finds and saves the request settings the model accepts (§7). On failure it shows the service's own error message.
- A model shows "last call failed" with the reason when a real call failed after its last successful test.

**Jobs.** Three rows, each with a **main** and an optional **backup** model:

| Job | Code that uses it |
|---|---|
| Reachy's replies | `conversation.reply_with_metrics` |
| Safety check | `conversation.classify_risk` (normal and late checks) |
| After-chat summary and memory | `conversation.summarize` and `memory.after_chat_call` (through `conversation.complete_with_reason`) |

- Only models with a saved request profile (tested, or copied from `.env` on day one) can be chosen.
- Saving applies from the next call.

**Warnings shown on the card.**
- `SECRET_KEY` is still the default value: "Keys are not really protected until SECRET_KEY is changed".
- Every connection used by a job has no key: "Reachy will use its fixed line".

## 4. Data

Three new tables in `sql/init.sql` (`CREATE TABLE IF NOT EXISTS`, like the others):

```sql
CREATE TABLE IF NOT EXISTS ai_connection (
    connection_id  BIGSERIAL PRIMARY KEY,
    name           VARCHAR(60)  NOT NULL UNIQUE,
    kind           VARCHAR(20)  NOT NULL CHECK (kind IN ('openrouter','openai','openai_compatible')),
    base_url       VARCHAR(300) NOT NULL,
    api_key_enc    TEXT,                     -- Fernet token; NULL = no key yet
    api_key_last4  VARCHAR(4),
    service_name   VARCHAR(80)  NOT NULL,    -- {LLM_SERVICE} in the notice
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS ai_model (
    model_ref      BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES ai_connection(connection_id),
    model_id       VARCHAR(200) NOT NULL,    -- as the service names it, e.g. gpt-5.4-mini
    company        VARCHAR(80)  NOT NULL DEFAULT '',   -- {LLM_PROVIDER}
    region         VARCHAR(80)  NOT NULL DEFAULT '',   -- {LLM_PROVIDER_REGION}
    retention      VARCHAR(200) NOT NULL DEFAULT '',   -- {LLM_RETENTION}
    profile        JSONB,                    -- request settings (§7); NULL = not tested
    tested_at      TIMESTAMPTZ,
    test_ms        INTEGER,
    last_error     TEXT,                     -- last failed real call or test, NULL after a success
    UNIQUE (connection_id, model_id)
);
CREATE TABLE IF NOT EXISTS ai_job (
    job            VARCHAR(20) PRIMARY KEY CHECK (job IN ('reply','safety','after_chat')),
    primary_ref    BIGINT NOT NULL REFERENCES ai_model(model_ref),
    fallback_ref   BIGINT REFERENCES ai_model(model_ref),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

- There is no `ON DELETE CASCADE`. Deleting a connection or model that a job uses is refused with 409 `in_use` (§10).
- Deleting a connection deletes its unused models in the same transaction.

**In memory.** `app/services/ai_settings.py` keeps one snapshot: connections with decrypted keys, models and jobs.
- It is loaded in the lifespan after `init_pool()`, and reloaded after every write.
- `python -m app.serve` runs both listeners in one process, so they share the snapshot.
- Model calls read only the snapshot, never the database, so a reply never waits on a settings query.

## 5. Key encryption

- Fernet from `cryptography` (new line in `requirements.txt`: `cryptography==50.0.1`, released 2026-08-25).
  50.0.2 (2026-09-30) is newer; check its changelog during implementation and take it if it fixes something relevant.
- The Fernet key is HKDF-SHA256 over `SECRET_KEY`, with info `b"medaicareplus ai-connection keys"`, 32 bytes,
  urlsafe-base64. A separate derived key means the face-token signing secret is never used directly as an
  encryption key.
- Stored: `api_key_enc` and `api_key_last4`. The plain key exists only in the in-memory snapshot and in outgoing
  `Authorization` headers.
- **Never** in any API response, log line, error message or test output. `last_error` keeps the service's message,
  minus anything that looks like a key (`sk-…`, `sk-or-…`, `Bearer …`).
- `SECRET_KEY` changed: `InvalidToken` on load. The connection is kept with `key_readable: false`, its models act as
  "no key", and the card asks for the key again.
- `SECRET_KEY` is the default: the card warns (§3). In `prod` the existing startup check already refuses the default.

## 6. Day one: copying `.env` in

In the lifespan, inside one transaction with `LOCK TABLE ai_connection`, and only when `ai_connection` is empty:

1. One connection: name `OpenRouter`, kind `openrouter`, key from `OPENROUTER_API_KEY` (may be empty, which behaves
   like today's `no_key`), and `service_name` from `LLM_SERVICE` (default `OpenRouter`).
2. One model each for `LLM_MODEL` and `LLM_FALLBACK_MODEL` (only one if they are equal), with company, region and
   retention from `LLM_PROVIDER`, `LLM_PROVIDER_REGION` and `LLM_RETENTION`.
   - Their profile is today's request (§7, the OpenRouter starting point), so they can be assigned without a test.
   - `tested_at` stays NULL; the card shows "copied from .env".
3. All three jobs: main = `LLM_MODEL` (or `LLM_FALLBACK_MODEL` when `LLM_MODEL` is empty), backup =
   `LLM_FALLBACK_MODEL`.

This reproduces today's behaviour exactly.

After this, the database is in charge. Those `.env` lines are ignored, and the card says so ("Settings came from .env
on <date>; edit them here").

The other settings stay in `.env`:
- `LLM_DEADLINE_SECONDS`.
- `OPENROUTER_PROVIDER_ONLY` / `OPENROUTER_DATA_COLLECTION`. These go only to connections of kind `openrouter`.

## 7. Request shaping and the Test button

**Measured on 2026-10-10** with the user's OpenAI key (single calls; HTTP status and OpenAI's own message):

| Model | Old length setting `max_tokens` | `temperature: 0.7` | `reasoning_effort: "none"` |
|---|---|---|---|
| gpt-6-luna | 400 "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead." | 400 "Unsupported value: 'temperature' does not support 0.7 with this model. Only the default (1) value is supported" | 200 |
| gpt-5.4-mini | 400 (same message) | 200 | 200 |
| gpt-4.1-mini | 200 | 200 | 400 "Unrecognized request argument supplied: reasoning_effort" |

One fixed request cannot serve every model. The **profile** saved per model is:

```json
{"token_param": "max_tokens" | "max_completion_tokens",
 "temperature": true | false,
 "reasoning_off": null | {"reasoning": {"enabled": false}} | {"reasoning_effort": "none"} | {"reasoning_effort": "minimal"}}
```

**Test** (`ai_profile.detect`):
1. Start from the kind's starting point:
   - `openrouter`: `max_tokens`, temperature on, `{"reasoning": {"enabled": false}}`. This is today's request, so the
     models copied from `.env` keep working.
   - `openai` and `openai_compatible`: `max_completion_tokens`, temperature on, `{"reasoning_effort": "none"}`.
2. Send a short zh-TW question (`max` 60 tokens, 15 s timeout).
3. On a 400, read the error message and change one setting, then send again. At most 5 tries:
   - `max_tokens` is not supported → `max_completion_tokens`.
   - `max_completion_tokens` is not recognized → `max_tokens`.
   - `temperature` is unsupported → temperature off.
   - `reasoning_effort` value `none` is unsupported → `minimal`.
   - `reasoning_effort` (or `reasoning`) is not recognized or unsupported → `reasoning_off: null`.
4. Success means HTTP 200, a non-empty answer, and a `finish_reason` other than `length`. Save the profile,
   `tested_at`, `test_ms` and the first 40 characters for the card.
   - Any other failure (401, 404 unknown model, 429, timeout, an unrecognised 400) saves nothing and shows the
     service's message.

**Every real call** (`conversation._post`) builds its payload from the model's profile:
- The length goes under `token_param`.
- `temperature` is sent only when `temperature` is true. With it off, memory extraction's `temperature=0` also
  falls back to the model's default.
- `reasoning_off` is merged in.
- `provider` routing and `X-Title` are sent only for kind `openrouter`.

The URL is `{base_url}/chat/completions` and the header is `Authorization: Bearer <key>`. The answer is read exactly
as today: `choices[0].message.content`, `finish_reason == "length"` → unusable, and `usage.completion_tokens`.

## 8. How the jobs are called

- `_complete(...)` gains a `job` argument and tries the job's main model, then its backup, within the same deadline,
  as today. With no backup, the main model is tried once.
  - Today an empty `LLM_MODEL` meant "the fallback, twice". The copied-in setup keeps both, so behaviour is unchanged.
- Each attempt in the metrics adds `connection` (the connection's name) next to the existing `model_requested` /
  `model_served`, so `conversation_turn.metrics` shows which service answered.
- The no-key check `if config.OPENROUTER_API_KEY:` becomes "the model has a readable key". `_reason()` returns
  `no_key` when neither model of the job has one.
- A failed real call writes `last_error` on that model through a background task. It never blocks or fails the turn,
  and the next success clears it.
- Callers:
  - `reply_with_metrics` uses job `reply`.
  - `classify_risk` uses `safety`.
  - `summarize` and `complete_with_reason` (used by `memory.after_chat_call`) use `after_chat`.
  - Deadlines, timeouts, token limits and every `accept` check stay as they are.
- `api_device.py` (the conversation `INSERT`, `model` column) stores the `reply` job's main `model_id` instead of
  `config.LLM_MODEL or config.LLM_FALLBACK_MODEL`.
- The keyword screen (`conversation.screen()`) still runs before any model call and does not depend on these
  settings.

## 9. Consent notice text (decision 4)

`legal_service.fills()` takes the four LLM values from the snapshot once it is loaded (from `.env` before that):

- `LLM_SERVICE`: the distinct `service_name`s of the connections behind every job's main and backup model, in job
  order, joined with ` / `.
- `LLM_PROVIDER`, `LLM_PROVIDER_REGION`, `LLM_RETENTION`: the distinct `company`, `region` and `retention` of those
  models, joined the same way.
- An empty value stays a placeholder, as today, so the notice shows as incomplete (`LegalDocument.complete` false).

After every settings write, in the **same transaction**:
1. Reload the snapshot.
2. `legal_service.load_documents.cache_clear()`.
3. `await register_documents(conn)`. `consent` has a foreign key to `legal_document(kind, terms_version, language,
   sha256)`, so the newly worded notice must be registered before anyone can accept it.

Consents already given stay current (decision 4). `consent_service.is_current` is unchanged: it compares
`terms_version` only.

## 10. API

New router `app/routers/api_ai.py`, prefix `/api/ai`. Every route depends on `get_consented_user` (decision 3;
`tests/test_consent_enforcement.py` checks this automatically).

| Method and path | Body | Result |
|---|---|---|
| `GET /settings` | | `{connections, models, jobs, seeded_at, weak_secret}`. A connection has `key_last4` and `key_readable`, never the key. |
| `POST /connections` | `name, kind, base_url?, api_key?, service_name` | the new connection |
| `PATCH /connections/{id}` | any of `name, base_url, api_key, service_name` | the connection |
| `DELETE /connections/{id}` | | 204, or 409 `in_use` |
| `GET /connections/{id}/remote-models` | | `{models: [id, …]}` from the service's `/models` (ids only, at most 2000, 10 s timeout) |
| `POST /models` | `connection_id, model_id, company, region, retention` | the new model (untested) |
| `PATCH /models/{ref}` | any of `company, region, retention` | the model |
| `DELETE /models/{ref}` | | 204, or 409 `in_use` |
| `POST /models/{ref}/test` | | `{ok, ms, sample, profile, error}` (§7) |
| `PUT /jobs` | `{reply: {primary_ref, fallback_ref}, safety: …, after_chat: …}` | the jobs. 400 `untested_model` when a model has no profile. |

**Validation.**
- `kind` is one of the three types.
- `base_url` is required only for `openai_compatible`, must start with `http://` or `https://`, and has its trailing
  `/` removed.
- Name, model and text lengths follow the column sizes.

**Errors.** Errors use the app's usual `{detail: <code>}` with codes `in_use`, `untested_model`, `not_found`,
`invalid_base_url` and `key_unreadable`. The card shows its own words for each code, never the server's text.

**With `REACHY_FEATURE_ENABLED` in `prod`.** `PUT /jobs`, and edits to a model a job uses, refuse (400
`notice_incomplete`) a state where a job's model has an empty company, region or retention, or is `openrouter/free`.
That way the next start cannot fail on it (§11).

## 11. Startup checks

`run_startup_checks()` runs before the database is ready, so the LLM checks change:

- **Removed from `run_startup_checks()`:**
  - `LLM_PROVIDER`, `LLM_PROVIDER_REGION` and `LLM_RETENTION` as required values.
  - The `LLM_MODEL` / `LLM_FALLBACK_MODEL` "pinned" rule.
- **New `ai_settings.check(snapshot)`**, run in the lifespan after the snapshot loads and only with
  `REACHY_FEATURE_ENABLED`. It applies the same rules to every job's main and backup model: pinned (not
  `openrouter/free`), with company, region and retention filled. It warns in `dev` and exits in `prod`, like today.
- `RISK_CLASSIFIER_API_KEY` stays in the required list unchanged. It is not used by any code today; this spec does not
  touch it, and it is noted for a later clean-up.
- `tests/test_startup_checks.py` is updated to match.

## 12. Error handling and edge cases

- **Service down, rate limit, timeout:** as today. Backup model, then Reachy's fixed line; the late risk check and
  the after-chat sweep retry as before.
- **A model changes upstream** (a 400 at run time, e.g. a setting no longer accepted): the call fails over to the
  backup, `last_error` shows the message, and pressing Test again finds the new profile. There is no automatic
  re-detection during a conversation.
- **Two people edit at once:** the last write wins. Every write reloads the whole snapshot, so it is never half-applied.
- **Address of an "Other" service** (decision 3 lets any logged-in account set it). The server only ever sends
  `POST {base}/chat/completions` and `GET {base}/models` there. It returns to the browser only model ids, a status,
  the first 40 characters of an answer, and the service's error message (cut to 300 characters).
  - Known risk, accepted with decision 3: a logged-in account can make the server contact an address on the home
    network.

## 13. Testing and verification

**Backend** (pytest, fake pools and stubbed `asyncpg`, as the existing tests do):
- `tests/test_ai_settings.py`:
  - encryption round trip, and an unreadable key after `SECRET_KEY` changes
  - `last4`
  - copying `.env` in (one model when both are equal; empty key gives `no_key`)
  - notice fills (distinct values, ` / ` joining, empty stays a placeholder)
  - `check()` rules
- `tests/test_ai_profile.py`: detection driven by the **exact 400 messages measured in §7**, the five-try limit, and
  failures saving nothing.
- `tests/test_api_ai.py`:
  - no response body ever contains the key; checked by searching every route's JSON for a known test key
  - 409 `in_use`, 400 `untested_model`, `weak_secret`
  - the `notice_incomplete` rule
  - `register_documents` called after writes
- Existing tests:
  - `tests/test_conversation.py` and `tests/test_conversation_metrics.py` set a job snapshot instead of
    `config.OPENROUTER_API_KEY` / `LLM_MODEL`.
  - They keep patching `conversation._http_session`.
  - All other tests pass unchanged.

**Frontend:**
- `npx tsc -b` and `npm run build` in `frontend_source`.
- A Playwright spec `tests/ai-connections.spec.ts` with the API mocked (the `reachy.spec.ts` pattern): add a
  connection, the key shown only as last 4, add and test a model, set jobs, the warnings, and the error words.
- No new `eslint` errors beyond the 20 already listed in `CLAUDE.md`.

**Real run on this PC (Docker 29.5.3):**
- `docker compose up -d --build` with a local `.env` (random `SECRET_KEY`, `APP_ENV=dev`).
- Register a test account and open Settings.
- Add the OpenAI key from the key file (never printed).
- Test `gpt-5.4-mini`, `gpt-4.1-mini` and `gpt-6-luna`, and compare the saved profiles with §7.
- Make `gpt-5.4-mini` the `reply` main model.
- Call `conversation.reply_with_metrics` inside the container and check that `metrics.attempts[0].connection` is the
  OpenAI connection.
- Take screenshots of the card at phone and desktop widths.
- **Not testable here:** OpenRouter with a real key (the key is only on the laptop). Its request is unchanged from
  today, and the starting profile is today's payload.

## 14. Not in this part

- GPT-Live voice and streaming replies: part 3.
- Calling the patient by name: part 2.
- Per-patient AI settings: there is one setup for the whole installation.
- Automatic re-detection of a model's settings during a call.
- An operator role or password (decision 3).
- Re-asking consent when the company changes (decision 4).
