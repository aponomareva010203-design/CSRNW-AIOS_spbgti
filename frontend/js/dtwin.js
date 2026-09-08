/* ============================================================
   Слайд 4: пример AIOS с PINN — цифровой двойник батареи.
   Мотив — пятиуровневая схема Zhu et al. (arXiv:2509.02366,
   NTU × NVIDIA), перерисованная в стиле презентации:
   полосы-карточки уровней, rounded-плашки, палитра сайта.
   Уровни появляются по очереди снизу вверх.
   ============================================================ */

(function () {
  var host = document.getElementById('dtwDiagram');
  if (!host) return;

  var W = 1200, H = 424;
  var PAD_L = 108, GAP = 8;
  var X = PAD_L, RW = W - X - 8;

  /* уровни снизу вверх; tag — короткая подпись слева */
  var layers = [
    { tag: 'Сценарии', sub: 'объекты', h: 52 },
    { tag: 'Данные', sub: 'сбор и управление', h: 76 },
    { tag: 'Модели', sub: 'физика + ИИ', h: 146 },
    { tag: 'Приложения', sub: 'решения', h: 52 },
    { tag: 'Визуализация', sub: 'среда', h: 52 }
  ];

  var yCursor = H - 6;
  layers.forEach(function (l) {
    yCursor -= l.h;
    l.y = yCursor;
    yCursor -= GAP;
  });

  var svg = '';
  svg += '<defs>' +
    '<marker id="dtwArr" markerWidth="10" markerHeight="10" refX="7" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 Z" fill="var(--green-deep)"/></marker>' +
    '<marker id="dtwArrD" markerWidth="10" markerHeight="10" refX="7" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 Z" fill="var(--gold)"/></marker>' +
    '</defs>';

  function pill(x, y, w, h, text, cls, opts) {
    opts = opts || {};
    var s = '<rect class="dtw-pill ' + (cls || '') + '" x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="' + (h / 2) + '"/>';
    s += '<text class="dtw-t' + (opts.tcls ? ' ' + opts.tcls : '') + '" x="' + (x + w / 2) + '" y="' + (y + h / 2 + 4.5) + '" text-anchor="middle">' + text + '</text>';
    return s;
  }

  /* ================= Сценарии ================= */
  var sc = layers[0];
  svg += '<g class="dtw-layer" data-layer="0">';
  svg += '<rect class="dtw-band dtw-band--gold" x="' + X + '" y="' + sc.y + '" width="' + RW + '" height="' + sc.h + '" rx="14"/>';
  var scen = ['Электротранспорт', 'Станции хранения', 'Зарядные станции', 'Аккумуляторные блоки', 'Энергосистемы'];
  var cw = (RW - 36 - (scen.length - 1) * 10) / scen.length;
  scen.forEach(function (t, i) {
    svg += pill(X + 18 + i * (cw + 10), sc.y + 11, cw, 30, t, 'dtw-pill--gold');
  });
  svg += '</g>';

  /* стрелки сценарии <-> данные */
  var da = layers[1];
  var midY1 = da.y + da.h, midY2 = sc.y;
  svg += '<g class="dtw-layer" data-layer="0">' +
    '<line class="dtw-arrow" x1="' + (X + 420) + '" x2="' + (X + 420) + '" y1="' + (midY2 - 2) + '" y2="' + (midY1 + 2) + '" marker-end="url(#dtwArr)"/>' +
    '<text class="dtw-t dtw-t--flow" x="' + (X + 408) + '" y="' + ((midY1 + midY2) / 2 + 4) + '" text-anchor="end">сбор данных</text>' +
    '<line class="dtw-arrow dtw-arrow--gold" x1="' + (X + 640) + '" x2="' + (X + 640) + '" y1="' + (midY1 + 2) + '" y2="' + (midY2 - 2) + '" marker-end="url(#dtwArrD)"/>' +
    '<text class="dtw-t dtw-t--flow" x="' + (X + 652) + '" y="' + ((midY1 + midY2) / 2 + 4) + '" text-anchor="start">команды управления</text>' +
    '</g>';

  /* ================= Данные ================= */
  svg += '<g class="dtw-layer" data-layer="1">';
  svg += '<rect class="dtw-band dtw-band--green" x="' + X + '" y="' + da.y + '" width="' + RW + '" height="' + da.h + '" rx="14"/>';
  var proc = ['Обработка', 'Очистка', 'Контроль качества', 'Хранение', 'Управление'];
  var pw = (RW - 36 - (proc.length - 1) * 10) / proc.length;
  proc.forEach(function (t, i) {
    svg += pill(X + 18 + i * (pw + 10), da.y + 9, pw, 24, t, 'dtw-pill--green');
  });
  var dtypes = [
    ['Эксплуатационные', 'ток · напряжение · температура'],
    ['Среда', 'климат · вибрация · режимы'],
    ['Обслуживание', 'отказы · ремонты · замены'],
    ['Лабораторное старение', 'циклы · сопротивление']
  ];
  var dw = (RW - 36 - (dtypes.length - 1) * 10) / dtypes.length;
  dtypes.forEach(function (d, i) {
    var x = X + 18 + i * (dw + 10);
    svg += pill(x, da.y + 44, dw, 22, d[0] + ': ' + d[1], 'dtw-pill--white', { tcls: 'dtw-t--sub2' });
  });
  svg += '</g>';

  /* ================= Модели ================= */
  var mo = layers[2];
  svg += '<g class="dtw-layer" data-layer="2">';
  svg += '<rect class="dtw-band dtw-band--model" x="' + X + '" y="' + mo.y + '" width="' + RW + '" height="' + mo.h + '" rx="14"/>';
  var caps = ['Оценка заряда', 'Прогноз износа', 'Тепловое состояние', 'Предупреждение разгона', 'Оптимизация зарядки'];
  var cw2 = (RW - 36 - (caps.length - 1) * 10) / caps.length;
  caps.forEach(function (t, i) {
    svg += pill(X + 18 + i * (cw2 + 10), mo.y + 9, cw2, 24, t, 'dtw-pill--green');
  });
  var half = (RW - 36 - 16) / 2;
  var boxY = mo.y + 38, boxH = mo.h - 46;
  /* физика */
  svg += '<rect class="dtw-box" x="' + (X + 18) + '" y="' + boxY + '" width="' + half + '" height="' + boxH + '" rx="12"/>';
  svg += '<text class="dtw-t dtw-t--head" x="' + (X + 18 + half / 2) + '" y="' + (boxY + 20) + '" text-anchor="middle">Физические модели</text>';
  svg += '<text class="dtw-t dtw-t--sub" x="' + (X + 18 + half / 2) + '" y="' + (boxY + 35) + '" text-anchor="middle">электрохимия · теплоперенос</text>';
  var phys = ['Эквивалентные схемы', 'Электрохимические модели', 'Рост SEI-слоя', 'Тепловые поля 1D–3D'];
  var pw3 = (half - 28 - 10) / 2;
  phys.forEach(function (t, i) {
    svg += pill(X + 18 + 14 + (i % 2) * (pw3 + 10), boxY + 46 + Math.floor(i / 2) * 29, pw3, 25, t, 'dtw-pill--line');
  });
  /* ИИ */
  var ax = X + 18 + half + 16;
  svg += '<rect class="dtw-box dtw-box--ai" x="' + ax + '" y="' + boxY + '" width="' + half + '" height="' + boxH + '" rx="12"/>';
  svg += '<text class="dtw-t dtw-t--head" x="' + (ax + half / 2) + '" y="' + (boxY + 20) + '" text-anchor="middle">Модели ИИ на данных</text>';
  svg += '<text class="dtw-t dtw-t--sub" x="' + (ax + half / 2) + '" y="' + (boxY + 35) + '" text-anchor="middle">машинное обучение · RL</text>';
  var ai = ['PINN · нейронный оператор', 'Перенос обучения', 'RL с физикой', 'Оптимизация стратегий'];
  ai.forEach(function (t, i) {
    svg += pill(ax + 14 + (i % 2) * (pw3 + 10), boxY + 46 + Math.floor(i / 2) * 29, pw3, 25, t, i === 0 ? 'dtw-pill--accent' : 'dtw-pill--line', i === 0 ? { tcls: 'dtw-t--onaccent' } : {});
  });
  /* мостик */
  var bx = X + 18 + half + 8, by = boxY + boxH / 2;
  svg += '<line class="dtw-link" x1="' + (X + 18 + half) + '" x2="' + ax + '" y1="' + by + '" y2="' + by + '"/>';
  svg += '<circle class="dtw-node" cx="' + bx + '" cy="' + by + '" r="8"/>';
  svg += '</g>';

  /* ================= Приложения ================= */
  var ap = layers[3];
  svg += '<g class="dtw-layer" data-layer="3">';
  svg += '<rect class="dtw-band dtw-band--green" x="' + X + '" y="' + ap.y + '" width="' + RW + '" height="' + ap.h + '" rx="14"/>';
  var apps = ['Предиктивное обслуживание', 'Диагностика отказов', 'Прогноз срока службы', 'Оптимизация режимов'];
  var aw = (RW - 36 - (apps.length - 1) * 10) / apps.length;
  apps.forEach(function (t, i) {
    svg += pill(X + 18 + i * (aw + 10), ap.y + 11, aw, 30, t, 'dtw-pill--green');
  });
  svg += '</g>';

  /* ================= Визуализация ================= */
  var vi = layers[4];
  svg += '<g class="dtw-layer" data-layer="4">';
  svg += '<rect class="dtw-band dtw-band--gold" x="' + X + '" y="' + vi.y + '" width="' + RW + '" height="' + vi.h + '" rx="14"/>';
  var vis = ['Физическая геометрия', '3D в реальном времени', 'Интерактивные расчёты', 'NVIDIA Omniverse'];
  var vw = (RW - 36 - (vis.length - 1) * 10) / vis.length;
  vis.forEach(function (t, i) {
    svg += pill(X + 18 + i * (vw + 10), vi.y + 11, vw, 30, t, i === vis.length - 1 ? 'dtw-pill--gold dtw-pill--bold' : 'dtw-pill--gold');
  });
  svg += '</g>';

  /* ================= левые подписи уровней ================= */
  layers.forEach(function (l, i) {
    svg += '<g class="dtw-layer" data-layer="' + i + '">' +
      '<text class="dtw-side" x="0" y="' + (l.y + l.h / 2 - 2) + '" text-anchor="start">' + l.tag + '</text>' +
      '<text class="dtw-side dtw-side--sub" x="0" y="' + (l.y + l.h / 2 + 16) + '" text-anchor="start">' + l.sub + '</text>' +
      '</g>';
  });

  host.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Пример AIOS с PINN: пятиуровневый цифровой двойник батареи">' + svg + '</svg>';

  /* --- анимация: уровни появляются снизу вверх --- */
  var groups = Array.prototype.slice.call(host.querySelectorAll('.dtw-layer'));
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  if (reduced) { groups.forEach(function (g) { g.classList.add('on'); }); return; }

  var started = false;
  function start() {
    if (started) return;
    started = true;
    groups.forEach(function (g) {
      var d = parseInt(g.getAttribute('data-layer'), 10);
      setTimeout(function () { g.classList.add('on'); }, 150 + d * 240);
    });
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
    }, { threshold: 0.2 });
    io.observe(host);
    window.addEventListener('load', function () { if (inViewport()) start(); });
    if (document.readyState !== 'loading' && inViewport()) start();
  } else {
    start();
  }
})();
