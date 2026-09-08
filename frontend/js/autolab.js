/* ============================================================
   GPT-5 × Ginkgo Bioworks — минимальная удельная стоимость
   бесклеточного синтеза белка ($/г, лог. шкала) по раундам
   автономной оптимизации. По мотивам панели C, фиг. 2.
   Линия рисуется слева направо при попадании слайда
   в зону видимости; точки появляются последовательно.
   ============================================================ */

(function () {
  var host = document.getElementById('autolabChart');
  if (!host) return;

  /* shape: diamond — эвристическая оценка, circle — только валидация,
     triangle — оценка по рубрике */
  var DATA = [
    { step: 0, v: 120000, shape: 'diamond',  tools: false, star: '*'  },
    { step: 1, v: 110000, shape: 'diamond',  tools: false, star: '**' },
    { step: 2, v: 55000,  shape: 'diamond',  tools: false, star: ''   },
    { step: 3, v: 12000,  shape: 'circle',   tools: true,  star: ''   },
    { step: 4, v: 7000,   shape: 'circle',   tools: true,  star: ''   },
    { step: 5, v: 1100,   shape: 'triangle', tools: true,  star: ''   }
  ];
  var SOTA = 8000; /* лучший опубликованный результат, $/г */

  var W = 1000, H = 520;
  var ML = 96, MR = 44, MT = 74, MB = 74;
  var PW = W - ML - MR, PH = H - MT - MB;
  var LOG_MIN = 3, LOG_MAX = 5.2; /* 10^3 … 10^5.2 */

  function Y(v) { return MT + (1 - (Math.log10(v) - LOG_MIN) / (LOG_MAX - LOG_MIN)) * PH; }
  function X(i) { return ML + (i + 0.5) / DATA.length * PW; }

  var svg = '';
  svg += '<defs><clipPath id="alClip"><rect id="alClipRect" x="' + ML + '" y="0" width="0" height="' + H + '"/></clipPath></defs>';

  /* --- зоны --- */
  var xSplit = ML + 2.5 / DATA.length * PW;
  var zoneLabelY = H - MB - 14;
  svg += '<rect class="al-zone" x="' + ML + '" y="' + MT + '" width="' + (xSplit - ML) + '" height="' + PH + '"/>' +
         '<text class="al-zone-t" x="' + (ML + 16) + '" y="' + zoneLabelY + '">Без инструментов</text>';
  svg += '<rect class="al-zone al-zone--tools" x="' + xSplit + '" y="' + MT + '" width="' + (W - MR - xSplit) + '" height="' + PH + '"/>' +
         '<text class="al-zone-t" x="' + (xSplit + 18) + '" y="' + (zoneLabelY - 20) + '">Инструменты: компьютер,</text>' +
         '<text class="al-zone-t" x="' + (xSplit + 18) + '" y="' + zoneLabelY + '">SOTA-статья, доступ в интернет</text>';

  /* --- сетка и ось Y (лог) --- */
  [1000, 10000, 100000].forEach(function (g) {
    svg += '<line class="bench-grid' + (g === 1000 ? ' bench-grid--zero' : '') + '" x1="' + ML + '" x2="' + (W - MR) + '" y1="' + Y(g) + '" y2="' + Y(g) + '"/>';
    svg += '<text class="bench-axis" x="' + (ML - 12) + '" y="' + (Y(g) + 5) + '" text-anchor="end">$' + (g / 1000) + 'K</text>';
  });
  svg += '<text class="bench-axis bench-axis--unit" x="' + ML + '" y="' + (MT - 48) + '" text-anchor="start">минимальная стоимость грамма белка, $ · лог. шкала · приблизительно, по графику статьи</text>';

  /* --- линия SOTA --- */
  svg += '<line class="bench-ref bench-ref--expert" x1="' + ML + '" x2="' + (W - MR) + '" y1="' + Y(SOTA) + '" y2="' + Y(SOTA) + '"/>' +
         '<text class="bench-ref-label bench-ref-label--expert" x="' + (ML + 8) + '" y="' + (Y(SOTA) - 10) + '">Лучший опубликованный результат (SOTA)</text>';

  /* --- линия по раундам --- */
  var dPath = 'M ' + X(0) + ' ' + Y(DATA[0].v);
  for (var i = 1; i < DATA.length; i++) {
    dPath += ' L ' + X(i) + ' ' + Y(DATA[i].v);
  }
  svg += '<path class="bench-line al-line" clip-path="url(#alClip)" d="' + dPath + '"/>';

  /* --- точки --- */
  DATA.forEach(function (d, i) {
    var cx = X(i), cy = Y(d.v);
    var cls = 'al-pt al-pt--' + d.shape;
    if (d.shape === 'diamond') {
      svg += '<rect class="' + cls + '" data-x="' + cx + '" x="' + (cx - 7) + '" y="' + (cy - 7) + '" width="14" height="14" transform="rotate(45 ' + cx + ' ' + cy + ')"/>';
    } else if (d.shape === 'circle') {
      svg += '<circle class="' + cls + '" data-x="' + cx + '" cx="' + cx + '" cy="' + cy + '" r="8"/>';
    } else {
      svg += '<path class="' + cls + '" data-x="' + cx + '" d="M ' + cx + ' ' + (cy + 8) + ' L ' + (cx - 8) + ' ' + (cy - 6) + ' L ' + (cx + 8) + ' ' + (cy - 6) + ' Z"/>';
    }
    /* номер раунда */
    svg += '<text class="bench-axis al-step" x="' + cx + '" y="' + (H - MB + 28) + '" text-anchor="middle">' + (d.step === 0 ? 'старт' : 'раунд ' + d.step) + '</text>';
    /* значение (в тексте статьи не указаны — приблизительно, по панели C) */
    svg += '<text class="al-val" data-x="' + cx + '" x="' + cx + '" y="' + (cy - 18) + '" text-anchor="middle">≈$' +
      (d.v >= 10000 ? Math.round(d.v / 1000) + 'K' : d.v.toLocaleString('ru-RU')) + '</text>';
    if (d.star) {
      svg += '<text class="al-star" data-x="' + cx + '" x="' + cx + '" y="' + (cy - 34) + '" text-anchor="middle">' + d.star + '</text>';
    }
  });

  /* --- подписи к звёздочкам --- */
  svg += '<text class="al-star-note" data-x="' + X(0) + '" x="' + (ML + 8) + '" y="' + (Y(55000) + 40) + '" text-anchor="start">* модель предлагает новые реагенты</text>' +
         '<text class="al-star-note" data-x="' + X(1) + '" x="' + (ML + 8) + '" y="' + (Y(55000) + 64) + '" text-anchor="start">** модель проектирует с новыми реагентами</text>';

  /* --- легенда форм: как модель отбирала гипотезы для эксперимента --- */
  var lx = ML + 180, ly = MT - 20;
  svg += '<g class="gn-legend">' +
    '<text class="bench-axis gn-legend-t" x="' + lx + '" y="' + ly + '" font-weight="700">Отбор гипотез:</text>' +
    '<rect class="al-pt al-pt--diamond al-swatch" x="' + (lx + 106) + '" y="' + (ly - 13) + '" width="13" height="13" transform="rotate(45 ' + (lx + 112.5) + ' ' + (ly - 6.5) + ')"/>' +
    '<text class="bench-axis gn-legend-t" x="' + (lx + 128) + '" y="' + ly + '">ИИ-экспертом</text>' +
    '<circle class="al-pt al-pt--circle al-swatch" cx="' + (lx + 252) + '" cy="' + (ly - 6) + '" r="7"/>' +
    '<text class="bench-axis gn-legend-t" x="' + (lx + 268) + '" y="' + ly + '">проверкой на корректность</text>' +
    '<path class="al-pt al-pt--triangle al-swatch" d="M ' + (lx + 472) + ' ' + (ly) + ' L ' + (lx + 464) + ' ' + (ly - 14) + ' L ' + (lx + 480) + ' ' + (ly - 14) + ' Z"/>' +
    '<text class="bench-axis gn-legend-t" x="' + (lx + 490) + '" y="' + ly + '">по критериям</text>' +
    '</g>';

  host.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Снижение стоимости синтеза белка по раундам автономной оптимизации">' + svg + '</svg>';

  /* --- анимация --- */
  var rect = host.querySelector('#alClipRect');
  var marks = Array.prototype.slice.call(host.querySelectorAll('[data-x]'));
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function finishInstantly() {
    rect.setAttribute('width', PW);
    marks.forEach(function (m) { m.classList.add('on'); });
  }

  function easeInOut(t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }

  function animate() {
    var DUR = 3600, start = null;
    function frame(now) {
      if (!start) start = now;
      var t = Math.min(1, (now - start) / DUR);
      var curX = ML + easeInOut(t) * PW;
      rect.setAttribute('width', Math.max(0, curX - ML));
      marks.forEach(function (m) {
        if (parseFloat(m.getAttribute('data-x')) <= curX + 0.5) m.classList.add('on');
      });
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  if (reduced) { finishInstantly(); return; }

  /* подготовка: скрыть точки и подписи */
  marks.forEach(function (m) { m.classList.remove('on'); });

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
