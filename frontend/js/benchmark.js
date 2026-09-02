/* ============================================================
   GPQA Diamond — анимированный график пробития уровня эксперта
   Ступенчатая линия рисуется слева направо при попадании слайда
   в зону видимости; пробитие уровня эксперта подсвечено золотом.
   Данные: epoch.ai/benchmarks/gpqa-diamond (стилизовано,
   ключевые точки по тезису: o1 — 77,3% (сен 2024),
   Gemini 3.1 Pro — 94,3% (фев 2026))
   ============================================================ */

(function () {
  var host = document.getElementById('benchChart');
  if (!host) return;

  var EXPERT = 69.7;  // средний результат экспертов с PhD
  var RANDOM = 25;    // случайный ответ

  var DATA = [
    { d: '2023-03-14', v: 36,   name: 'GPT-4' },
    { d: '2023-11-06', v: 43,   name: 'GPT-4 Turbo' },
    { d: '2024-05-13', v: 53,   name: 'GPT-4o', below: true, dy: 26 },
    { d: '2024-06-20', v: 59,   name: 'Claude 3.5 Sonnet', below: true, anchor: 'start', dy: 26 },
    { d: '2024-09-12', v: 77.3, name: 'o1', cross: true, anchor: 'end', dx: -16 },
    { d: '2025-01-20', v: 79,   name: 'DeepSeek R1', below: true, anchor: 'start', dy: 30 },
    { d: '2025-02-24', v: 82,   name: 'Claude 3.7 Sonnet', dy: -20 },
    { d: '2025-03-25', v: 84,   name: 'Gemini 2.5 Pro', below: true, anchor: 'end', dx: -14 },
    { d: '2025-07-09', v: 88,   name: 'Grok 4', dy: -20 },
    { d: '2026-02-15', v: 94.3, name: 'Gemini 3.1 Pro', end: true }
  ];

  var W = 1000, H = 420;
  var ML = 56, MR = 28, MT = 34, MB = 42;
  var PW = W - ML - MR, PH = H - MT - MB;

  var T0 = Date.UTC(2023, 0, 1), T1 = Date.UTC(2026, 5, 1);
  /* вертикальная подпись оси Y */
  function X(dateStr) { return ML + (Date.parse(dateStr + 'T00:00:00Z') - T0) / (T1 - T0) * PW; }
  var YMIN = 20, YMAX = 100;
  function Y(v) { return MT + (1 - (v - YMIN) / (YMAX - YMIN)) * PH; }

  /* --- сетка и оси --- */
  var svg = '';
  svg += '<defs><clipPath id="benchClip"><rect id="benchClipRect" x="' + ML + '" y="0" width="0" height="' + H + '"/></clipPath></defs>';

  [20, 40, 60, 80, 100].forEach(function (g) {
    svg += '<line class="bench-grid' + (g === YMIN ? ' bench-grid--zero' : '') + '" x1="' + ML + '" x2="' + (W - MR) + '" y1="' + Y(g) + '" y2="' + Y(g) + '"/>';
    svg += '<text class="bench-axis" x="' + (ML - 10) + '" y="' + (Y(g) + 4) + '" text-anchor="end">' + g + '%</text>';
  });

  ['2023-01-01', '2023-07-01', '2024-01-01', '2024-07-01', '2025-01-01', '2025-07-01', '2026-01-01'].forEach(function (dt) {
    var x = X(dt);
    var lab = (dt.slice(5, 7) === '01' ? 'Янв' : 'Июл') + ' ' + dt.slice(0, 4);
    svg += '<line class="bench-grid bench-grid--v" x1="' + x + '" x2="' + x + '" y1="' + MT + '" y2="' + (H - MB) + '"/>';
    svg += '<text class="bench-axis" x="' + x + '" y="' + (H - MB + 22) + '" text-anchor="middle">' + lab + '</text>';
  });

  /* --- реперные линии --- */
  svg += '<line class="bench-ref bench-ref--random" x1="' + ML + '" x2="' + (W - MR) + '" y1="' + Y(RANDOM) + '" y2="' + Y(RANDOM) + '"/>';
  svg += '<text class="bench-ref-label bench-ref-label--random" x="' + (ML + 8) + '" y="' + (Y(RANDOM) - 8) + '">Случайный ответ — 25%</text>';

  svg += '<line class="bench-ref bench-ref--expert" x1="' + ML + '" x2="' + (W - MR) + '" y1="' + Y(EXPERT) + '" y2="' + Y(EXPERT) + '"/>';
  svg += '<text class="bench-ref-label bench-ref-label--expert" x="' + (ML + 8) + '" y="' + (Y(EXPERT) - 8) + '">Эксперты с PhD — 69,7%</text>';

  /* --- ступенчатая линия --- */
  var dPath = 'M ' + X(DATA[0].d) + ' ' + Y(DATA[0].v);
  for (var i = 1; i < DATA.length; i++) {
    dPath += ' H ' + X(DATA[i].d) + ' V ' + Y(DATA[i].v);
  }
  dPath += ' H ' + (W - MR);
  svg += '<path class="bench-line" clip-path="url(#benchClip)" d="' + dPath + '"/>';

  /* --- точки и подписи --- */
  function fmt(v) { return String(v).replace('.', ','); }

  DATA.forEach(function (p) {
    var cx = X(p.d), cy = Y(p.v);
    svg += '<circle class="bench-pt' + (p.cross ? ' bench-pt--cross' : '') + '" data-x="' + cx + '" cx="' + cx + '" cy="' + cy + '" r="4.5"/>';
    var anchor = p.end ? 'end' : (p.anchor || 'middle');
    var ty = p.below ? cy + (p.dy || 24) : cy - 14 + (p.dy && !p.below ? p.dy + 14 : 0);
    var tx = cx + (p.anchor === 'start' ? 12 : 0) + (p.dx || 0);
    svg += '<text class="bench-lab' + (p.cross || p.end ? ' bench-lab--key' : '') + '" data-x="' + cx + '" x="' + tx + '" y="' + ty + '" text-anchor="' + anchor + '">' +
      p.name + ' <tspan class="v">' + fmt(p.v) + '%</tspan></text>';
  });

  /* --- маркер пробития: на пересечении линии и пунктира эксперта --- */
  var cross = null;
  for (var j = 0; j < DATA.length; j++) {
    if (DATA[j].cross) { cross = DATA[j]; break; }
  }
  if (cross) {
    var kx = X(cross.d), ky = Y(EXPERT);
    svg += '<g id="benchCross" class="bench-cross-g" opacity="0">' +
      '<circle class="bench-pulse" cx="' + kx + '" cy="' + ky + '" r="7"/>' +
      '<circle class="bench-cross" cx="' + kx + '" cy="' + ky + '" r="6"/>' +
      '<text class="bench-cross-lab" x="' + (kx - 14) + '" y="' + (ky + 26) + '" text-anchor="end">сен 2024 — первое пробитие</text>' +
      '</g>';
  }

  host.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="GPQA Diamond: рост результатов ИИ-моделей выше уровня экспертов с PhD">' + svg + '</svg>';

  /* --- анимация --- */
  var rect = host.querySelector('#benchClipRect');
  var marks = Array.prototype.slice.call(host.querySelectorAll('[data-x]'));
  var crossG = host.querySelector('#benchCross');
  var crossX = cross ? X(cross.d) : Infinity;
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function finishInstantly() {
    rect.setAttribute('width', PW);
    marks.forEach(function (m) { m.classList.add('on'); });
    if (crossG) crossG.setAttribute('opacity', '1');
  }

  function easeInOut(t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }

  function animate() {
    if (reduced) { finishInstantly(); return; }
    var DUR = 4200, start = null, crossShown = false;
    function frame(now) {
      if (!start) start = now;
      var t = Math.min(1, (now - start) / DUR);
      var curX = ML + easeInOut(t) * PW;
      rect.setAttribute('width', Math.max(0, curX - ML));
      marks.forEach(function (m) {
        if (parseFloat(m.getAttribute('data-x')) <= curX + 0.5) m.classList.add('on');
      });
      if (!crossShown && curX >= crossX && crossG) {
        crossG.setAttribute('opacity', '1');
        crossShown = true;
      }
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  var started = false;
  function start() {
    if (started) return;
    started = true;
    animate();
  }

  function inViewport() {
    var r = host.getBoundingClientRect();
    return r.top < window.innerHeight && r.bottom > 0;
  }

  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { io.disconnect(); start(); }
      });
    }, { threshold: 0.35 });
    io.observe(host);
    /* запасной путь: график уже виден при загрузке */
    window.addEventListener('load', function () { if (inViewport()) start(); });
    if (document.readyState !== 'loading' && inViewport()) start();
  } else {
    start();
  }
})();
