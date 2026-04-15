(function() {
  // Don't show on the dashboard itself (port 3000)
  if (location.port === '3000') return;

  var btn = document.createElement('button');
  btn.id = 'pi-dash-home';
  btn.textContent = '\u2302';  // ⌂
  btn.title = 'Back to Dashboard';
  btn.addEventListener('click', function() {
    // Cache-buster forces a fresh load so stats aren't stale
    window.location.href = 'http://localhost:3000/?t=' + Date.now();
  });
  document.body.appendChild(btn);
})();
