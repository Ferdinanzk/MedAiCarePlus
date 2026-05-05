let _stream = null, _ws = null, _interval = null;
let _lastResult = null;
const EMOJI = { Angry:'😠', Happy:'😊', Neutral:'😐', Sad:'😢' };
const BAR_COLOR = { Angry:'bg-danger', Happy:'bg-success', Neutral:'bg-secondary', Sad:'bg-primary' };
const SESSION_DURATION = 5000;

(async () => {
  const video = document.getElementById('video');
  try {
    _stream = await initCamera(video, { video: { width: 480, height: 360 }, audio: false });
  } catch(e) {
    document.getElementById('startBtn').disabled = true;
    alert('Camera error: ' + e.message);
  }
})();

function startSession() {
  document.getElementById('startBtn').disabled = true;
  document.getElementById('resultPanel').classList.add('d-none');
  document.getElementById('timerBar').classList.remove('d-none');

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  _ws = new WebSocket(`${proto}://${location.host}/emotion/ws/emotion`);
  _ws.binaryType = 'arraybuffer';

  const video  = document.getElementById('video');
  const canvas = document.getElementById('canvas');
  const fill   = document.getElementById('timerFill');
  const tLabel = document.getElementById('timerLabel');
  const start  = Date.now();

  _interval = setInterval(async () => {
    const elapsed = Date.now() - start;
    const pct = Math.max(0, 100 - (elapsed / SESSION_DURATION * 100));
    fill.style.width = pct + '%';
    tLabel.textContent = `Recording… ${((SESSION_DURATION - elapsed)/1000).toFixed(1)}s`;

    if (_ws.readyState === WebSocket.OPEN) {
      const blob = await captureFrame(video, canvas, 0.75);
      if (blob) _ws.send(await blob.arrayBuffer());
    }

    if (elapsed >= SESSION_DURATION) {
      clearInterval(_interval);
      showResult();
    }
  }, 250);

  _ws.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if (d.detected && d.emotion_type) {
      _lastResult = d;
      document.getElementById('emotionLabel').textContent = d.emotion_type;
      document.getElementById('emotionEmoji').textContent = EMOJI[d.emotion_type] || '';
      ['Angry','Happy','Neutral','Sad'].forEach(em => {
        const pct = Math.round((d.probabilities[em] || 0) * 100);
        const bar = document.getElementById('bar_' + em);
        bar.style.width = pct + '%';
        bar.className = `progress-bar ${BAR_COLOR[em]}`;
        document.getElementById('pct_' + em).textContent = pct + '%';
      });
    }
  };
}

function showResult() {
  document.getElementById('timerBar').classList.add('d-none');
  if (_lastResult) {
    document.getElementById('finalEmotion').textContent =
      (_lastResult.emotion_type || '—') + ' ' + (EMOJI[_lastResult.emotion_type] || '');
    document.getElementById('finalScore').textContent =
      Math.round((_lastResult.emotion_score || 0) * 100) + '%';
  }
  document.getElementById('resultPanel').classList.remove('d-none');
}

async function saveEmotion() {
  if (!_lastResult) return;
  const note = document.getElementById('noteInput').value;
  const resp = await fetch('/emotion/save', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      emotion_type:  _lastResult.emotion_type,
      emotion_score: _lastResult.emotion_score,
      note: note || null,
    })
  });
  const data = await resp.json();
  if (data.emot_id) {
    alert('Emotion saved!');
    resetSession();
  }
}

function resetSession() {
  if (_ws) { _ws.close(); _ws = null; }
  clearInterval(_interval);
  _lastResult = null;
  document.getElementById('startBtn').disabled = false;
  document.getElementById('resultPanel').classList.add('d-none');
  document.getElementById('timerBar').classList.add('d-none');
  ['Angry','Happy','Neutral','Sad'].forEach(em => {
    document.getElementById('bar_' + em).style.width = '0%';
    document.getElementById('pct_' + em).textContent = '0%';
  });
  document.getElementById('emotionLabel').textContent = '—';
  document.getElementById('emotionEmoji').textContent = '';
}
