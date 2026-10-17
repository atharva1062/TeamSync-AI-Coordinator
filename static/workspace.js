let changed = false;
document.addEventListener('input', () => { changed = true; });
if (document.querySelector('[data-live-workspace]')) {
  let version;
  const poll = async () => {
    if (changed || document.hidden || document.querySelector('details[open]')) return;
    try {
      const response = await fetch('/workspace/version', {cache: 'no-store'});
      if (!response.ok) return;
      const next = JSON.stringify((await response.json()).version);
      if (version !== undefined && version !== next) location.reload();
      version = next;
    } catch (_) { /* Keep the current board visible during network interruptions. */ }
  };
  poll();
  setInterval(poll, 10000);
}
