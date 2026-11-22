(function () {
  var t = null; try { t = localStorage.getItem('theme'); } catch (e) {}
  var dark = t === 'dark' || (t !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  return { theme: dark ? 'dark' : 'default' };
})()
