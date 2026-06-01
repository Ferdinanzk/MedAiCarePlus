import { useCallback, useEffect, useRef, useState } from 'react';

// useTTS — Text-to-Speech hook using the Web Speech API (window.speechSynthesis).
//
// Returns: { speak, stop, isSpeaking }
//
//   speak(text, lang): maps i18n lang codes ('en' -> 'en-US', 'zh-TW' -> 'zh-TW'),
//     cancels any current speech, then speaks after a 500ms delay (lets UI render first).
//   stop(): cancels current speech via speechSynthesis.cancel().
//   isSpeaking: boolean, true while speaking.
//
// Safety:
//   - Auto-cancels on unmount.
//   - Gracefully no-ops if window.speechSynthesis is undefined (no crash).
//   - Handles the 'voiceschanged' event for voice loading.

function isSupported(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window && !!window.speechSynthesis;
}

function resolveLang(lang: string): string {
  // Accept i18n lang codes and normalize to BCP-47 voice locales.
  if (lang === 'en') return 'en-US';
  if (lang === 'zh-TW') return 'zh-TW';
  return lang || 'en-US';
}

export function useTTS() {
  const [isSpeaking, setIsSpeaking] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const voicesRef = useRef<SpeechSynthesisVoice[]>([]);

  // Load voices and listen for the voiceschanged event (voices load asynchronously).
  useEffect(() => {
    if (!isSupported()) return;

    const loadVoices = () => {
      try {
        voicesRef.current = window.speechSynthesis.getVoices();
      } catch {
        voicesRef.current = [];
      }
    };

    loadVoices();
    window.speechSynthesis.addEventListener('voiceschanged', loadVoices);

    return () => {
      window.speechSynthesis.removeEventListener('voiceschanged', loadVoices);
    };
  }, []);

  const stop = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (isSupported()) {
      try {
        window.speechSynthesis.cancel();
      } catch {
        // ignore
      }
    }
    setIsSpeaking(false);
  }, []);

  const speak = useCallback(
    (text: string, lang: string) => {
      if (!isSupported() || !text) return;

      // Cancel any current/queued speech before starting new.
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      try {
        window.speechSynthesis.cancel();
      } catch {
        // ignore
      }
      setIsSpeaking(false);

      const targetLang = resolveLang(lang);

      // 500ms delay before speaking so the UI can render first.
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        try {
          const utterance = new SpeechSynthesisUtterance(text);
          utterance.lang = targetLang;

          // Prefer a voice matching the target locale if one is available.
          const voices = voicesRef.current.length
            ? voicesRef.current
            : (isSupported() ? window.speechSynthesis.getVoices() : []);
          const match = voices.find((v) => v.lang === targetLang)
            || voices.find((v) => v.lang.toLowerCase().startsWith(targetLang.split('-')[0].toLowerCase()));
          if (match) utterance.voice = match;

          utterance.onstart = () => setIsSpeaking(true);
          utterance.onend = () => setIsSpeaking(false);
          utterance.onerror = () => setIsSpeaking(false);

          window.speechSynthesis.speak(utterance);
          setIsSpeaking(true);
        } catch {
          setIsSpeaking(false);
        }
      }, 500);
    },
    []
  );

  // Auto-cancel on unmount.
  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (isSupported()) {
        try {
          window.speechSynthesis.cancel();
        } catch {
          // ignore
        }
      }
    };
  }, []);

  return { speak, stop, isSpeaking };
}
