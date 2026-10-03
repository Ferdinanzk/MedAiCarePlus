import { useEffect, useState } from 'react';

/**
 * The current time, refreshed every `intervalMs` and when the tab is shown again. A page left open
 * then offers a dose once it becomes due (lib/doses isDue), instead of keeping the time it was drawn at.
 */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const tick = () => setNow(Date.now());
    const timer = window.setInterval(tick, intervalMs);
    document.addEventListener('visibilitychange', tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [intervalMs]);
  return now;
}
