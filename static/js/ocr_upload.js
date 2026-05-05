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
    try { _camStream = await initCamera(video, { video: { width: 320, height: 240 }, audio: false }); }
    catch(e) { alert('Camera error: ' + e.message); }
  }
});

async function scanFile() {
  const file = document.getElementById('fileInput').files[0];
  if (!file) return;
  showScanning(true);
  const fd = new FormData();
  fd.append('file', file);
  const data = await fetch('/ocr/upload', { method: 'POST', body: fd }).then(r => r.json());
  showScanning(false);
  displayResult(data);
}

async function captureAndScan() {
  const video  = document.getElementById('camVideo');
  const canvas = document.getElementById('camCanvas');
  const blob   = await captureFrame(video, canvas, 0.92);
  if (!blob) return;
  showScanning(true);
  const fd = new FormData();
  fd.append('file', new File([blob], 'capture.jpg', { type: 'image/jpeg' }));
  const data = await fetch('/ocr/upload', { method: 'POST', body: fd }).then(r => r.json());
  showScanning(false);
  displayResult(data);
}

function showScanning(show) {
  document.getElementById('scanning').classList.toggle('d-none', !show);
  document.getElementById('resultCard').classList.add('d-none');
}

function displayResult(data) {
  if (data.error) { alert('OCR error: ' + data.error); return; }
  _lastResult = data;
  document.getElementById('rMedName').textContent  = data.med_name || '—';
  document.getElementById('rQty').textContent      = data.quantity || '—';
  document.getElementById('rAmount').textContent   = data.amount_each_intake || '—';
  document.getElementById('rTotal').textContent    = data.total_intake || '—';
  document.getElementById('rWarning').textContent  = data.warning || '—';
  const sched = data.schedule_time || {};
  const slots = Object.entries(sched).filter(([,v])=>v).map(([k])=>k.replace('_',' ')).join(', ');
  document.getElementById('rSchedule').textContent = slots || '—';
  document.getElementById('resultCard').classList.remove('d-none');
  document.getElementById('saveMsg').classList.add('d-none');
}

async function saveResult() {
  if (!_lastResult) return;
  const pillInput = parseInt(document.getElementById('pillInput').value) || 0;
  const body = { ..._lastResult, pill_prescribed: pillInput, total_intake_num: 0 };
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
