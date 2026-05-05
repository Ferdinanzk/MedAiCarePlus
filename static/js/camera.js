async function initCamera(videoEl, constraints) {
  constraints = constraints || { video: { width: 640, height: 480, facingMode: 'user' }, audio: false };
  const stream = await navigator.mediaDevices.getUserMedia(constraints);
  videoEl.srcObject = stream;
  return stream;
}

function captureFrame(videoEl, canvasEl, quality) {
  quality = quality || 0.85;
  canvasEl.width  = videoEl.videoWidth  || videoEl.width;
  canvasEl.height = videoEl.videoHeight || videoEl.height;
  const ctx = canvasEl.getContext('2d');
  ctx.drawImage(videoEl, 0, 0, canvasEl.width, canvasEl.height);
  return new Promise(resolve => canvasEl.toBlob(resolve, 'image/jpeg', quality));
}

function stopCamera(stream) {
  if (stream) stream.getTracks().forEach(t => t.stop());
}
