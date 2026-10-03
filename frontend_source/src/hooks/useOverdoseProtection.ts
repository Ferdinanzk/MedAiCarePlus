import { useEffect, useState } from 'react';
import { fetchOverdoseProtection, overdoseProtectionCached } from '../lib/notify-api';

/**
 * The patient's overdose protection switch (Settings), null until it is known. With it off the server lets any open
 * dose be started, so a page stops labelling doses as not due or missed; while it loads, doses are checked as if on.
 */
export function useOverdoseProtection(): boolean | null {
  const [value, setValue] = useState<boolean | null>(() => overdoseProtectionCached());
  useEffect(() => {
    let active = true;
    void fetchOverdoseProtection().then(next => { if (active) setValue(next); });
    return () => { active = false; };
  }, []);
  return value;
}
