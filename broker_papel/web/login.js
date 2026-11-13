document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target));
  const r = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-BP': '1'}, body: JSON.stringify(d)});
  const j = await r.json();
  if (j.ok) location.href = '/'; else document.getElementById('err').textContent = j.error || 'Error';
});
