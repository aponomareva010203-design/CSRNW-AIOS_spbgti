/* ============================================================
   Materials Project — рост базы по пятилеткам и вклад GNoME
   Классический конвейер (DFT по экспериментальным структурам)
   vs предсказания GNoME (DeepMind). Столбцы вырастают снизу
   при попадании слайда в зону видимости.
   Источники: matsci.org; Dagdelen et al. 2017; MP 2021;
   Merchant et al., Nature (2023); Berkeley Lab News, 2026
   ============================================================ */

(function () {
  var host = document.getElementById('gnomeChart');
  if (!host) return;

  /* материалов в базе, тыс. */
  var DATA = [
    { y: '2011', sub: 'запуск',      ext: 12,  gnome: 0,   note: null },
    { y: '2016', sub: null,          ext: 67,  gnome: 0,   note: '+55 тыс. за 5 лет' },
    { y: '2021', sub: null,          ext: 130, gnome: 0,   note: '+63 тыс. за 5 лет' },
    { y: '2026', sub: 'интеграция',  ext: 155, gnome: 400, note: null }
  ];

  var W = 1000, H = 540;
  var ML = 78, MR = 30, MT = 92, MB = 72;
  var PW = W - ML - MR, PH = H - MT - MB;
  var YMAX = 600;

  function Y(v) { return MT + (1 - v / YMAX) * PH; }
  function BH(v) { return v / YMAX * PH; }

  var BW = 108;
  var step = PW / DATA.length;

  var svg = '';

  /* --- сетка и ось Y --- */
  [0, 100, 200, 300, 400, 500, 600].forEach(function (g) {
    svg += '<line class="bench-grid' + (g === 0 ? ' bench-grid--zero' : '') + '" x1="' + ML + '" x2="' + (W - MR) + '" y1="' + Y(g) + '" y2="' + Y(g) + '"/>';
    svg += '<text class="bench-axis" x="' + (ML - 12) + '" y="' + (Y(g) + 5) + '" text-anchor="end">' + g + '</text>';
  });
  svg += '<text class="bench-axis bench-axis--unit" x="' + ML + '" y="' + (MT - 8) + '" text-anchor="start">тыс. материалов в базе</text>';

  /* --- столбцы --- */
  DATA.forEach(function (d, i) {
    var cx = ML + step * i + step / 2;
    var x0 = cx - BW / 2;

    svg += '<rect class="gn-bar gn-bar--ext" data-i="' + i + '" x="' + x0 + '" width="' + BW + '" y="' + Y(d.ext) + '" height="' + BH(d.ext) + '" rx="6"/>';

    if (d.gnome > 0) {
      var yTop = Y(d.ext + d.gnome);
      svg += '<rect class="gn-bar gn-bar--gnome" data-i="' + i + '" x="' + x0 + '" width="' + BW + '" y="' + yTop + '" height="' + BH(d.gnome) + '" rx="6"/>';
      /* подпись вклада внутри зелёного блока */
      var my = yTop + BH(d.gnome) * 0.5;
      svg += '<text class="gn-in" data-i="' + i + '" x="' + cx + '" y="' + (my - 30) + '" text-anchor="middle">+400 тыс.</text>' +
             '<text class="gn-in gn-in--sm" data-i="' + i + '" x="' + cx + '" y="' + (my - 4) + '" text-anchor="middle">предсказания</text>' +
             '<text class="gn-in gn-in--sm" data-i="' + i + '" x="' + cx + '" y="' + (my + 22) + '" text-anchor="middle">GNoME · 2023</text>';
    }

    /* год */
    svg += '<text class="bench-axis gn-year" x="' + cx + '" y="' + (H - MB + 30) + '" text-anchor="middle">' + d.y + '</text>';
    if (d.sub) {
      svg += '<text class="bench-axis gn-sub" x="' + cx + '" y="' + (H - MB + 50) + '" text-anchor="middle">' + d.sub + '</text>';
    }

    /* итоговое значение над столбцом */
    var tot = d.ext + d.gnome;
    svg += '<text class="gn-val' + (d.gnome ? ' gn-val--total' : '') + '" data-i="' + i + '" x="' + cx + '" y="' + (Y(tot) - 14) + '" text-anchor="middle">' +
      (i === 0 ? '~12 тыс.' : i === 1 ? '~67 тыс.' : i === 2 ? '130 тыс.' : '~555 тыс.') + '</text>';

    /* аннотация прироста между столбцами */
    if (d.note) {
      var nx = cx - step / 2, ny = Y(tot) - 6;
      svg += '<text class="gn-note" data-i="' + i + '" x="' + nx + '" y="' + ny + '" text-anchor="middle">' + d.note + '</text>';
    }
  });

  /* --- легенда --- */
  var lx = ML + 4, ly = MT - 40;
  svg += '<g class="gn-legend">' +
    '<rect x="' + lx + '" y="' + (ly - 12) + '" width="16" height="16" rx="4" class="gn-bar--ext gn-swatch"/>' +
    '<text class="bench-axis gn-legend-t" x="' + (lx + 24) + '" y="' + ly + '">Классический конвейер: DFT по экспериментальным структурам</text>' +
    '<rect x="' + (lx + 480) + '" y="' + (ly - 12) + '" width="16" height="16" rx="4" class="gn-bar--gnome gn-swatch"/>' +
    '<text class="bench-axis gn-legend-t" x="' + (lx + 504) + '" y="' + ly + '">Вклад ИИ: GNoME (DeepMind)</text>' +
    '</g>';

  /* --- пауза 2023 → 2026: дуга от зелёного блока к подписи --- */
  (function () {
    var cx26 = ML + step * 3 + step / 2;
    var yTop26 = Y(DATA[3].ext + DATA[3].gnome);
    var tx = cx26 - BW / 2 - 20, ty = yTop26 + 26;
    svg += '<g class="gn-ann" data-i="3">' +
      '<line class="gn-ann-line" x1="' + (cx26 - BW / 2 - 6) + '" x2="' + (tx + 4) + '" y1="' + (yTop26 + 12) + '" y2="' + (ty + 10) + '"/>' +
      '<text class="gn-ann-text gn-ann-text--gold" x="' + tx + '" y="' + ty + '" text-anchor="end">созданы одним ИИ-проектом в 2023</text>' +
      '<text class="gn-ann-text gn-ann-text--gold" x="' + tx + '" y="' + (ty + 20) + '" text-anchor="end">приняты базой в 2026</text>' +
      '</g>';
  })();

  host.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Рост базы Materials Project и вклад GNoME">' + svg + '</svg>';

  /* --- анимация: столбцы вырастают снизу --- */
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  if (reduced) {
    host.querySelectorAll('.gn-val, .gn-in, .gn-note, .gn-ann').forEach(function (el) { el.classList.add('on'); });
    return;
  }

  host.querySelectorAll('.gn-bar').forEach(function (bar) {
    var y = parseFloat(bar.getAttribute('y')), h = parseFloat(bar.getAttribute('height'));
    bar.dataset.finalY = y;
    bar.dataset.finalH = h;
    bar.setAttribute('y', Y(0));
    bar.setAttribute('height', '0');
  });

  function animate() {
    var bars = Array.prototype.slice.call(host.querySelectorAll('.gn-bar'));
    bars.forEach(function (bar, idx) {
      setTimeout(function () {
        bar.classList.add('grow');
        bar.setAttribute('y', bar.dataset.finalY);
        bar.setAttribute('height', bar.dataset.finalH);
      }, 220 * idx);
    });
    setTimeout(function () {
      host.querySelectorAll('.gn-val, .gn-in').forEach(function (el) { el.classList.add('on'); });
    }, 220 * bars.length + 250);
    setTimeout(function () {
      host.querySelectorAll('.gn-note, .gn-ann').forEach(function (el) { el.classList.add('on'); });
    }, 220 * bars.length + 650);
  }

  var started = false;
  function start() {
    if (started) return;
    started = true;
    animate();
  }

  function inViewport() {
    var r = host.getBoundingClientRect();
    return r.top < window.innerHeight && r.bottom > 0 && r.left < window.innerWidth && r.right > 0;
  }

  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { io.disconnect(); start(); }
      });
    }, { threshold: 0.3 });
    io.observe(host);
    window.addEventListener('load', function () { if (inViewport()) start(); });
    if (document.readyState !== 'loading' && inViewport()) start();
  } else {
    start();
  }
})();
