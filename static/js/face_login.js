let _stream = null;
const COLORS = { identified: '#28a745', unknown: '#007bff', error: '#dc3545' };
const FRAME_COUNT = 5;
const FRAME_GAP_MS = 200;
const PAUSE_MS = 500;
const REASON_TEXT = {
  come_closer: 'Please come closer to the camera.',
  no_face: 'Please come closer to the camera.',
  two_faces: 'Two people are close to the camera. Please let one person come closer.',
  not_live: 'Please look at the camera yourself.',
  unknown: 'Face not recognised. Try email login below.',
};

(async () => {
  const video  = document.getElementById('video');
  const canvas = document.getElementById('canvas');
  const status = document.getElementById('status');
  try {
    _stream = await initCamera(video);
    status.textContent = 'Camera ready — scanning…';
    await scanLoop(video, canvas, status);
  } catch (e) {
    status.textContent = 'Camera error: ' + e.message;
    status.className = 'text-danger small mb-3';
  }
})();

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// One attempt at a time: 5 frames, one request, wait for the answer, then the next attempt.
async function scanLoop(video, canvas, status) {
  while (true) {
    if (await attempt(video, canvas, status)) return;
    await sleep(PAUSE_MS);
  }
}

async function attempt(video, canvas, status) {
  const form = new FormData();
  for (let i = 0; i < FRAME_COUNT; i++) {
    if (i > 0) await sleep(FRAME_GAP_MS);
    const blob = await captureFrame(video, canvas, 0.8);
    if (blob) form.append('files', blob, `face${i}.jpg`);
  }
  if (![...form.keys()].length) return false;
  try {
    const resp = await fetch('/auth/face-frame', { method: 'POST', body: form });
    const data = await resp.json();

    if (data.identified && data.login_ticket) {
      stopCamera(_stream);
      status.textContent = 'Recognised';
      status.className = 'text-success small mb-3';
      document.getElementById('loginTicket').value = data.login_ticket;
      document.getElementById('recognisedMsg').textContent =
        `Welcome back, ${data.name}! Click Confirm Login to continue.`;
      document.getElementById('confirmForm').classList.remove('d-none');
      return true;
    } else if (!resp.ok) {
      status.textContent = 'Server unavailable. Trying again…';
    } else if (data.error) {
      status.textContent = 'Model: ' + data.error;
    } else {
      status.textContent = REASON_TEXT[data.reason] || 'Scanning…';
    }
  } catch (e) {
    status.textContent = 'Network error: ' + e.message;
  }
  return false;
}
