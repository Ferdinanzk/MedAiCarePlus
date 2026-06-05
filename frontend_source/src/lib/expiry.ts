// expiry.ts — parse a medication "use before" date and classify its expiry status.
//
// The OCR returns use_before as a raw string, most often in the ROC / Minguo calendar
// (e.g. "114年08月18日" = Gregorian 2025-08-18), but sometimes Gregorian ("2025-08-18",
// "2025/08/18"). This mirrors the backend ROC rule (roc_year + 1911, see
// app/routers/api_medications.py) and adds Gregorian parsing, which the backend lacks.

export type ExpiryStatus = 'expired' | 'soon' | 'ok' | 'unknown';

export interface ExpiryInfo {
  status: ExpiryStatus;
  date: Date | null;     // resolved Gregorian date (local, midnight)
  daysLeft: number | null;
  iso: string | null;    // YYYY-MM-DD of the resolved date, for display
}

function toLocalMidnight(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function toIso(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${m}-${day}`;
}

function parseDate(raw: string): Date | null {
  const s = raw.trim();

  // ROC / Gregorian "年月日": 114年08月18日 / 2025年08月18日
  const cjk = s.match(/(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日/);
  if (cjk) {
    let year = parseInt(cjk[1], 10);
    const month = parseInt(cjk[2], 10);
    const day = parseInt(cjk[3], 10);
    if (year < 1000) year += 1911; // ROC year -> Gregorian
    const d = new Date(year, month - 1, day);
    return isNaN(d.getTime()) ? null : d;
  }

  // Gregorian with separators: 2025-08-18 / 2025/08/18 / 2025.08.18
  const greg = s.match(/(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})/);
  if (greg) {
    const d = new Date(parseInt(greg[1], 10), parseInt(greg[2], 10) - 1, parseInt(greg[3], 10));
    return isNaN(d.getTime()) ? null : d;
  }

  // Fallback: let the engine try (handles ISO timestamps, locale strings, etc.)
  const fallback = new Date(s);
  return isNaN(fallback.getTime()) ? null : fallback;
}

export function getExpiryStatus(useBefore?: string | null, soonDays = 7): ExpiryInfo {
  const none: ExpiryInfo = { status: 'unknown', date: null, daysLeft: null, iso: null };
  if (!useBefore || useBefore.trim() === '' || useBefore.trim() === 'N/A') return none;

  const parsed = parseDate(useBefore);
  if (!parsed) return none;

  const date = toLocalMidnight(parsed);
  const today = toLocalMidnight(new Date());
  const daysLeft = Math.floor((date.getTime() - today.getTime()) / 86400000);

  let status: ExpiryStatus;
  if (daysLeft < 0) status = 'expired';
  else if (daysLeft <= soonDays) status = 'soon';
  else status = 'ok';

  return { status, date, daysLeft, iso: toIso(date) };
}
