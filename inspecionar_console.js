/* ===================================================================
   COLE ISTO NO CONSOLE do DevTools (F12 > aba "Console") enquanto
   estiver na tela que quero mapear. Ele copia o resultado para a
   area de transferencia (Ctrl+V para colar de volta pra mim).
   Se o console pedir, digite  allow pasting  e tecle Enter primeiro.
   =================================================================== */
(() => {
  const root = document.querySelector(
    '[role=dialog], mat-dialog-container, .mat-dialog-container, .modal.show, ' +
    '.modal[style*="display: block"], .p-dialog, .swal2-popup, .ui-dialog'
  ) || document.body;
  const vis = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const sel = el => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const at = n => (el.getAttribute && el.getAttribute(n)) || '';
    ['name', 'type', 'formcontrolname', 'ng-reflect-name', 'placeholder', 'title', 'aria-label']
      .forEach(n => { const v = at(n); if (v) s += ` [${n}="${v}"]`; });
    const cls = (typeof el.className === 'string' ? el.className : '').trim();
    if (cls) s += ' .' + cls.split(/\s+/).slice(0, 6).join('.');
    const checked = el.matches && el.matches('input[type=checkbox],input[type=radio]') ? ` checked=${el.checked}` : '';
    const val = (el.value != null && el.value !== '') ? ` value="${String(el.value).slice(0,40)}"` : '';
    const txt = (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    return s + checked + val + (txt ? ` >> "${txt}"` : '');
  };
  const lines = [];
  lines.push('### RAIZ: ' + (root === document.body ? 'PAGINA INTEIRA' : 'MODAL/DIALOG') + ' ###');
  lines.push('### URL: ' + location.href + ' ###');
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let n; const saldos = [];
  while (n = walker.nextNode()) {
    const t = n.textContent.trim();
    if (/saldo|aprova/i.test(t) && t.length < 140) saldos.push(t);
  }
  if (saldos.length) { lines.push('--- textos com "saldo/aprova" ---'); saldos.forEach(s => lines.push('  ' + s)); }
  lines.push('--- campos e botoes visiveis ---');
  [...root.querySelectorAll(
    'input,select,textarea,button,a[href],[role=button],[role=checkbox],' +
    'mat-checkbox,mat-slide-toggle,label,th'
  )].filter(vis).forEach(el => lines.push('  ' + sel(el)));
  const out = lines.join('\n');
  try { copy(out); console.log('%c>> COPIADO! Cole (Ctrl+V) de volta pro Claude.', 'color:green;font-weight:bold'); }
  catch (e) { console.log('nao consegui copiar automatico; selecione o texto abaixo:'); }
  return out;
})();
