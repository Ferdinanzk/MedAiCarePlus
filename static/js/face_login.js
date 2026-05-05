let _stream = null;
let _interval = null;
const COLORS = { identified: '#28a745', unknown: '#007bff', error: '#dc3545' };

(async () => {
  const video  = document.getElementById('video');
  const canvas = document.getElementById('canvas');
  const status = document.getElementById('status');
  try {
    _stream = await initCamera(video);
    status.textContent = 'Camera ready — scanning…';
    _interval = setInterval(() => sendFrame(video, canvas, status), 1500);
  } catch (e) {
    status.textContent = 'Camera error: ' + e.message;
    status.className = 'text-danger small mb-3';
  }
})();

async function sendFrame(video, canvas, status) {
  const blob = await captureFrame(video, canvas, 0.8);
  if (!blob) return;
  try {
    const resp = await fetch('/auth/face-frame', { method: 'POST', body: blob,
      headers: { 'Content-Type': 'application/octet-stream' } });
    const data = await resp.json();

    if (data.identified && data.name) {
      clearInterval(_interval);
      stopCamera(_stream);
      status.textContent = `Recognised: ${data.name} (dist ${(data.distance||0).toFixed(3)})`;
      status.className = 'text-success small mb-3';
      document.getElementById('recognisedName').value = data.name;
      document.getElementById('recognisedMsg').textContent =
        `Welcome back, ${data.name}! Click Confirm Login to continue.`;
      document.getElementById('confirmForm').classList.remove('d-none');
    } else if (data.error) {
      status.textContent = 'Model: ' + data.error;
    } else {
      status.textContent = `Scanning… (${data.face_count} face${data.face_count!==1?'s':''} detected)`;
    }
  } catch (e) {
    status.textContent = 'Network error: ' + e.message;
  }
}
