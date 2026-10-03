# Prescription OCR (the Scan page)

The Scan page reads a photo of a prescription, a medicine bag or a pharmacy receipt and shows **every medicine on
the paper** as an editable card: name, strength, amount on hand, dose form, amount per dose, times a day, days, the
dose times and the last day. The user checks each card against the paper, unticks what they don't want, and adds
the rest to their medicines.

## Setup: one key, every account

OCR needs only **your own Gemini API key** in `.env`:

```
GEMINI_API_KEY=<your key from https://aistudio.google.com/apikey>
```

- The key is a secret. Keep it in `.env` (gitignored), never in a committed file: the repository is public.
  `.env.example` ships with it empty.
- Recreate the app container so it reads `.env`: `docker compose up -d --force-recreate app` (no rebuild needed).
- Check `http://localhost:8080/health`: `"ocr": true`. The app log says
  `[OCR] Gemini vision model: gemini-3.5-flash (fallback: gemini-3.5-flash-lite)`.

Nothing else is per installation or per account. One OCR service in the server process serves every account; the
route only checks that the account has accepted the current terms (core consent), like every other data route.

The models are code defaults, so a new installation needs no model setting:

| Setting | Default | Meaning |
| --- | --- | --- |
| `OCR_MODEL` | `gemini-3.5-flash` | The model asked first. Empty means the default. |
| `OCR_GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash-lite` | Asked once when the first model is busy (HTTP 429/500/502/503/504), does not answer in time, or no longer exists (404). Empty turns the fallback off. |
| `OCR_GEMINI_TIMEOUT` | `25` | Seconds to wait for one Gemini request. |
| `OCR_GEMINI_IMAGE_BUDGET` | `45` | Seconds for the whole scan, both models together. With the budget spent, no further request is made. |

## Which model, and why

Until 4 Oct 2026 the default was `gemini-3.8-flash`. On 3-4 Oct it answered every request with 503 "high demand",
and the old code hid the error and showed every field as `N/A`, for every account. **If an older `.env` still says
`OCR_MODEL=gemini-3.8-flash`, change it or delete the line.**

Later on 4 Oct a real photo of a Taiwanese pharmacy receipt (a dot-matrix 藥品明細收據 with three medicines in a
table, held in a hand on a dark background) failed with `ocr_incomplete_schedule`. Read with the old one-medicine
prompt, both models returned only one of the three medicines, and they invented fields: a hospital guessed from the
pharmacy's city, the prescriber's institution code as the physician, the visit date as "use before", a clinic name
for the pharmacy and a year off by two. That led to the multi-medicine prompt and the server-side checks below, and
to a comparison of the two models with the final prompt. The comparison script and the photo stay outside the
repository, because the receipt holds a real person's data; its personal fields were compared with expected values
and printed only as match or mismatch.

Measured on this laptop on 4 Oct 2026, one scan per model and image, fallback off, through `process_image` (photo
clean-up included):

| Image | Model | Checks right | Gemini request | Whole scan |
| --- | --- | --- | --- | --- |
| Synthetic hospital slip, 2 medicines, check-box row | `gemini-3.5-flash` | 24 / 24 | 7.2 s | 7.8 s |
| | `gemini-3.5-flash-lite` | 24 / 24 | 2.1 s | 2.5 s |
| Real pharmacy receipt, 3 medicines, dot-matrix photo | `gemini-3.5-flash` | 30 / 32 | 11.8 s | 12.4 s |
| | `gemini-3.5-flash-lite` | 27 / 32 | 2.3 s | 3.0 s |

The checks are per medicine (name exact and ignoring spaces and dots, count, times a day, days, dose form, dose
times) and per paper (medicine count, hospital, physician, pharmacist, patient, prescription number, visit and
dispensing dates, use-before, pharmacy, no ID number or birth date in the answer).

- On the receipt, both models found all three medicines with the right quantities, frequencies, days, dose forms
  and dose times, and neither invented a hospital, physician, prescription number or use-before date.
- `gemini-3.5-flash` read 2 of the 3 names exactly; in the third it read one letter wrong (I as O).
- `gemini-3.5-flash-lite` read 1 of 3: the same wrong letter, and it gave the ear drops the first row's name, a
  different medicine. It also misread the patient's name.
- With the prompt before its last revision (4 more calls), flash-lite read none of the three names right and flash
  completed a name cut off at the column edge (ending in "0") into a strength that is not printed. The final prompt
  tells the model not to complete cut-off names and to read each row's own letters.

So **`gemini-3.5-flash` goes first** and `gemini-3.5-flash-lite` is the fallback. Flash is slower (7-12 s against
2-3 s), so one request may take 25 s (`OCR_GEMINI_TIMEOUT`); a timed-out flash request still leaves the fallback 20 s
of the 45 s scan budget. Eight live calls in total; these are two images, not an accuracy study. A test pins both
defaults. Don't change a default without a live scan.

**Neither model is reliable letter by letter on dot-matrix print.** The cards show "On the paper: …" under each name
and ask the user to check every medicine against the paper before adding it.

## How a scan works

1. The photo is cleaned up (denoise, contrast, sharpen; under 1 s at 1920×1080).
2. **One** Gemini request (`_PROMPT_GEMINI` in `app/services/ocr_service.py`) returns the paper's own fields and a
   `medications` list with one entry per medicine line, copied as printed. The prompt says:
   - list every row of a table, never merge, skip or copy a name from another row;
   - copy only printed text, use `N/A` when a label is absent, never correct or complete a name;
   - never infer a hospital or clinic from an address, city, phone number or code; a pharmacy (藥局) is not a hospital;
   - a number or code is never a person, and fee words (藥費, 藥事費) are never names;
   - what the Taiwanese receipt labels mean: 健保代碼 (the pharmacy's insurance code), 就醫序號 (a visit sequence
     number, not a prescription number), 部分負擔代號, 開立處方箋之醫院(診所)/醫師 followed by a number (the
     prescribing institution's code, neither a hospital name nor a physician), 藥費/藥事費/合計 (fees), 調劑者 (the
     dispensing pharmacist), 就醫日 and 調劑日期 in compact ROC form (1150928 = ROC 115/09/28), 日份 (days);
   - never return a national ID number (身分證) or a date of birth (生日);
   - the six schedule icons (早上, 中午, 晚上, 睡前, 飯前, 飯後) when the paper has such a row, else `null`.
   The key goes only in the `x-goog-api-key` header.
3. The server works out the rest (`app/services/ocr_parsing.py`, pure functions with unit tests):
   - **Dose times from the usage text**, for every medicine: 一天/一日/每日/每天 N 次 (Arabic, full-width or Chinese
     numerals), 分N次, QD/BID/TID/QID, HS, QAM/QPM, Q4H-Q12H and 每 N 小時 (by doses a day), 早晚, 早中晚, 三餐,
     早/中午/晚/睡前, 飯前/飯後 (AC/PC, also run together as TIDPC), dotted forms (t.i.d., b.i.d. p.c., h.s.), English
     phrases, and 需要時/必要時/PRN (as needed). 1 a day is morning, 2 morning + evening, 3 + noon, 4 + bedtime; more
     than 4 adds other times (16:00, 06:00, …). Words that name a time win, and a count adds the usual times they
     leave out (一天2次 睡前: morning + bedtime). An as-needed medicine gets no fixed times; its count is its daily
     maximum.
     - 晚 written right before 睡前 (每晚睡前, 晚上睡前, 夜間睡前) is the bedtime dose, not an evening dose as well. A
       list keeps every time (晚上、睡前 and 早晚睡前 are separate doses), unless the printed count is lower: 一天1次
       晚上 睡前 is one bedtime dose.
     - A range (一天3-4次, 每日3至4次) is scheduled at its lower count; the upper one is `max_per_day`, saved as the
       medicine's daily maximum.
     - 每天1粒 with no 次 anywhere is once a day; 每日2粒 分2次 is twice a day, one each.
     - QOD (every other day) gives `interval_hours` 48 and no times: reminders can only repeat daily, so the card
       says so, and a dose time ticked for it is saved with a 46-hour minimum gap (overdose protection).
   - **The icon row** (hospital bags) wins for the dose times when it is readable, at least one time is checked and
     the paper has one medicine; its meal marks win when it checks one. On a paper with several medicines the row
     only fills in a medicine whose own text gives no time, because one row can't tell which medicine a mark is for.
   - **Dates** become ISO `YYYY-MM-DD`: 1150928, 115/09/28, 115.9.28, 115年9月28日, 2026-09-28, 20260928. A visit or
     dispensing date must be within the last 5 years and not in the future; a use-before date within 5 years back and
     10 years ahead. A use-before date equal to or before the visit or dispensing date is that date copied, and is
     dropped.
   - **Names**: a physician, pharmacist or patient name that has 3+ digits, no letters, or words such as 費, 藥事,
     健保, 代號, 醫院, 藥局 is dropped. In a hospital or pharmacy name, codes are removed first (健保代碼:S945001122,
     or a stand-alone code of 6+ digits), then an address or phone after it and a document title (藥品明細收據); what
     is left must not be a label (開立處方箋…, 代碼, 地址). A "hospital" that is a pharmacy moves to `pharmacy`.
   - **No ID numbers or birth dates**: every text field has national ID numbers (a letter and 9 digits, or masked
     like `A12*****89`, also with a mask a character longer or shorter, and the older two-letter resident
     certificate masked) and labelled birth dates (生日, 出生日期, DOB, followed by a date) removed before anything
     else reads it, and once more on the whole answer. A label is removed only with a value of the right shape after
     it, and English labels only as whole words, so medicine text stays as printed: "Calcium Dobesilate", "Tranexamic
     Acid Normal", "Avoid nonsteroidal…" and 新生兒出生後 were corrupted by the first version of this rule. An NHI
     drug code (two letters and 8 digits, never masked) is left alone.
   - **Dose form** from the name first, then the unit and usage: 錠/TAB/F.C./顆 → tablet, CAP/膠囊 → capsule (both
     `solid_oral`); OTIC/EAR/耳 → ear drops, EYE/OPH/眼 → eye drops, nasal, drops, granules, suppositories (also
     陰道錠 and VAGINAL tablets) and anything unrecognised → `other`; 膏/OINTMENT/CREAM/GEL/PATCH → `topical`;
     SYRUP/SOLUTION → `liquid`; INHALER → `inhaler`; INJ → `injection`. Only one `solid_oral` unit a dose may be
     recorded by the camera alone, so drops and creams always need a person.
   - **Amounts**: tablets per dose from 每次1顆, 1.5錠, 半顆, 一顆半, 1/2 TAB, 每日2粒分2次, or, when no amount is
     printed, from the total (15 tablets, 3 a day, 5 days: 1). 半 counts only next to a tablet word or 每次: 飯前半小時
     and 7點半 are times. The suggested stock is the tablet count; for a bottle or tube (瓶, 支, 條, ml) it is the
     course's doses (2 a day for 5 days: 10), one application each, and `null` when they can't be counted (as
     needed, no days): the card then asks for the amount on hand.
4. **422 `ocr_incomplete_schedule` only when no medicine on the paper has any schedule information**: a dose time
   or meal mark, as-needed use, or a recognised interval (QOD). A medicine without a schedule still gets its card
   ("no dose times were read").

## What the API returns

`POST /api/ocr/parse` returns the 20 fields it always did, now holding the **first** medicine plus the paper's
fields (so older clients keep working), and these additions:

| Field | Meaning |
| --- | --- |
| `medications` | One object per medicine, in printed order (below) |
| `pharmacy` | The pharmacy's name as printed, or `N/A` |
| `visit_date` | 就醫日, ISO, or `N/A` |
| `document_type` | `medicine_bag`, `prescription`, `pharmacy_receipt` or `other` |

`date_dispensed` and `use_before` are ISO dates or `N/A`. `schedule_time` has the six booleans, plus `custom_times`
only when a medicine needs more than four times a day.

Each `medications` entry has the per-medicine fields of the top level (`med_name`, `dosage`, `quantity`,
`pill_count`, `amount_each_intake`, `total_intake`, `schedule_time`, `instructions`, `intake_time_label`, `warning`,
`pill_description`, `clinical_uses`, `manufacturer`) and: `unit`, `frequency_text` (the usage as printed),
`times_per_day`, `max_per_day` (the upper end of a printed range, else `null`), `interval_hours`, `days`,
`units_per_dose`, `form` (tablet, capsule, ear_drops, eye_drops, topical, oral_liquid, …), `dose_form` (the
medication API's value), `as_needed`, `schedule_source` (`icons`, `text` or `none`) and `stock` (the suggested amount
on hand, or `null`).

## The Scan page

- One card per medicine, each with a tick box "Add this medicine". Every field the medication API stores can be
  edited: name, strength, dose form, amount on hand, amount per dose, times a day (which sets the usual times),
  "only when needed", the six time and meal boxes, other times, days and the last day.
- **Last day** (saved as `use_before`, which ends the generated reminders): the dispensing date (else the visit
  date, else today) plus the days, minus one; without days, a printed use-before date. The card's
  `prescription_meta` keeps `course_end` (when the date is the course's end, not the printed use-before) and
  `printed_use_before`; the Medications page then says "Course ends …" instead of "Expires …", and `/today` gives
  no expiry warning for it (the Schedule page shows that warning).
- **A course that already ended** by the paper's dates (the receipt above, dispensed 28 Sep for 5 days, scanned
  4 Oct) starts **unticked**, and a ticked one can't be saved until its last day is changed or cleared: saving it
  would have created reminders for the rest of the day. The server also generates no doses for a `use_before`
  before today (`schedule.occurrences`).
- **Amount on hand**: the server's `stock`, never the printed count of bottles. Less than one dose on hand blocks
  the save ("no dose can be recorded with none"): the camera, Reachy and Take Now all refuse a dose without stock.
- A medicine that is not one tablet or capsule a dose shows "Reachy cannot record this medicine by itself". Drops
  and creams are saved with a non-solid `dose_form`, so the robot and the browser never record them alone.
- **As needed**: the card shows "At most times a day" (the paper's maximum, or a range's upper end) instead of
  "Times a day"; editing it never ticks dose times. It is saved unscheduled (unless the user ticks times), with
  `max_daily_doses` and the printed interval as `min_interval_minutes`. A scheduled medicine with a range
  (一天3-4次) is saved with the upper count as `max_daily_doses`.
- **Already in the list**: after the scan and again just before saving, the page reads `GET /api/medications`. A card
  whose name matches an active medicine (ignoring case, spaces and dots) is unticked once with a note; the user can
  tick it again. The check before saving also catches a save whose answer was lost but which the server stored.
- "Add N medicines" saves each ticked card through `POST /api/medications`, one after the other. A card that fails
  says so in the page's language (sign in again, accept the terms, or "could not be saved"); the server's text goes
  to the browser console only. It stays ticked; pressing the button again saves only what is not saved yet.
- All labels are in English and 繁體中文 (`scan.*` in `frontend_source/src/i18n.ts`); the logic is in
  `frontend_source/src/lib/scan.ts`.
- The old Jinja page `/ocr` still shows and saves only the first medicine (it now says how many more the paper
  has). `/ocr/save` stores that medicine's `dose_form` and `units_per_dose` from the scan, `other` when the body has
  no valid form.

Without a Gemini key, OCR uses a local Ollama vision model if one answers at `OLLAMA_URL` (two passes: the text with
the same multi-medicine prompt, then the icon-row crop). Both passes share one `OLLAMA_TIMEOUT` (180 s) budget. YOLO
page detection is optional and not installed in the container; the whole photo is used.

## Errors

A failed scan never looks like a result. The server answers with a non-2xx JSON body `{"error", "code"}`, and the
Scan page shows its own sentence for each code in the page's language (`scan.errors.<code>` in
`frontend_source/src/i18n.ts`, English and 繁體中文).

| Code | HTTP | Meaning | What to do |
| --- | --- | --- | --- |
| `ocr_unavailable` | 503 | No Gemini key and no Ollama model | Add `GEMINI_API_KEY` to `.env`, recreate the app container |
| `invalid_image` | 400 | Not a readable image | Take the photo again |
| `ocr_provider_busy` | 503 | Both models busy or too slow | Try again in a minute |
| `ocr_provider_timeout` | 504 | The 45 s scan budget ran out | Try again |
| `ocr_provider_auth` | 502 | Google refused the key (401/403) | Check the key in `.env` |
| `ocr_model_not_found` | 502 | Both configured models are gone (404) | Fix or delete `OCR_MODEL` / `OCR_GEMINI_FALLBACK_MODEL` in `.env` |
| `ocr_provider_error` | 502 | Network error or another provider error | Check the laptop's internet connection |
| `ocr_invalid_response` | 502 | The model's answer was not usable JSON | Try again with a clearer photo |
| `ocr_no_prescription_text` | 422 | No medicine name could be read | Retake in good light, whole label in the frame |
| `ocr_incomplete_schedule` | 422 | No medicine has a schedule (no icon, no usage text with a time, none as-needed) | Retake with the icon row and instructions in view |

## Troubleshooting

- `/health` shows `"ocr": false`: the app has no key. Check `.env`, then `docker compose up -d --force-recreate app`.
- Look in `docker compose logs app` for lines starting `[OCR]`. They name the model and the HTTP status of a failed
  request (`returned transient HTTP 503`, `was not found (HTTP 404)`, `did not answer within 25 s`), and say when
  the fallback was tried. They never contain the key, the photo or the text read; keep it that way when debugging.
- To check which models your key can use, call Gemini's `models.list` and look for `generateContent` in
  `supportedGenerationMethods`.

## Tests

- `tests/test_ocr_parsing.py`: the frequency parser, ROC dates, dose forms, amounts, ID and birth-date removal, names.
- `tests/test_ocr_service.py`: Gemini requests and the fallback, the defaults, and whole scans with mocked answers
  modelled on the receipt (made-up names, codes and ID): every medicine, no invented fields, no ID or birth date,
  the icon rule and the 422 rule.
- `frontend_source/tests/scan.unit.ts` (`node --experimental-strip-types --test tests/scan.unit.ts`): drafts, last
  day, ended courses, stock, as-needed limits, ranges and QOD, duplicates, error keys and the save payload.
  `frontend_source/tests/scan.spec.ts` (Playwright, API mocked, a canvas as the camera): the cards, saving the
  ticked ones, a course that already ended (unticked, saved only with a new last day), a failed save retried, and a
  medicine already in the list.
- `tests/test_schedule_adherence.py` and `tests/test_medication_fields.py`: no doses for a last day that passed, and
  no expiry warning for a scanned course's end.

## Privacy

With Gemini, the whole photo (patient name, physician, hospital, medicines, and whatever else is printed, such as an
ID number or birth date) goes to Google for each scan. Nothing from OCR is stored: the photo is processed in memory,
and only the medications the user saves are kept. The server never returns a national ID number or a date of birth
read from the paper, so neither can reach the medication record; the patient name, pharmacy, pharmacist and dates
are kept in the saved medicine's `prescription_meta`, as before.
