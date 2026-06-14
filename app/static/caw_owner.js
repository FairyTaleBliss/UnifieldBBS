function escapeCawHtml(value) {
  return String(value || '').replace(/[&<>"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[char]));
}

function formatCawValue(value, fallback = 'pending') {
  return escapeCawHtml(value || fallback);
}

function renderCawPairResult(card, result) {
  const target = card.querySelector('.caw-pair-result');
  if (!target) return;
  if (!result || result.error) {
    target.textContent = result && result.error ? result.error : 'CAW pairing failed.';
    return;
  }
  const binding = result.binding || result.pairing || {};
  const status = binding.pair_status || (result.connected ? 'paired' : 'pending');
  const code = binding.pair_code || (result.pairing && result.pairing.pair_code) || '';
  const message = result.message || '';
  if (!binding && message) {
    target.textContent = message;
    return;
  }

  const instruction = status === 'paired'
    ? 'Pairing complete. This browser session is now CAW owner-linked.'
    : code
      ? 'Open the Cobo Agentic Wallet App and enter this code, then check status.'
      : 'Waiting for CAW pairing information. Try checking status again.';

  target.innerHTML = `
    <dl class="audit-list compact-audit">
      <div><dt>Status</dt><dd>${formatCawValue(status)}</dd></div>
      <div><dt>Pair code</dt><dd class="pair-code">${code ? escapeCawHtml(code) : 'not issued'}</dd></div>
      <div><dt>Wallet</dt><dd>${formatCawValue(binding.caw_wallet_address, 'created wallet')}</dd></div>
      <div><dt>Wallet ID</dt><dd>${formatCawValue(binding.caw_wallet_id)}</dd></div>
    </dl>
    <p class="lead">${instruction}</p>
  `;
}

async function callCawPairing(url, card, options = {}) {
  const resultBox = card.querySelector('.caw-pair-result');
  if (resultBox) resultBox.textContent = options.loadingText || 'Checking CAW pairing...';
  try {
    const response = await fetch(url, { method: options.method || 'GET' });
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || 'CAW pairing request failed');
    renderCawPairResult(card, result);
  } catch (error) {
    renderCawPairResult(card, { error: error.message });
  }
}

window.addEventListener('load', () => {
  document.querySelectorAll('.caw-owner-card').forEach((card) => {
    const start = card.querySelector('.caw-pair-start');
    const refresh = card.querySelector('.caw-pair-refresh');
    if (start) start.addEventListener('click', () => callCawPairing('/api/caw/pairing/start', card, { method: 'POST', loadingText: 'Creating a fresh CAW MPC wallet and secure pairing code...' }));
    if (refresh) refresh.addEventListener('click', () => callCawPairing('/api/caw/pairing/status', card, { loadingText: 'Checking pairing status...' }));
  });
});
