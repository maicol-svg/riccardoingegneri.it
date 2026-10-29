// Parti comuni a tutte le pagine (lo stile è in sito.css):
// menu che segna la pagina corrente e si stringe quando scendi, entrate morbide allo scroll,
// evidenziatore e caselle che si spuntano, barra dei passi nei moduli, timbro quando il modulo è partito.
(function () {
  var doc = document.documentElement;
  var calmo = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  doc.classList.add('js');

  // Menu
  var barra = document.querySelector('.barra-sito');
  if (barra) {
    var qui = location.pathname.replace(/index\.html$/, '');
    barra.querySelectorAll('ul a').forEach(function (a) {
      if (qui.indexOf(a.getAttribute('href')) === 0) a.setAttribute('aria-current', 'page');
    });
    // scendendo si stringe (non sparisce); torna pieno solo vicino alla cima. Due soglie diverse: niente sfarfallio.
    var inAttesa = false;
    function muovi() {
      inAttesa = false;
      var y = scrollY, alto = doc.scrollHeight - innerHeight;
      barra.style.setProperty('--p', alto > 0 ? Math.min(1, y / alto).toFixed(4) : 0);
      if (y > 90) barra.classList.add('stretto');
      else if (y < 30) barra.classList.remove('stretto');
    }
    addEventListener('scroll', function () { if (!inAttesa) { inAttesa = true; requestAnimationFrame(muovi); } }, { passive: true });
    muovi();
  }

  // Entrate allo scroll: blocchi, schede, voci di elenco. Chi è vicino entra un po' dopo l'altro.
  var vedi = 'IntersectionObserver' in window ? new IntersectionObserver(function (voci) {
    voci.forEach(function (v) {
      if (!v.isIntersecting) return;
      v.target.classList.add('visto');
      vedi.unobserve(v.target);
    });
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0.1 }) : null;
  function osserva(el) { if (vedi) vedi.observe(el); else el.classList.add('visto'); }

  var ENTRANO = 'main h2, main .porta, main .servizio, main .assaggio, main li, main .sheet, main .form-sheet, main details, ' +
    'main .racconto p, main .incerto, main .about-grid > *, main .blocco > p, main .legge, main .closing, main .cta, [data-entra]';
  var gruppi = new Map();
  document.querySelectorAll(ENTRANO).forEach(function (el) {
    if (el.closest('form, .grazie, .barra-sito')) return;
    var n = gruppi.get(el.parentNode) || 0;
    gruppi.set(el.parentNode, n + 1);
    el.style.setProperty('--i', Math.min(n, 5));
    el.classList.add('entra');
    osserva(el);
  });
  document.querySelectorAll('.segno').forEach(osserva);

  // Moduli con data-passi: ogni campo [data-passo] fatto riceve la sua spunta e riempie la barra.
  var EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
  document.querySelectorAll('form[data-passi]').forEach(function (form) {
    var passi = [].slice.call(form.querySelectorAll('[data-passo]'));
    var box = document.createElement('div');
    box.className = 'tappe';
    box.innerHTML = '<div class="tappe-barra"><i></i></div><p class="tappe-testo" aria-live="polite"></p>';
    form.insertBefore(box, form.firstChild);
    var testo = box.querySelector('.tappe-testo'), riga = box.querySelector('i');
    var invia = form.querySelector('[type=submit]');
    var mancavano = -1;
    function fatto(el) {
      if (el.tagName === 'FIELDSET') return !!el.querySelector('input:checked');
      if (el.type === 'checkbox') return el.checked;
      if (el.type === 'email') return EMAIL.test(el.value.trim());
      return el.value.trim().length > 1;
    }
    function conta() {
      var n = 0;
      passi.forEach(function (el) {
        var ok = fatto(el);
        if (ok) n++;
        var c = el.closest('.campo');
        if (c) c.classList.toggle('ok', ok);
      });
      var manca = passi.length - n;
      riga.style.transform = 'scaleX(' + (n / passi.length) + ')';
      testo.textContent = !manca ? 'Tutto pronto: premi il pulsante' : manca === 1 ? 'Manca un ultimo passo' : 'Ancora ' + manca + ' passi';
      box.classList.toggle('pronto', !manca);
      if (!manca && mancavano !== 0 && mancavano !== -1 && invia) {
        invia.classList.remove('invito');
        void invia.offsetWidth;
        invia.classList.add('invito');
      }
      mancavano = manca;
    }
    form.addEventListener('input', conta);
    form.addEventListener('change', conta);
    conta();
  });

  // Quando compare il ringraziamento: timbro e qualche tratto di evidenziatore.
  document.querySelectorAll('.grazie').forEach(function (g) {
    new MutationObserver(function () {
      if (g.hidden || g.querySelector('.timbro')) return;
      var t = document.createElement('span');
      t.className = 'timbro';
      t.setAttribute('aria-hidden', 'true');
      t.textContent = g.getAttribute('data-timbro') || 'Ricevuto';
      g.insertBefore(t, g.firstChild);
      if (calmo) return;
      for (var k = 0; k < 14; k++) {
        var c = document.createElement('i');
        c.className = 'coriandolo';
        c.style.setProperty('--a', (k * 360 / 14 + (k % 3) * 9) + 'deg');
        c.style.setProperty('--d', (80 + (k * 37) % 70) + 'px');
        t.appendChild(c);
      }
    }).observe(g, { attributes: true, attributeFilter: ['hidden'] });
  });
})();
