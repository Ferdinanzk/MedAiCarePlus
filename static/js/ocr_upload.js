let _lastResult = null;
let _camStream  = null;

// File upload preview
document.getElementById('fileInput')?.addEventListener('change', function() {
  const file = this.files[0];
  if (!file) return;
  const preview = document.getElementById('preview');
  preview.src = URL.createObjectURL(file);
  preview.classList.remove('d-none');
  document.getElementById('scanFileBtn').disabled = false;
});

// Init webcam tab on show
document.querySelector('[data-bs-target="#camTab"]')?.addEventListener('shown.bs.tab', async () => {
  const video = document.getElementById('camVideo');
  if (!_camStream) {
    // Small print on a prescription needs more than 320x240.
    try { _camStream = await initCamera(video, { video: { width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false }); }
    catch(e) { alert('Camera error: ' + e.message); }
  }
});

// Send one image; the scanning spinner always clears, also on a network error or a non-JSON answer.
async function uploadForScan(fd) {
  showScanning(true);
  let data;
  try {
    const r = await fetch('/ocr/upload', { method: 'POST', body: fd });
    data = await r.json().catch(() => ({ error: 'HTTP ' + r.status }));
  } catch (e) {
    data = { error: 'Could not reach the server. Please try again.' };
  } finally {
    showScanning(false);
  }
  displayResult(data);
}

async function scanFile() {
  const file = document.getElementById('fileInput').files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  await uploadForScan(fd);
}

async function captureAndScan() {
  const video  = document.getElementById('camVideo');
  const canvas = document.getElementById('camCanvas');
  const blob   = await captureFrame(video, canvas, 0.92);
  if (!blob) return;
  const fd = new FormData();
  fd.append('file', new File([blob], 'capture.jpg', { type: 'image/jpeg' }));
  await uploadForScan(fd);
}

function showScanning(show) {
  document.getElementById('scanning').classList.toggle('d-none', !show);
  document.getElementById('resultCard').classList.add('d-none');
}

function displayResult(data) {
  if (data.error) { alert('OCR error: ' + data.error); return; }
  _lastResult = data;
  // This page shows and saves the first medicine only; the app's Scan page adds every medicine on the paper.
  const others = Array.isArray(data.medications) ? data.medications.length - 1 : 0;
  document.getElementById('rMedName').textContent  = (data.med_name || '—') +
    (others > 0 ? ` (+${others} more on the paper: use the app's Scan page to add them)` : '');
  document.getElementById('rQty').textContent      = data.quantity || '—';
  document.getElementById('rAmount').textContent   = data.amount_each_intake || '—';
  document.getElementById('rTotal').textContent    = data.total_intake || '—';
  document.getElementById('rWarning').textContent  = data.warning || '—';
  const sched = data.schedule_time || {};
  // The six slots are booleans; custom_times is a list of "HH:MM" times, shown as times.
  const slots = Object.entries(sched).filter(([, v]) => v === true).map(([k]) => k.replace('_', ' '));
  if (Array.isArray(sched.custom_times)) slots.push(...sched.custom_times);
  document.getElementById('rSchedule').textContent = slots.join(', ') || '—';
  document.getElementById('resultCard').classList.remove('d-none');
  document.getElementById('saveMsg').classList.add('d-none');
}

async function saveResult() {
  if (!_lastResult) return;
  const pillInput = parseInt(document.getElementById('pillInput').value) || 0;
  // The first medicine's dose form: drops or creams are never recorded by the camera alone.
  const first = (Array.isArray(_lastResult.medications) && _lastResult.medications[0]) || {};
  const body = {
    ..._lastResult, pill_prescribed: pillInput, total_intake_num: 0,
    dose_form: first.dose_form, units_per_dose: first.units_per_dose,
  };
  const resp = await fetch('/ocr/save', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const data = await resp.json();
  const msg = document.getElementById('saveMsg');
  msg.classList.remove('d-none');
  if (data.med_id) {
    msg.innerHTML = `<div class="alert alert-success py-2">Saved! <a href="/medicines/">View medicines</a></div>`;
  } else {
    msg.innerHTML = `<div class="alert alert-danger py-2">${data.error || 'Save failed'}</div>`;
  }
}
