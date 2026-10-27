/* ============================================================
   Страница модели проекта (model.html?id=mdl-…).

   Интерфейс один на все модели и собирается из паспорта версии:
   входы с единицами и диапазонами опытов → форма расчёта, выходы →
   карточки результата, проверка платформой → вкладка «Как модель
   учится». Под конкретную модель здесь ничего не программируется —
   как у онтологий: общий интерфейс, модели разные.

   Данные — только через window.AIOS_API.teamModel* (api.js):
   GET/POST /api/models/registry/{id}/… Все расчёты идут в песочнице
   на сервере; страница ничего не считает сама, только рисует.
   ============================================================ */

(function () {
  var API = window.AIOS_API;
  var root = document.getElementById('mdlHead');
  if (!root || !API) return;

  var modelId = new URLSearchParams(window.location.search).get('id') || '';
  var state = {
    view: null,          // ответ GET /api/models/registry/{id}
    version: null,       // показанная версия (null — текущая)
    calc: {},            // значения формы расчёта
    lastPred: null,
    profileVar: null,
    program: null,       // температурная программа (кинетика)
    data: null,
    facts: null,
    prevSkill: null,     // точность до переобучения — для анимации «умнеет»
    tab: 'calc'
  };

  var METHOD_TITLES = {
    linear: 'линейная регрессия', quadratic: 'квадратичная регрессия', gp: 'гауссов процесс',
    gbm: 'градиентный бустинг', formula: 'формула с подобранными коэффициентами',
    kinetics: 'уравнение скорости (кинетика)', onnx: 'импортированная модель'
  };
  var SCHEME_TITLES = {
    loo: 'каждый опыт по очереди исключался из обучения и предсказывался моделью, обученной без него',
    kfold5: 'опыты делились на 5 частей; каждая часть предсказывалась моделью, обученной на остальных',
    loro: 'каждая кривая предсказывалась моделью, подобранной без неё (как рекомендует ICTAC)',
    in_sample: 'проверка только на тех же данных — честной оценки нет',
    holdout: 'проверочная таблица, не участвовавшая в обучении'
  };
  var DOMAIN_SHORT = { inside: 'в опытах', edge: 'на границе', outside: 'вне опытов' };
  var DOMAIN_TITLES = { inside: 'в области опытов', edge: 'на границе опытов', outside: 'вне опытов — прогноз недостоверен' };

  /* ---------- утилиты ---------- */

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function num(v, digitsHint) {
    if (v === null || v === undefined || v === '' || isNaN(v)) return '—';
    v = Number(v);
    var a = Math.abs(v);
    if (a !== 0 && (a >= 1e6 || a < 1e-3)) return v.toExponential(2).replace('.', ',');
    var digits = digitsHint !== undefined ? digitsHint : a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3;
    return v.toLocaleString('ru-RU', { maximumFractionDigits: digits });
  }

  function unit(u) { return u ? ' ' + esc(u) : ''; }

  function capital(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

  function fmtShort(ts) {
    return ts ? new Date(ts * 1000).toLocaleDateString('ru-RU') : '';
  }

  function fmtDate(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
  }

  function loggedIn() {
    try {
      var raw = localStorage.getItem('aios-auth-v1');
      return !!(raw && JSON.parse(raw).token);
    } catch (e) {
      return false;
    }
  }

  function askLogin() {
    var btn = document.getElementById('btnLogin');
    if (btn) btn.click();
  }

  var toastTimer = null;
  function toast(text, kind) {
    var t = document.getElementById('mdlToast');
    if (!t) return;
    t.textContent = text;
    t.className = 'mdl-toast' + (kind ? ' mdl-toast--' + kind : '');
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 6000);
  }

  function passport() { return (state.view && state.view.passport) || {}; }
  function spec() { return passport().spec || {}; }
  function kind() { return (state.view && state.view.kind) || spec().kind; }
  function can(what) { return !!(state.view && state.view.can && state.view.can[what]); }
  function outputs() { return spec().outputs || []; }
  function inputs() { return spec().inputs || []; }
  function perOutput() { return ((passport().validation || {}).per_output) || {}; }
  function domain() { return passport().domain || {}; }
  function versionParam() { return state.version && state.version !== state.view.current_version ? state.version : undefined; }

  function button(label, cls, onClick) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = cls || 'btn-register';
    b.textContent = label;
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }

  function busy(btn, on, text) {
    if (!btn) return;
    if (on) {
      btn.dataset.label = btn.textContent;
      btn.textContent = text || 'Считаем…';
      btn.disabled = true;
    } else {
      btn.textContent = btn.dataset.label || btn.textContent;
      btn.disabled = false;
    }
  }

  /* ---------- графики (SVG без библиотек) ---------- */

  function niceTicks(min, max, count) {
    if (!isFinite(min) || !isFinite(max)) return [0, 1];
    if (min === max) { min -= 1; max += 1; }
    var span = max - min;
    var step = Math.pow(10, Math.floor(Math.log10(span / count)));
    var err = (count * step) / span;
    if (err <= 0.15) step *= 10; else if (err <= 0.35) step *= 5; else if (err <= 0.75) step *= 2;
    var ticks = [];
    for (var v = Math.ceil(min / step) * step; v <= max + step * 1e-6; v += step) ticks.push(Number(v.toFixed(10)));
    return ticks;
  }

  /** Линейный график: series [{points: [[x,y]], cls, dash}], bands [{points: [[x, lo, hi]], cls}],
   *  hlines [{y, label, cls}], vshade [x0, x1], markers [{x, y, cls}], extraPoints [{points, cls}]. */
  function chart(opts) {
    var W = opts.width || 640, H = opts.height || 260;
    var m = { l: 58, r: opts.right ? 52 : 16, t: 14, b: 40 };
    var xs = [], ys = [];
    (opts.series || []).forEach(function (s) { s.points.forEach(function (p) { if (p[1] !== null && isFinite(p[1])) { xs.push(p[0]); ys.push(p[1]); } }); });
    (opts.bands || []).forEach(function (b) { b.points.forEach(function (p) { if (isFinite(p[1]) && isFinite(p[2])) { xs.push(p[0]); ys.push(p[1], p[2]); } }); });
    (opts.hlines || []).forEach(function (h) { if (isFinite(h.y)) ys.push(h.y); });
    (opts.extraPoints || []).forEach(function (s) { s.points.forEach(function (p) { xs.push(p[0]); ys.push(p[1]); }); });
    if (!xs.length) return '<p class="mdl-muted">Нет данных для графика</p>';
    var xmin = opts.xmin !== undefined ? opts.xmin : Math.min.apply(null, xs);
    var xmax = opts.xmax !== undefined ? opts.xmax : Math.max.apply(null, xs);
    var ymin = opts.ymin !== undefined ? opts.ymin : Math.min.apply(null, ys);
    var ymax = opts.ymax !== undefined ? opts.ymax : Math.max.apply(null, ys);
    if (opts.zeroY && ymin > 0) ymin = 0;
    var pad = (ymax - ymin) * 0.08 || Math.abs(ymax) * 0.1 || 1;
    if (opts.ymin === undefined && !(opts.zeroY && ymin === 0)) ymin -= pad;
    if (opts.ymax === undefined) ymax += pad;
    if (xmin === xmax) { xmin -= 1; xmax += 1; }
    var X = function (v) { return m.l + (v - xmin) / (xmax - xmin) * (W - m.l - m.r); };
    var Y = function (v) { return H - m.b - (v - ymin) / (ymax - ymin) * (H - m.t - m.b); };
    var out = ['<svg class="mdl-chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(opts.label || 'график') + '">'];
    niceTicks(ymin, ymax, 5).forEach(function (t) {
      if (t < ymin || t > ymax) return;
      out.push('<line class="mdl-grid-line" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + Y(t).toFixed(1) + '" y2="' + Y(t).toFixed(1) + '"/>');
      out.push('<text class="mdl-tick" x="' + (m.l - 6) + '" y="' + (Y(t) + 4).toFixed(1) + '" text-anchor="end">' + num(t) + '</text>');
    });
    niceTicks(xmin, xmax, 6).forEach(function (t) {
      if (t < xmin || t > xmax) return;
      out.push('<text class="mdl-tick" x="' + X(t).toFixed(1) + '" y="' + (H - m.b + 16) + '" text-anchor="middle">' + num(t) + '</text>');
    });
    if (opts.vshade) {
      var a = Math.max(xmin, opts.vshade[0]), b = Math.min(xmax, opts.vshade[1]);
      if (b > a) out.push('<rect class="mdl-shade" x="' + X(a).toFixed(1) + '" y="' + m.t + '" width="' + (X(b) - X(a)).toFixed(1) + '" height="' + (H - m.t - m.b) + '"/>');
    }
    (opts.bands || []).forEach(function (bd) {
      var pts = bd.points.filter(function (p) { return isFinite(p[1]) && isFinite(p[2]); });
      if (!pts.length) return;
      var up = pts.map(function (p) { return X(p[0]).toFixed(1) + ',' + Y(p[2]).toFixed(1); });
      var lo = pts.slice().reverse().map(function (p) { return X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1); });
      out.push('<polygon class="' + (bd.cls || 'mdl-band') + '" points="' + up.concat(lo).join(' ') + '"/>');
    });
    if (opts.diagonal) {
      var d0 = Math.max(xmin, ymin), d1 = Math.min(xmax, ymax);
      out.push('<line class="mdl-diag" x1="' + X(d0).toFixed(1) + '" y1="' + Y(d0).toFixed(1) + '" x2="' + X(d1).toFixed(1) + '" y2="' + Y(d1).toFixed(1) + '"/>');
    }
    (opts.hlines || []).forEach(function (h) {
      if (!isFinite(h.y) || h.y < ymin || h.y > ymax) return;
      out.push('<line class="' + (h.cls || 'mdl-hline') + '" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + Y(h.y).toFixed(1) + '" y2="' + Y(h.y).toFixed(1) + '"/>');
      if (h.label) out.push('<text class="mdl-hline-label" x="' + (W - m.r - 4) + '" y="' + (Y(h.y) - 5).toFixed(1) + '" text-anchor="end">' + esc(h.label) + '</text>');
    });
    (opts.series || []).forEach(function (s) {
      var pts = s.points.filter(function (p) { return p[1] !== null && isFinite(p[1]); });
      if (s.type === 'points') {
        pts.forEach(function (p) { out.push('<circle class="' + (s.cls || 'mdl-pt') + '" cx="' + X(p[0]).toFixed(1) + '" cy="' + Y(p[1]).toFixed(1) + '" r="' + (s.r || 3.5) + '"/>'); });
        return;
      }
      if (!pts.length) return;
      out.push('<polyline class="' + (s.cls || 'mdl-line') + '" points="' + pts.map(function (p) { return X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1); }).join(' ') + '"/>');
      if (s.dots) pts.forEach(function (p) { out.push('<circle class="' + (s.dotCls || 'mdl-dot') + '" cx="' + X(p[0]).toFixed(1) + '" cy="' + Y(p[1]).toFixed(1) + '" r="3.5"/>'); });
    });
    (opts.extraPoints || []).forEach(function (s) {
      s.points.forEach(function (p) { out.push('<circle class="' + (s.cls || 'mdl-pt--gold') + '" cx="' + X(p[0]).toFixed(1) + '" cy="' + Y(p[1]).toFixed(1) + '" r="4.5"/>'); });
    });
    (opts.markers || []).forEach(function (mk) {
      if (!isFinite(mk.y)) return;
      out.push('<circle class="' + (mk.cls || 'mdl-marker') + '" cx="' + X(mk.x).toFixed(1) + '" cy="' + Y(mk.y).toFixed(1) + '" r="6"/>');
    });
    if (opts.right) {
      var r = opts.right;
      var rmin = r.min, rmax = r.max;
      var YR = function (v) { return H - m.b - (v - rmin) / (rmax - rmin || 1) * (H - m.t - m.b); };
      out.push('<polyline class="mdl-line--right" points="' + r.points.map(function (p) { return X(p[0]).toFixed(1) + ',' + YR(p[1]).toFixed(1); }).join(' ') + '"/>');
      niceTicks(rmin, rmax, 4).forEach(function (t) {
        if (t < rmin || t > rmax) return;
        out.push('<text class="mdl-tick mdl-tick--right" x="' + (W - m.r + 6) + '" y="' + (YR(t) + 4).toFixed(1) + '">' + num(t) + '</text>');
      });
      if (r.label) out.push('<text class="mdl-axis mdl-tick--right" x="' + (W - 4) + '" y="' + (m.t + 4) + '" text-anchor="end">' + esc(r.label) + '</text>');
    }
    out.push('<line class="mdl-axis-line" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + (H - m.b) + '" y2="' + (H - m.b) + '"/>');
    if (opts.xLabel) out.push('<text class="mdl-axis" x="' + ((m.l + W - m.r) / 2) + '" y="' + (H - 6) + '" text-anchor="middle">' + esc(opts.xLabel) + '</text>');
    if (opts.yLabel) out.push('<text class="mdl-axis" x="14" y="' + ((m.t + H - m.b) / 2) + '" text-anchor="middle" transform="rotate(-90 14 ' + ((m.t + H - m.b) / 2) + ')">' + esc(opts.yLabel) + '</text>');
    out.push('</svg>');
    return out.join('');
  }

  /** Столбцы по версиям: [{label, sub, value, base, skill}] + порог. */
  function versionBars(items, threshold, unitText) {
    var W = 640, H = 250, m = { l: 58, r: 16, t: 26, b: 46 };
    var vals = [];
    items.forEach(function (it) { if (isFinite(it.value)) vals.push(it.value); if (isFinite(it.base)) vals.push(it.base); });
    if (isFinite(threshold)) vals.push(threshold);
    if (!vals.length) return '';
    var ymax = Math.max.apply(null, vals) * 1.15;
    var Y = function (v) { return H - m.b - v / ymax * (H - m.t - m.b); };
    var bw = Math.min(64, (W - m.l - m.r) / items.length * 0.55);
    var out = ['<svg class="mdl-chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Ошибка модели по версиям">'];
    niceTicks(0, ymax, 5).forEach(function (t) {
      if (t > ymax) return;
      out.push('<line class="mdl-grid-line" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + Y(t).toFixed(1) + '" y2="' + Y(t).toFixed(1) + '"/>');
      out.push('<text class="mdl-tick" x="' + (m.l - 6) + '" y="' + (Y(t) + 4).toFixed(1) + '" text-anchor="end">' + num(t) + '</text>');
    });
    items.forEach(function (it, i) {
      var cx = m.l + (i + 0.5) * (W - m.l - m.r) / items.length;
      if (isFinite(it.base)) {
        out.push('<rect class="mdl-bar-base" x="' + (cx - bw / 2 - 5).toFixed(1) + '" y="' + Y(it.base).toFixed(1) + '" width="' + (bw + 10).toFixed(1) + '" height="' + (H - m.b - Y(it.base)).toFixed(1) + '" rx="4"/>');
      }
      if (isFinite(it.value)) {
        out.push('<rect class="mdl-bar' + (it.current ? ' mdl-bar--current' : '') + '" x="' + (cx - bw / 2).toFixed(1) + '" y="' + Y(it.value).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + (H - m.b - Y(it.value)).toFixed(1) + '" rx="4" style="animation-delay:' + (i * 90) + 'ms"/>');
        out.push('<text class="mdl-bar-label" x="' + cx.toFixed(1) + '" y="' + (Y(it.value) - 6).toFixed(1) + '" text-anchor="middle">±' + num(it.value) + '</text>');
      }
      out.push('<text class="mdl-tick" x="' + cx.toFixed(1) + '" y="' + (H - m.b + 16) + '" text-anchor="middle">' + esc(it.label) + '</text>');
      if (it.sub) out.push('<text class="mdl-tick mdl-tick--sub" x="' + cx.toFixed(1) + '" y="' + (H - m.b + 30) + '" text-anchor="middle">' + esc(it.sub) + '</text>');
    });
    if (isFinite(threshold)) {
      out.push('<line class="mdl-hline mdl-hline--threshold" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + Y(threshold).toFixed(1) + '" y2="' + Y(threshold).toFixed(1) + '"/>');
      out.push('<text class="mdl-hline-label" x="' + (W - m.r - 4) + '" y="' + (Y(threshold) - 5).toFixed(1) + '" text-anchor="end">порог приёмки ' + num(threshold) + unit(unitText) + '</text>');
    }
    out.push('<line class="mdl-axis-line" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + (H - m.b) + '" y2="' + (H - m.b) + '"/>');
    out.push('</svg>');
    return out.join('');
  }

  /* ---------- шапка и шкала точности ---------- */

  function skillOf(view) {
    var h = view && view.headline;
    return h && typeof h.skill === 'number' ? h.skill : null;
  }

  function renderSkill(animateFrom) {
    var box = document.getElementById('mdlSkill');
    var view = state.view;
    var shownSummary = (view.history || []).filter(function (v) { return v.version === (state.version || view.current_version); })[0];
    var skill = shownSummary && typeof shownSummary.skill === 'number' ? shownSummary.skill : skillOf(view);
    if (skill === null || skill === undefined) {
      box.innerHTML = '<div class="mdl-skill-empty">Точность появится после обучения на проверочных данных</div>';
      return;
    }
    var p = Math.max(0, Math.min(1, skill));
    var C = 2 * Math.PI * 54;
    var from = animateFrom !== undefined && animateFrom !== null ? Math.max(0, Math.min(1, animateFrom)) : 0;
    var hist = view.history || [];
    var idx = hist.findIndex(function (v) { return v.version === (state.version || view.current_version); });
    var prev = idx > 0 ? hist[idx - 1] : null;
    var delta = prev && typeof prev.skill === 'number' ? Math.round((skill - prev.skill) * 100) : null;
    var out = perOutput();
    var firstOut = outputs()[0] || {};
    var m = out[firstOut.name] || {};
    box.innerHTML =
      '<svg class="mdl-gauge" viewBox="0 0 132 132" aria-hidden="true">' +
        '<circle cx="66" cy="66" r="54" class="mdl-gauge-bg"/>' +
        '<circle cx="66" cy="66" r="54" class="mdl-gauge-fg" id="mdlGaugeArc" stroke-dasharray="' + (C * from).toFixed(1) + ' ' + C.toFixed(1) + '"/>' +
      '</svg>' +
      '<div class="mdl-gauge-center"><b id="mdlGaugeNum">' + Math.round(from * 100) + '</b><span>%</span></div>' +
      '<div class="mdl-skill-text">' +
        '<b>точнее угадывания средним</b>' +
        (m.mae !== undefined ? '<span>типичная ошибка ±' + num(m.mae) + unit(firstOut.unit) + ' против ±' + num(m.baseline_mae) + '</span>' : '') +
        (delta !== null ? '<span class="mdl-delta ' + (delta >= 0 ? 'mdl-delta--up' : 'mdl-delta--down') + '">' +
          (delta === 0 ? '≈ как у версии ' + prev.version : (delta > 0 ? '▲ +' : '▼ ') + delta + ' п.п. к версии ' + prev.version) + '</span>' : '') +
      '</div>';
    var arc = document.getElementById('mdlGaugeArc');
    var numEl = document.getElementById('mdlGaugeNum');
    var start = null, dur = 1100;
    function step(ts) {
      if (!start) start = ts;
      var k = Math.min(1, (ts - start) / dur);
      var e = 1 - Math.pow(1 - k, 3);
      var v = from + (p - from) * e;
      arc.setAttribute('stroke-dasharray', (C * v).toFixed(1) + ' ' + C.toFixed(1));
      numEl.textContent = Math.round(v * 100);
      if (k < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function renderHead() {
    var v = state.view;
    document.title = v.name + ' — модель AIOS';
    document.getElementById('mdlName').textContent = v.name;
    document.getElementById('mdlDesc').textContent = v.description || '';
    var chips = document.getElementById('mdlChips');
    chips.innerHTML = '<span class="mdl-kind-chip">' + esc(v.kind_title) + '</span>' +
      '<span class="mdl-status mdl-status--' + esc(v.status) + '">' + esc(v.status_title) + '</span>';
    var hist = v.history || [];
    if (hist.length) {
      var sel = document.createElement('select');
      sel.className = 'mdl-version-select';
      sel.setAttribute('aria-label', 'Версия модели');
      hist.slice().reverse().forEach(function (h) {
        var o = document.createElement('option');
        o.value = h.version;
        o.textContent = 'версия ' + h.version + (h.version === v.current_version ? ' · текущая' : '') +
          (h.passed === false ? ' · не прошла порог' : '');
        if (h.version === (state.version || v.current_version)) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener('change', function () {
        var n = parseInt(sel.value, 10);
        load(n === v.current_version ? null : n);
      });
      chips.appendChild(sel);
    }
    var p = passport();
    var meta = [];
    if (v.project_title) meta.push('Проект «' + v.project_title + '»');
    if (v.author) meta.push(v.author);
    if (p.n_rows) meta.push(p.n_rows + (kind() === 'kinetics' ? ' точек' : ' опытов'));
    if (v.verified && v.status === 'verified') meta.push('подтверждена: ' + (v.verified.by_name || '') + ', ' + fmtDate(v.verified.at));
    if (p.created_at) meta.push('версия от ' + fmtDate(p.created_at));
    document.getElementById('mdlMeta').textContent = meta.join(' · ');

    var actions = document.getElementById('mdlActions');
    actions.innerHTML = '';
    if (can('admin') && v.status === 'draft') actions.appendChild(button('Опубликовать', 'btn-primary', publish));
    if (can('admin') && v.status === 'published') actions.appendChild(button('Подтвердить как проверенную', 'btn-primary', verifyForm));
    if (can('admin') && (v.status === 'published' || v.status === 'verified')) actions.appendChild(button('Снять с публикации', 'btn-register', function () { status('unpublish', 'Модель снята с публикации'); }));
    if (loggedIn() && !can('write') && v.status !== 'draft') actions.appendChild(button('Скопировать себе', 'btn-register', fork));
    if (loggedIn() && can('write')) actions.appendChild(button('Доработать в чате', 'btn-register', openChat));
    if (can('admin') && v.status !== 'archived') actions.appendChild(button('Отозвать', 'btn-register mdl-btn-quiet', function () {
      if (window.confirm('Отозвать модель? Её перестанут вызывать агенты; данные и версии сохранятся.')) status('archive', 'Модель отозвана');
    }));
    var notice = document.getElementById('mdlNotice');
    var shown = state.version && state.version !== v.current_version;
    if (shown) {
      notice.hidden = false;
      notice.innerHTML = 'Вы смотрите версию ' + state.version + ', текущая — ' + v.current_version + '. ';
      if (can('write')) {
        var mk = button('Сделать эту версию текущей', 'btn-register', function () { setCurrent(state.version); });
        notice.appendChild(mk);
      }
    } else if (v.status === 'draft') {
      notice.hidden = false;
      notice.textContent = 'Черновик: модель видите вы и участники проекта. Опубликуйте её, когда порог приёмки пройден, — тогда ей смогут пользоваться агенты и другие проектные команды.';
    } else {
      notice.hidden = true;
    }
    renderSkill(state.prevSkill);
    state.prevSkill = null;
  }

  /* ---------- действия с моделью ---------- */

  function status(action, done, body) {
    return API.teamModelStatus(modelId, action, body).then(function () {
      toast(done, 'ok');
      load(state.version);
    }).catch(function (err) { toast(err.message, 'err'); });
  }

  function publish() {
    status('publish', 'Модель опубликована — агенты и проектные команды могут считать по ней');
  }

  function verifyForm() {
    var evidence = window.prompt('На каких независимых данных проверена модель? Например: «5 новых опытов сентября, ошибка 12 нм, все в пределах порога»');
    if (!evidence) return;
    status('verify', 'Модель подтверждена как проверенная', { evidence: evidence });
  }

  function fork() {
    API.teamModelFork(modelId).then(function (res) {
      window.location.href = 'model.html?id=' + encodeURIComponent(res.id);
    }).catch(function (err) { toast(err.message, 'err'); });
  }

  function openChat() {
    var v = state.view;
    document.dispatchEvent(new CustomEvent('aios-launch-skill', {
      detail: {
        skill_id: 'model-builder', name: 'Конструктор моделей',
        prompt: 'Доработка модели «' + v.name + '» (id ' + v.id + ', текущая версия ' + v.current_version +
          '). Покажи, что умеет модель сейчас, и спроси, что поправить: добавить опыты, сменить входы или метод, задать порог.'
      }
    }));
  }

  function setCurrent(n) {
    API.teamModelSetCurrent(modelId, n).then(function () {
      toast('Версия ' + n + ' стала текущей', 'ok');
      load(null);
    }).catch(function (err) { toast(err.message, 'err'); });
  }

  /* ---------- вкладка «Расчёт» ---------- */

  function defaultValue(inp) {
    var d = domain()[inp.name] || {};
    if (d.type === 'number') {
      if (d.median !== undefined && d.median !== null) return d.median;
      if (d.min !== undefined) return (d.min + d.max) / 2;
    }
    if (d.type === 'category' && d.levels && d.levels.length) return d.levels[0];
    return '';
  }

  function inputControl(inp, value, onChange) {
    var wrap = document.createElement('label');
    wrap.className = 'mdl-input';
    var d = domain()[inp.name] || {};
    var head = '<span class="mdl-input-title">' + esc(inp.title || inp.name) + (inp.unit ? ', <i>' + esc(inp.unit) + '</i>' : '') + '</span>';
    if (inp.type === 'category') {
      wrap.innerHTML = head;
      var sel = document.createElement('select');
      (d.levels || []).forEach(function (lv) {
        var o = document.createElement('option');
        o.value = lv; o.textContent = lv;
        if (String(lv) === String(value)) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener('change', function () { onChange(sel.value, true); });
      wrap.appendChild(sel);
      return wrap;
    }
    if (inp.type === 'smiles' || inp.type === 'sequence') {
      wrap.innerHTML = head;
      var ta = document.createElement(inp.type === 'sequence' ? 'textarea' : 'input');
      ta.value = value || '';
      ta.placeholder = inp.type === 'smiles' ? 'SMILES, например CCO' : 'аминокислотная последовательность';
      ta.addEventListener('change', function () { onChange(ta.value, true); });
      wrap.appendChild(ta);
      return wrap;
    }
    var lo = d.min, hi = d.max;
    var hasRange = isFinite(lo) && isFinite(hi) && hi > lo;
    wrap.innerHTML = head + (hasRange ? '<span class="mdl-input-range">опыты: ' + num(lo) + '–' + num(hi) + '</span>' : '');
    var row = document.createElement('span');
    row.className = 'mdl-input-row';
    var numIn = document.createElement('input');
    numIn.type = 'number';
    numIn.step = 'any';
    numIn.value = value;
    if (hasRange) {
      var span = hi - lo;
      var slider = document.createElement('input');
      slider.type = 'range';
      slider.min = lo - 0.1 * span;
      slider.max = hi + 0.1 * span;
      slider.step = span / 200;
      slider.value = value;
      slider.addEventListener('input', function () { numIn.value = Number(slider.value).toPrecision(4) * 1; onChange(parseFloat(slider.value), false); });
      slider.addEventListener('change', function () { onChange(parseFloat(slider.value), true); });
      numIn.addEventListener('change', function () { slider.value = numIn.value; onChange(parseFloat(numIn.value), true); });
      row.appendChild(slider);
    } else {
      numIn.addEventListener('change', function () { onChange(parseFloat(numIn.value), true); });
    }
    row.appendChild(numIn);
    wrap.appendChild(row);
    return wrap;
  }

  function renderCalc() {
    var panel = document.getElementById('tab-calc');
    panel.innerHTML = '';
    if (!state.view.current_version) {
      panel.innerHTML = '<p class="mdl-muted">Модель ещё не обучена.</p>';
      return;
    }
    if (kind() === 'kinetics') { renderKineticsCalc(panel); return; }
    var grid = document.createElement('div');
    grid.className = 'mdl-calc';
    var form = document.createElement('div');
    form.className = 'mdl-card-box mdl-calc-form';
    form.innerHTML = '<h3>Условия</h3>';
    var resultBox = document.createElement('div');
    resultBox.className = 'mdl-calc-result';
    inputs().forEach(function (inp) {
      if (state.calc[inp.name] === undefined) state.calc[inp.name] = defaultValue(inp);
      form.appendChild(inputControl(inp, state.calc[inp.name], function (v, commit) {
        state.calc[inp.name] = v;
        if (commit) runPredict();
      }));
    });
    var runBtn = button('Рассчитать', 'btn-primary mdl-run', runPredict);
    form.appendChild(runBtn);
    grid.appendChild(form);
    grid.appendChild(resultBox);
    panel.appendChild(grid);

    var prof = document.createElement('div');
    prof.className = 'mdl-card-box mdl-profile';
    var numeric = inputs().filter(function (i) { return (i.type || 'number') === 'number' && (domain()[i.name] || {}).type === 'number'; });
    if (numeric.length) {
      if (!state.profileVar || !numeric.some(function (i) { return i.name === state.profileVar; })) state.profileVar = numeric[0].name;
      var head = document.createElement('div');
      head.className = 'mdl-profile-head';
      head.innerHTML = '<h3>Как меняется результат</h3>';
      var sel = document.createElement('select');
      numeric.forEach(function (i) {
        var o = document.createElement('option');
        o.value = i.name; o.textContent = 'от: ' + (i.title || i.name);
        if (i.name === state.profileVar) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener('change', function () { state.profileVar = sel.value; runProfile(); });
      head.appendChild(sel);
      prof.appendChild(head);
      var chartBox = document.createElement('div');
      chartBox.id = 'mdlProfileChart';
      chartBox.className = 'mdl-chart-box';
      prof.appendChild(chartBox);
      prof.insertAdjacentHTML('beforeend', '<p class="mdl-caption">Остальные условия — как в форме слева. Затенено — диапазон опытов, полоса — ± типичная ошибка.</p>');
      panel.appendChild(prof);
    }
    if (!loggedIn()) {
      resultBox.innerHTML = '<div class="mdl-card-box mdl-result-empty">Войдите, чтобы считать по модели.</div>';
      runBtn.disabled = true;
      return;
    }
    if (state.lastPred) drawPrediction(state.lastPred); else runPredict();
  }

  var predictSeq = 0;
  function runPredict() {
    if (!loggedIn()) { askLogin(); return; }
    var seq = ++predictSeq;
    var box = document.querySelector('.mdl-calc-result');
    if (box) box.classList.add('mdl-loading');
    API.teamModelPredict(modelId, { inputs: state.calc, version: versionParam() }).then(function (res) {
      if (seq !== predictSeq) return;
      state.lastPred = res;
      drawPrediction(res);
      runProfile();
    }).catch(function (err) {
      if (box) box.innerHTML = '<div class="mdl-card-box mdl-result-empty">' + esc(err.message) + '</div>';
    }).then(function () { if (box) box.classList.remove('mdl-loading'); });
  }

  function drawPrediction(res) {
    var box = document.querySelector('.mdl-calc-result');
    if (!box) return;
    var item = (res.items || [])[0];
    if (!item) return;
    var status = (item.domain || {}).status || 'inside';
    var html = '';
    (res.outputs_meta || []).forEach(function (o) {
      var r = item.outputs[o.name] || {};
      html += '<div class="mdl-card-box mdl-result mdl-result--' + status + '">' +
        '<span class="mdl-result-title">' + esc(o.title || o.name) + '</span>' +
        '<span class="mdl-result-value">' + num(r.value) + (r.error !== null && r.error !== undefined ? '<small> ± ' + num(r.error) + '</small>' : '') + '<em>' + esc(o.unit || '') + '</em></span>' +
        '</div>';
    });
    html += '<div class="mdl-domain mdl-domain--' + status + '"><b>' + esc(DOMAIN_TITLES[status] || status) + '</b>' +
      ((item.domain || {}).notes || []).map(function (n) { return '<span>' + esc(n) + '</span>'; }).join('') +
      '<span class="mdl-muted">версия ' + esc(res.model && res.model.version) + '</span></div>';
    box.innerHTML = html;
  }

  var profileSeq = 0;
  function runProfile() {
    var box = document.getElementById('mdlProfileChart');
    if (!box || !state.profileVar || !loggedIn()) return;
    var seq = ++profileSeq;
    box.classList.add('mdl-loading');
    API.teamModelProfile(modelId, { base: state.calc, vary: state.profileVar, points: 41, version: versionParam() })
      .then(function (res) {
        if (seq !== profileSeq) return;
        var o = outputs()[0];
        var s = res.series[o.name];
        var inp = inputs().filter(function (i) { return i.name === state.profileVar; })[0] || {};
        var pts = res.x.map(function (x, i) { return [x, s.y[i]]; });
        var band = res.x.map(function (x, i) { return [x, s.y[i] - (s.err[i] || 0), s.y[i] + (s.err[i] || 0)]; });
        var cur = state.lastPred && state.lastPred.items && state.lastPred.items[0];
        box.innerHTML = chart({
          label: 'Зависимость выхода от входа',
          series: [{ points: pts, cls: 'mdl-line' }],
          bands: [{ points: band }],
          vshade: res.range,
          markers: cur ? [{ x: parseFloat(state.calc[state.profileVar]), y: (cur.outputs[o.name] || {}).value }] : [],
          xLabel: (inp.title || inp.name) + (inp.unit ? ', ' + inp.unit : ''),
          yLabel: (o.title || o.name) + (o.unit ? ', ' + o.unit : '')
        });
      })
      .catch(function (err) { box.innerHTML = '<p class="mdl-muted">' + esc(err.message) + '</p>'; })
      .then(function () { box.classList.remove('mdl-loading'); });
  }

  /* ---- кинетика: температурная программа ---- */

  function defaultProgram() {
    var d = domain().T || {};
    var hi = isFinite(d.max) ? d.max : 150;
    var target = Math.round((isFinite(d.min) ? (d.min + hi) / 2 : hi - 20));
    return { T0: 25, segments: [{ kind: 'ramp', to: target, rate: 5 }, { kind: 'hold', time: 60 }], targets: [0.5, 0.9, 0.95] };
  }

  function renderKineticsCalc(panel) {
    if (!state.program) state.program = defaultProgram();
    var prog = state.program;
    var tu = (spec().kinetics || {}).time_unit || 'мин';
    var grid = document.createElement('div');
    grid.className = 'mdl-calc';
    var form = document.createElement('div');
    form.className = 'mdl-card-box mdl-calc-form';
    form.innerHTML = '<h3>Температурная программа</h3>' +
      '<label class="mdl-input"><span class="mdl-input-title">Начальная температура, <i>°C</i></span><input type="number" step="any" id="kinT0" value="' + esc(prog.T0) + '"></label>' +
      '<div class="mdl-segments" id="kinSegments"></div>';
    var segs = form.querySelector('#kinSegments');
    function drawSegments() {
      segs.innerHTML = '';
      prog.segments.forEach(function (sg, i) {
        var row = document.createElement('div');
        row.className = 'mdl-segment';
        if (sg.kind === 'ramp') {
          row.innerHTML = '<span>Нагрев до</span><input type="number" step="any" data-k="to" value="' + esc(sg.to) + '"><span>°C со скоростью</span><input type="number" step="any" data-k="rate" value="' + esc(sg.rate) + '"><span>°C/' + esc(tu) + '</span>';
        } else {
          row.innerHTML = '<span>Выдержка</span><input type="number" step="any" data-k="time" value="' + esc(sg.time) + '"><span>' + esc(tu) + '</span>';
        }
        row.querySelectorAll('input').forEach(function (inp) {
          inp.addEventListener('change', function () { sg[inp.dataset.k] = parseFloat(inp.value); });
        });
        var del = button('✕', 'mdl-seg-del', function () { prog.segments.splice(i, 1); drawSegments(); });
        del.setAttribute('aria-label', 'Убрать шаг');
        row.appendChild(del);
        segs.appendChild(row);
      });
    }
    drawSegments();
    var addRow = document.createElement('div');
    addRow.className = 'mdl-seg-add';
    addRow.appendChild(button('+ нагрев', 'btn-register', function () {
      var last = prog.segments.filter(function (s) { return s.kind === 'ramp'; }).pop();
      prog.segments.push({ kind: 'ramp', to: last ? last.to + 20 : 120, rate: 5 }); drawSegments();
    }));
    addRow.appendChild(button('+ выдержка', 'btn-register', function () { prog.segments.push({ kind: 'hold', time: 30 }); drawSegments(); }));
    form.appendChild(addRow);
    var runBtn = button('Рассчитать', 'btn-primary mdl-run', runKinetics);
    form.appendChild(runBtn);
    form.querySelector('#kinT0').addEventListener('change', function (e) { prog.T0 = parseFloat(e.target.value); });
    var result = document.createElement('div');
    result.className = 'mdl-calc-result';
    grid.appendChild(form);
    grid.appendChild(result);
    panel.appendChild(grid);
    var chartBox = document.createElement('div');
    chartBox.className = 'mdl-card-box';
    chartBox.innerHTML = '<h3>Степень превращения по ходу программы</h3><div class="mdl-chart-box" id="kinChart"></div>' +
      '<p class="mdl-caption">Сплошная линия — модель, полоса — разброс из-за неопределённости коэффициентов, тонкая линия — температура (правая шкала).</p>';
    panel.appendChild(chartBox);
    if (!loggedIn()) {
      result.innerHTML = '<div class="mdl-card-box mdl-result-empty">Войдите, чтобы считать по модели.</div>';
      runBtn.disabled = true;
      return;
    }
    runKinetics();
  }

  function runKinetics() {
    var prog = state.program;
    var result = document.querySelector('.mdl-calc-result');
    var chartEl = document.getElementById('kinChart');
    result.classList.add('mdl-loading');
    API.teamModelPredict(modelId, { T0: prog.T0, program: prog.segments, targets: prog.targets, version: versionParam() })
      .then(function (res) {
        var tu = (spec().kinetics || {}).time_unit || 'мин';
        var status = (res.domain || {}).status || 'inside';
        var out = outputs()[0] || {};
        result.innerHTML =
          '<div class="mdl-card-box mdl-result mdl-result--' + status + '"><span class="mdl-result-title">' + esc(out.title || 'Степень превращения') + ' в конце программы</span>' +
          '<span class="mdl-result-value">' + num(res.final, 3) + '</span></div>' +
          '<div class="mdl-card-box mdl-reach">' + (res.reach || []).map(function (r) {
            return '<div><span>до ' + num(r.target, 2) + '</span><b>' + (r.time === null ? 'не достигается' : num(r.time) + ' ' + esc(tu)) + '</b></div>';
          }).join('') + '</div>' +
          '<div class="mdl-domain mdl-domain--' + status + '"><b>' + esc(DOMAIN_TITLES[status]) + '</b>' +
          ((res.domain || {}).notes || []).map(function (n) { return '<span>' + esc(n) + '</span>'; }).join('') +
          '<span class="mdl-muted">версия ' + esc(res.model && res.model.version) + '</span></div>';
        var pts = res.t.map(function (t, i) { return [t, res.y[i]]; });
        var bands = res.y_lo ? [{ points: res.t.map(function (t, i) { return [t, res.y_lo[i], res.y_hi[i]]; }) }] : [];
        var Tmin = Math.min.apply(null, res.T), Tmax = Math.max.apply(null, res.T);
        chartEl.innerHTML = chart({
          label: 'Степень превращения по ходу программы',
          series: [{ points: pts }], bands: bands, ymin: 0, ymax: 1.02,
          right: { points: res.t.map(function (t, i) { return [t, res.T[i]]; }), min: Tmin - 5, max: Tmax + 5, label: '°C' },
          xLabel: 'время, ' + tu, yLabel: out.title || 'степень превращения'
        });
      })
      .catch(function (err) { result.innerHTML = '<div class="mdl-card-box mdl-result-empty">' + esc(err.message) + '</div>'; })
      .then(function () { result.classList.remove('mdl-loading'); });
  }

  /* ---------- вкладка «Подбор режима» ---------- */

  function renderOptimize() {
    var panel = document.getElementById('tab-optimize');
    panel.innerHTML = '';
    if (!state.view.current_version) return;
    if (kind() === 'kinetics') { renderKineticsOptimize(panel); return; }
    var box = document.createElement('div');
    box.className = 'mdl-card-box';
    box.innerHTML = '<h3>Что нужно получить</h3>';
    var targets = {};
    outputs().forEach(function (o) {
      var row = document.createElement('div');
      row.className = 'mdl-target';
      row.innerHTML = '<span class="mdl-input-title">' + esc(o.title || o.name) + unit(o.unit) + '</span>' +
        '<select><option value="value">значение</option><option value="min">как можно меньше</option><option value="max">как можно больше</option><option value="skip">не важно</option></select>' +
        '<input type="number" step="any" placeholder="цель"><span class="mdl-muted">±</span><input type="number" step="any" placeholder="допуск">';
      var sel = row.querySelector('select');
      var inputsEls = row.querySelectorAll('input');
      if (outputs().length > 1 && o !== outputs()[0]) sel.value = 'skip';
      sel.addEventListener('change', function () {
        inputsEls.forEach(function (i) { i.disabled = sel.value !== 'value'; });
      });
      targets[o.name] = { sel: sel, value: inputsEls[0], tol: inputsEls[1] };
      box.appendChild(row);
    });
    box.insertAdjacentHTML('beforeend', '<h3 class="mdl-sub">Что нельзя менять</h3>');
    var fixed = {};
    inputs().forEach(function (inp) {
      var row = document.createElement('label');
      row.className = 'mdl-fix';
      var mustFix = inp.type === 'smiles' || inp.type === 'sequence';
      row.innerHTML = '<input type="checkbox"' + (mustFix ? ' checked disabled' : '') + '> <span>' + esc(inp.title || inp.name) + unit(inp.unit) + '</span>';
      var val = document.createElement(inp.type === 'category' ? 'select' : 'input');
      var d = domain()[inp.name] || {};
      if (inp.type === 'category') {
        (d.levels || []).forEach(function (lv) { var o = document.createElement('option'); o.value = lv; o.textContent = lv; val.appendChild(o); });
      } else if (inp.type !== 'smiles' && inp.type !== 'sequence') {
        val.type = 'number'; val.step = 'any';
      }
      val.value = state.calc[inp.name] !== undefined ? state.calc[inp.name] : defaultValue(inp);
      row.appendChild(val);
      fixed[inp.name] = { box: row.querySelector('input'), val: val };
      box.appendChild(row);
    });
    var go = button('Подобрать режимы', 'btn-primary mdl-run', function () {
      if (!loggedIn()) { askLogin(); return; }
      var tg = {};
      Object.keys(targets).forEach(function (k) {
        var t = targets[k];
        if (t.sel.value === 'skip') return;
        if (t.sel.value === 'value') {
          if (t.value.value === '') return;
          tg[k] = { value: parseFloat(t.value.value), tolerance: t.tol.value === '' ? null : parseFloat(t.tol.value) };
        } else tg[k] = { goal: t.sel.value };
      });
      if (!Object.keys(tg).length) { toast('Задайте цель хотя бы для одного выхода', 'err'); return; }
      var fx = {};
      Object.keys(fixed).forEach(function (k) {
        if (fixed[k].box.checked) fx[k] = fixed[k].val.type === 'number' ? parseFloat(fixed[k].val.value) : fixed[k].val.value;
      });
      busy(go, true, 'Подбираем…');
      API.teamModelOptimize(modelId, { targets: tg, fixed: fx, count: 4, version: versionParam() }).then(function (res) {
        drawRegimes(res);
      }).catch(function (err) { toast(err.message, 'err'); }).then(function () { busy(go, false); });
    });
    box.appendChild(go);
    panel.appendChild(box);
    var results = document.createElement('div');
    results.id = 'optResults';
    panel.appendChild(results);
  }

  function drawRegimes(res) {
    var box = document.getElementById('optResults');
    var items = res.items || [];
    if (!items.length) { box.innerHTML = '<p class="mdl-muted">Подходящих режимов не нашлось.</p>'; return; }
    var ins = res.inputs_meta || inputs();
    var outs = res.outputs_meta || outputs();
    var html = '<div class="mdl-card-box"><h3>Режимы</h3><div class="mdl-table-wrap"><table class="mdl-table"><thead><tr><th>#</th>' +
      ins.map(function (i) { return '<th>' + esc(i.title || i.name) + (i.unit ? ', ' + esc(i.unit) : '') + '</th>'; }).join('') +
      outs.map(function (o) { return '<th>' + esc(o.title || o.name) + (o.unit ? ', ' + esc(o.unit) : '') + '</th>'; }).join('') +
      '<th>область</th><th></th></tr></thead><tbody>';
    items.forEach(function (it, n) {
      var st = (it.domain || {}).status || 'inside';
      html += '<tr><td>' + (n + 1) + '</td>' +
        ins.map(function (i) { var v = it.inputs[i.name]; return '<td>' + (typeof v === 'number' ? num(v) : esc(v)) + '</td>'; }).join('') +
        outs.map(function (o) { var r = it.outputs[o.name] || {}; return '<td><b>' + num(r.value) + '</b> ± ' + num(r.error) + '</td>'; }).join('') +
        '<td class="mdl-nowrap"><span class="mdl-dot-status mdl-dot-status--' + st + '"></span>' + esc(DOMAIN_SHORT[st] || st) + '</td>' +
        '<td><button type="button" class="btn-register mdl-take" data-i="' + n + '">В расчёт</button></td></tr>';
    });
    html += '</tbody></table></div><p class="mdl-caption">Поставьте опыт по одному из режимов и внесите результат во вкладке «Данные и версии» — так видна реальная ошибка модели.</p></div>';
    box.innerHTML = html;
    box.querySelectorAll('.mdl-take').forEach(function (b) {
      b.addEventListener('click', function () {
        var it = items[parseInt(b.dataset.i, 10)];
        Object.keys(it.inputs).forEach(function (k) { state.calc[k] = it.inputs[k]; });
        state.lastPred = null;
        switchTab('calc');
      });
    });
  }

  function renderKineticsOptimize(panel) {
    var d = domain().T || {};
    var lo = isFinite(d.min) ? d.min : 80, hi = isFinite(d.max) ? d.max : 180;
    var temps = [];
    for (var i = 0; i < 5; i++) temps.push(Math.round(lo + (hi - lo) * (0.35 + 0.65 * i / 4)));
    var tu = (spec().kinetics || {}).time_unit || 'мин';
    var box = document.createElement('div');
    box.className = 'mdl-card-box';
    box.innerHTML = '<h3>Время выдержки до заданной степени превращения</h3>' +
      '<label class="mdl-input"><span class="mdl-input-title">Цель: степень превращения</span><input type="number" step="0.01" min="0" max="1" id="kinTarget" value="0.9"></label>' +
      '<label class="mdl-input"><span class="mdl-input-title">Температуры выдержки, °C (через запятую)</span><input type="text" id="kinTemps" value="' + temps.join(', ') + '"></label>' +
      '<label class="mdl-input"><span class="mdl-input-title">Нагрев от, °C и скорость, °C/' + esc(tu) + ' (необязательно)</span><span class="mdl-input-row"><input type="number" step="any" id="kinFrom" placeholder="25"><input type="number" step="any" id="kinRate" placeholder="5"></span></label>';
    var go = button('Рассчитать время', 'btn-primary mdl-run', function () {
      if (!loggedIn()) { askLogin(); return; }
      var list = document.getElementById('kinTemps').value.split(/[,;\s]+/).map(function (v) { return parseFloat(v.replace(',', '.')); }).filter(function (v) { return !isNaN(v); });
      var body = { kinetics: { target: parseFloat(document.getElementById('kinTarget').value), temperatures: list,
        T0: parseFloat(document.getElementById('kinFrom').value) || null, rate: parseFloat(document.getElementById('kinRate').value) || null }, version: versionParam() };
      busy(go, true, 'Считаем…');
      API.teamModelOptimize(modelId, body).then(function (res) {
        var html = '<div class="mdl-card-box"><h3>До степени превращения ' + num(res.target, 2) + '</h3><div class="mdl-table-wrap"><table class="mdl-table"><thead><tr><th>Температура, °C</th><th>Время, ' + esc(tu) + '</th><th>Степень в конце горизонта</th></tr></thead><tbody>' +
          (res.items || []).map(function (it) {
            return '<tr><td>' + num(it.T) + '</td><td><b>' + (it.time === null ? 'не достигается' : num(it.time)) + '</b></td><td>' + num(it.final, 3) + '</td></tr>';
          }).join('') + '</tbody></table></div></div>';
        document.getElementById('optResults').innerHTML = html;
      }).catch(function (err) { toast(err.message, 'err'); }).then(function () { busy(go, false); });
    });
    box.appendChild(go);
    panel.appendChild(box);
    var results = document.createElement('div');
    results.id = 'optResults';
    panel.appendChild(results);
  }

  /* ---------- вкладка «Как модель учится» ---------- */

  function renderLearn() {
    var panel = document.getElementById('tab-learn');
    panel.innerHTML = '';
    var p = passport();
    if (!p.version) { panel.innerHTML = '<p class="mdl-muted">Модель ещё не обучена.</p>'; return; }
    var val = p.validation || {};
    var o = outputs()[0] || {};
    var m = (val.per_output || {})[o.name] || {};

    var summary = document.createElement('div');
    summary.className = 'mdl-card-box mdl-learn-summary';
    var times = m.baseline_mae && m.mae ? m.baseline_mae / m.mae : null;
    summary.innerHTML = '<p>Модель ' + (kind() === 'kinetics' ? 'подобрана по ' + (m.runs || '') + ' кривым (' + (p.n_rows || '') + ' точек)' : 'обучена на ' + (p.n_rows || m.n || '') + ' опытах') + '. ' +
      (m.mae !== undefined ? 'Типичная ошибка на данных, не участвовавших в обучении, — <b>±' + num(m.mae) + unit(o.unit) + '</b>' +
        (times ? ', это в <b>' + num(times, 1) + ' раза</b> меньше, чем если угадывать средним (±' + num(m.baseline_mae) + ').' : '.') : '') +
      '</p>' + (m.curve_verdict ? '<p class="mdl-verdict">' + esc(m.curve_verdict) + '</p>' : '');
    panel.appendChild(summary);

    // как модель умнела — по версиям
    var hist = state.view.history || [];
    var vb = document.createElement('div');
    vb.className = 'mdl-card-box';
    var thr = (p.threshold_check || {}).max;
    if (thr === undefined && spec().threshold) thr = spec().threshold.max;
    vb.innerHTML = '<h3>Как модель умнела</h3>';
    if (hist.length > 1) {
      vb.insertAdjacentHTML('beforeend', versionBars(hist.map(function (h) {
        var ho = (h.outputs || {})[o.name] || {};
        return { label: 'версия ' + h.version, sub: (h.n ? h.n + (kind() === 'kinetics' ? ' точек' : ' опытов') : '') + (typeof h.skill === 'number' ? ' · ' + Math.round(h.skill * 100) + ' %' : ''),
          value: ho.mae, base: ho.baseline_mae, current: h.version === state.view.current_version };
      }), thr, o.unit));
      vb.insertAdjacentHTML('beforeend', '<p class="mdl-caption">Столбец — типичная ошибка версии, светлая подложка — ошибка угадывания средним на тех же данных. Процент — насколько модель точнее угадывания.</p>');
    } else {
      vb.insertAdjacentHTML('beforeend', '<p class="mdl-muted">Сейчас одна версия. После дообучения на новых опытах здесь будет видно, как падает ошибка.</p>');
    }
    panel.appendChild(vb);

    var row = document.createElement('div');
    row.className = 'mdl-two';
    // кривая обучения
    var curve = ((val.learning_curve || {})[o.name]) || [];
    if (curve.length >= 2) {
      var lc = document.createElement('div');
      lc.className = 'mdl-card-box';
      lc.innerHTML = '<h3>Что дают новые опыты</h3>' + chart({
        label: 'Кривая обучения', width: 420, height: 240, zeroY: true,
        series: [{ points: curve.map(function (c) { return [c.n, c.mae]; }), dots: true }],
        xLabel: 'опытов в обучении', yLabel: 'ошибка' + (o.unit ? ', ' + o.unit : '')
      }) + '<p class="mdl-caption">Ошибка модели, обученной на части опытов. Если кривая ещё идёт вниз — новые опыты сделают модель точнее.</p>';
      row.appendChild(lc);
    }
    // прогноз против опыта
    var parity = ((val.parity || {})[o.name]) || [];
    if (parity.length) {
      var pc = document.createElement('div');
      pc.className = 'mdl-card-box';
      var facts = (state.facts && state.facts.items) || [];
      var extra = facts.filter(function (f) { return f.measured && f.predicted && f.measured[o.name] !== undefined && f.predicted[o.name] !== null; })
        .map(function (f) { return [f.measured[o.name], f.predicted[o.name]]; });
      pc.innerHTML = '<h3>Прогноз против опыта</h3>' + chart({
        label: 'Прогноз против опыта', width: 420, height: 240, diagonal: true,
        series: [{ type: 'points', points: parity }],
        extraPoints: extra.length ? [{ points: extra, cls: 'mdl-pt--gold' }] : [],
        xLabel: 'опыт' + (o.unit ? ', ' + o.unit : ''), yLabel: 'прогноз'
      }) + '<p class="mdl-caption">Точки — прогноз для опытов, которых модель не видела при обучении; чем ближе к диагонали, тем лучше.' +
        (extra.length ? ' Золотые — замеры, внесённые после обучения.' : '') + '</p>';
      row.appendChild(pc);
    }
    if (row.children.length) panel.appendChild(row);

    // влияние факторов / формула / кинетика
    var fit = (p.fit || {})[o.name] || {};
    if (kind() === 'formula' || kind() === 'kinetics') {
      var fb = document.createElement('div');
      fb.className = 'mdl-card-box';
      var paramsRows = (fit.params || []).map(function (pr) {
        var val2 = pr.value, ci = pr.ci95;
        var label = pr.name;
        if (pr.name === 'E' || /^E\d*$|^Ea$/.test(pr.name)) {
          return '<tr><td>' + esc(label) + '</td><td>' + num(val2 / 1000, 1) + ' кДж/моль</td><td>' + (ci !== undefined && ci !== null ? '± ' + num(ci / 1000, 1) : '—') + '</td></tr>';
        }
        if (pr.log10 !== undefined) {
          return '<tr><td>' + esc(label) + '</td><td>' + num(val2) + '</td><td>' + (pr.ci95_log10 !== null && pr.ci95_log10 !== undefined ? '× 10^±' + num(pr.ci95_log10, 2) : '—') + '</td></tr>';
        }
        return '<tr><td>' + esc(label) + '</td><td>' + num(val2) + '</td><td>' + (ci !== undefined && ci !== null ? '± ' + num(ci) : '—') + '</td></tr>';
      }).join('');
      fb.innerHTML = '<h3>' + (kind() === 'kinetics' ? 'Кинетические параметры' : 'Формула и коэффициенты') + '</h3>' +
        (fit.equation ? '<p class="mdl-equation">' + esc((outputs()[0] || {}).title || '') + ' = ' + esc(fit.equation) + '</p>' : '') +
        (kind() === 'kinetics' ? '<p class="mdl-equation">d' + esc((spec().kinetics || {}).state || 'a') + '/dt = ' + esc((spec().kinetics || {}).rate) + '</p>' : '') +
        '<div class="mdl-table-wrap"><table class="mdl-table"><thead><tr><th>Коэффициент</th><th>Значение</th><th>95 %-интервал</th></tr></thead><tbody>' + paramsRows + '</tbody></table></div>';
      if (kind() === 'kinetics' && m.per_run) {
        fb.insertAdjacentHTML('beforeend', '<h3 class="mdl-sub">Проверка на каждой кривой</h3><div class="mdl-table-wrap"><table class="mdl-table"><thead><tr><th>Опыт</th><th>Точек</th><th>Ошибка прогноза</th></tr></thead><tbody>' +
          m.per_run.map(function (r) { return '<tr><td>' + esc(r.run) + '</td><td>' + r.points + '</td><td>' + num(r.mae, 3) + (r.in_sample ? ' (на тех же данных)' : '') + '</td></tr>'; }).join('') +
          '</tbody></table></div>');
      }
      panel.appendChild(fb);
    }
    var effects = ((val.effects || {})[o.name]) || [];
    if (effects.length) {
      var eb = document.createElement('div');
      eb.className = 'mdl-card-box';
      var maxAbs = Math.max.apply(null, effects.map(function (e) { return Math.abs(e.delta || 0); })) || 1;
      eb.innerHTML = '<h3>Что сильнее всего влияет на «' + esc(o.title || o.name) + '»</h3>' + effects.map(function (e) {
        var inp = inputs().filter(function (i) { return i.name === e.input; })[0] || { title: e.input };
        var w = Math.abs(e.delta || 0) / maxAbs * 100;
        var what = e.kind === 'category' ? esc(e.low) + ' → ' + esc(e.high) : num(e.low) + ' → ' + num(e.high) + unit(inp.unit);
        return '<div class="mdl-effect"><span class="mdl-effect-name">' + esc(inp.title || inp.name) + '<small>' + what + '</small></span>' +
          '<span class="mdl-effect-bar"><i class="' + ((e.delta || 0) >= 0 ? 'mdl-effect-up' : 'mdl-effect-down') + '" style="width:' + w.toFixed(1) + '%"></i></span>' +
          '<span class="mdl-effect-val">' + ((e.delta || 0) >= 0 ? '+' : '') + num(e.delta) + unit(o.unit) + '</span></div>';
      }).join('') + '<p class="mdl-caption">Изменение прогноза, если сдвинуть один вход с нижней четверти опытов до верхней при остальных на медиане.</p>';
      panel.appendChild(eb);
    }

    // область применимости и как проверяли
    var two = document.createElement('div');
    two.className = 'mdl-two';
    var db = document.createElement('div');
    db.className = 'mdl-card-box';
    var dom = domain();
    var domRows = inputs().map(function (inp) {
      var d = dom[inp.name] || {};
      var range = d.type === 'number' ? num(d.min) + ' … ' + num(d.max) + unit(inp.unit) :
        d.type === 'category' ? esc((d.levels || []).join(', ')) :
        d.type ? 'по сходству с ' + (d.count || '') + ' обучающими' : '—';
      return '<tr><td>' + esc(inp.title || inp.name) + '</td><td>' + range + '</td></tr>';
    }).join('');
    if (kind() === 'kinetics') {
      domRows = '<tr><td>Температура</td><td>' + num((dom.T || {}).min) + ' … ' + num((dom.T || {}).max) + ' °C</td></tr>' +
        '<tr><td>Время</td><td>' + num((dom.t || {}).min) + ' … ' + num((dom.t || {}).max) + '</td></tr>';
    }
    db.innerHTML = '<h3>Где модели можно верить</h3><div class="mdl-table-wrap"><table class="mdl-table"><tbody>' + domRows + '</tbody></table></div>' +
      '<p class="mdl-caption">За пределами опытов модель не молчит: прогноз помечается «на границе» или «вне опытов — недостоверен».</p>';
    two.appendChild(db);
    var cb = document.createElement('div');
    cb.className = 'mdl-card-box';
    var cands = ((val.candidates || {})[o.name]) || [];
    var th = p.threshold_check;
    cb.innerHTML = '<h3>Как проверяли</h3>' +
      '<p>Метод: <b>' + esc(METHOD_TITLES[m.method] || m.method || '') + '</b>. ' + esc(capital(SCHEME_TITLES[m.scheme] || '')) + '.</p>' +
      (cands.length > 1 ? '<div class="mdl-table-wrap"><table class="mdl-table"><thead><tr><th>Сравнивались</th><th>Ошибка</th></tr></thead><tbody>' +
        cands.map(function (c) { return '<tr' + (c.method === m.method ? ' class="mdl-row-best"' : '') + '><td>' + esc(METHOD_TITLES[c.method] || c.method) + '</td><td>' + (c.mae !== undefined ? '±' + num(c.mae) : esc(c.error || '—')) + '</td></tr>'; }).join('') +
        '</tbody></table></div><p class="mdl-caption">Выбран самый простой метод из почти одинаково точных.</p>' : '') +
      (th ? '<p class="mdl-threshold mdl-threshold--' + (th.passed ? 'ok' : 'fail') + '">Порог приёмки ±' + num(th.max) + unit(o.unit) + ': ' + (th.passed ? 'пройден' : 'не пройден') + ' (±' + num(th.value) + ')</p>' :
        '<p class="mdl-threshold mdl-threshold--none">Порог приёмки не задан — опубликовать модель нельзя. Задайте его во вкладке «Данные и версии».</p>') +
      ((p.controls || []).length ? '<p>Контрольные значения: ' + p.controls.filter(function (c) { return c.passed; }).length + ' из ' + p.controls.length + ' в допуске.</p>' : '') +
      ((p.warnings || []).length ? '<ul class="mdl-warnings">' + p.warnings.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('') + '</ul>' : '') +
      (p.env ? '<p class="mdl-muted mdl-small">Окружение: ' + esc(Object.keys(p.env).map(function (k) { return k + ' ' + p.env[k]; }).join(', ')) + '</p>' : '');
    two.appendChild(cb);
    panel.appendChild(two);
  }

  /* ---------- вкладка «Данные и версии» ---------- */

  function renderData() {
    var panel = document.getElementById('tab-data');
    panel.innerHTML = '';
    var v = state.view;
    // переобучение
    if (can('write')) {
      var rb = document.createElement('div');
      rb.className = 'mdl-card-box mdl-retrain';
      var thr = (spec().threshold || {}).max;
      rb.innerHTML = '<h3>Дообучить модель</h3>' +
        '<p class="mdl-muted">Каждое обучение — новая версия. Старые версии остаются, к ним можно вернуться.</p>' +
        '<div class="mdl-retrain-grid">' +
        '<label class="mdl-input"><span class="mdl-input-title">Новые опыты (Excel или CSV с теми же столбцами)</span><input type="file" id="rtFile" accept=".xlsx,.xlsm,.csv"></label>' +
        '<label class="mdl-input"><span class="mdl-input-title">Что сделать с данными</span><select id="rtMode"><option value="append">добавить к опытам модели</option><option value="replace">заменить все данные</option><option value="same">те же данные, другие настройки</option></select></label>' +
        (kind() === 'table' ? '<label class="mdl-input"><span class="mdl-input-title">Метод</span><select id="rtMethod"><option value="">как сейчас</option><option value="auto">подобрать автоматически</option><option value="linear">линейная регрессия</option><option value="quadratic">квадратичная</option><option value="gp">гауссов процесс</option><option value="gbm">бустинг</option></select></label>' : '') +
        '<label class="mdl-input"><span class="mdl-input-title">Порог приёмки: ошибка «' + esc((outputs()[0] || {}).title || '') + '» не больше</span><input type="number" step="any" min="0" id="rtThreshold" value="' + (thr !== undefined ? esc(thr) : '') + '"></label>' +
        '<label class="mdl-input mdl-input--wide"><span class="mdl-input-title">Что изменилось</span><input type="text" id="rtNote" maxlength="300" placeholder="добавлены опыты сентября"></label>' +
        '</div>';
      var go = button('Переобучить', 'btn-primary mdl-run', retrain);
      rb.appendChild(go);
      panel.appendChild(rb);
    }
    // версии
    var vb = document.createElement('div');
    vb.className = 'mdl-card-box';
    var o = outputs()[0] || {};
    vb.innerHTML = '<h3>Версии</h3><div class="mdl-table-wrap"><table class="mdl-table"><thead><tr><th>Версия</th><th>Дата</th><th>Данных</th><th>Ошибка</th><th>Точнее среднего</th><th>Порог</th><th>Что изменилось</th><th></th></tr></thead><tbody>' +
      (v.history || []).slice().reverse().map(function (h) {
        var ho = (h.outputs || {})[o.name] || {};
        var cur = h.version === v.current_version;
        return '<tr' + (cur ? ' class="mdl-row-best"' : '') + '><td>' + h.version + (cur ? ' · текущая' : '') + '</td><td>' + fmtShort(h.created_at) + '</td><td>' + (h.n || '—') + '</td>' +
          '<td>' + (ho.mae !== undefined ? '±' + num(ho.mae) + unit(o.unit) : '—') + '</td><td>' + (typeof h.skill === 'number' ? Math.round(h.skill * 100) + ' %' : '—') + '</td>' +
          '<td>' + (h.passed === true ? 'пройден' : h.passed === false ? 'не пройден' : '—') + '</td><td>' + esc(h.note || '') + '</td>' +
          '<td>' + (!cur && can('write') ? '<button type="button" class="btn-register mdl-make-current" data-v="' + h.version + '">Сделать текущей</button>' : '') +
          '<button type="button" class="btn-register mdl-show-version" data-v="' + h.version + '">Открыть</button></td></tr>';
      }).join('') + '</tbody></table></div>';
    vb.querySelectorAll('.mdl-make-current').forEach(function (b) {
      b.addEventListener('click', function () { setCurrent(parseInt(b.dataset.v, 10)); });
    });
    vb.querySelectorAll('.mdl-show-version').forEach(function (b) {
      b.addEventListener('click', function () { var n = parseInt(b.dataset.v, 10); load(n === v.current_version ? null : n); });
    });
    panel.appendChild(vb);

    // «прогноз → факт»
    if (kind() !== 'kinetics') {
      var fb = document.createElement('div');
      fb.className = 'mdl-card-box';
      fb.id = 'mdlFacts';
      fb.innerHTML = '<h3>Прогноз → факт</h3><p class="mdl-muted">Поставили опыт — внесите результат: платформа сравнит его с прогнозом текущей версии.</p><div id="mdlFactsBody" class="mdl-muted">Загрузка…</div>';
      panel.appendChild(fb);
      loadFacts();
    }
    // данные
    if (can('read') && kind() !== 'onnx') {
      var dbx = document.createElement('div');
      dbx.className = 'mdl-card-box';
      dbx.innerHTML = '<h3>Данные версии ' + (state.version || v.current_version) + '</h3><div id="mdlDataBody" class="mdl-muted">Загрузка…</div>';
      panel.appendChild(dbx);
      loadData();
    }
  }

  function retrain() {
    var btn = document.querySelector('.mdl-retrain .mdl-run');
    var fileIn = document.getElementById('rtFile');
    var mode = document.getElementById('rtMode').value;
    var file = fileIn.files && fileIn.files[0];
    if (mode !== 'same' && !file) { toast('Приложите файл с опытами', 'err'); return; }
    var patch = {};
    var methodEl = document.getElementById('rtMethod');
    if (methodEl && methodEl.value) patch.method = methodEl.value;
    var thrEl = document.getElementById('rtThreshold');
    if (thrEl && thrEl.value !== '') patch.threshold = { output: (outputs()[0] || {}).name, max: parseFloat(thrEl.value) };
    busy(btn, true, 'Модель учится…');
    document.getElementById('mdlSkill').classList.add('mdl-skill--learning');
    var before = skillOf(state.view);
    API.teamModelRetrain(modelId, { mode: mode, note: document.getElementById('rtNote').value, spec_patch: JSON.stringify(patch) }, file)
      .then(function (res) {
        var after = res.version && res.version.skill;
        if (res.made_current) {
          state.prevSkill = before;
          var msg = 'Версия ' + res.version.version + ' обучена';
          if (typeof before === 'number' && typeof after === 'number') {
            var d = Math.round((after - before) * 100);
            msg += d > 0 ? ': точнее на ' + d + ' п.п.' : d < 0 ? ': точность ниже на ' + (-d) + ' п.п.' : ': точность прежняя';
          }
          toast(msg, 'ok');
        } else {
          toast(res.message || 'Новая версия сохранена кандидатом', 'warn');
        }
        state.version = null;
        state.lastPred = null;
        load(null, 'learn');
      })
      .catch(function (err) { toast(err.message, 'err'); })
      .then(function () {
        busy(btn, false);
        document.getElementById('mdlSkill').classList.remove('mdl-skill--learning');
      });
  }

  function loadFacts() {
    API.teamModelFacts(modelId).then(function (res) {
      state.facts = res;
      var body = document.getElementById('mdlFactsBody');
      if (!body) return;
      body.className = '';
      var outs = outputs();
      var html = '';
      var stats = res.stats || {};
      outs.forEach(function (o) {
        var s = stats[o.name];
        if (s) html += '<p>Замеров после обучения: <b>' + s.n + '</b>, реальная ошибка ±' + num(s.mae) + unit(o.unit) + ', в пределах ±2 ошибок модели — ' + Math.round(s.within_2sigma * 100) + ' %.</p>';
      });
      var items = res.items || [];
      if (items.length) {
        html += '<div class="mdl-table-wrap"><table class="mdl-table"><thead><tr><th>Дата</th><th>Условия</th>' +
          outs.map(function (o) { return '<th>' + esc(o.title || o.name) + ': прогноз → опыт</th>'; }).join('') + '<th>Версия</th></tr></thead><tbody>' +
          items.map(function (f) {
            var cond = inputs().map(function (i) { var v = (f.inputs || {})[i.name]; return v === undefined ? '' : esc(i.title || i.name) + ' ' + (typeof v === 'number' ? num(v) : esc(v)); }).filter(Boolean).join('; ');
            return '<tr><td>' + fmtShort(f.at) + '</td><td>' + cond + '</td>' + outs.map(function (o) {
              var m = (f.measured || {})[o.name], p = (f.predicted || {})[o.name];
              return '<td>' + (m === undefined ? '—' : num(p) + ' → <b>' + num(m) + '</b>') + '</td>';
            }).join('') + '<td>' + (f.version || '') + '</td></tr>';
          }).join('') + '</tbody></table></div>';
      } else if (!html) {
        html = '<p class="mdl-muted">Замеров пока нет.</p>';
      }
      if (can('write')) {
        html += '<div class="mdl-fact-form"><p class="mdl-small">Условия берутся из вкладки «Расчёт». Измерено:</p>' +
          outs.map(function (o) { return '<label class="mdl-input"><span class="mdl-input-title">' + esc(o.title || o.name) + unit(o.unit) + '</span><input type="number" step="any" data-out="' + esc(o.name) + '"></label>'; }).join('') +
          '<label class="mdl-input mdl-input--wide"><span class="mdl-input-title">Комментарий</span><input type="text" id="factNote" maxlength="300"></label></div>';
      }
      body.innerHTML = html;
      if (can('write')) {
        var add = button('Внести замер', 'btn-primary', function () {
          var measured = {};
          body.querySelectorAll('input[data-out]').forEach(function (i) { if (i.value !== '') measured[i.dataset.out] = parseFloat(i.value); });
          if (!Object.keys(measured).length) { toast('Укажите измеренное значение', 'err'); return; }
          var cond = {};
          inputs().forEach(function (i) { cond[i.name] = state.calc[i.name] !== undefined ? state.calc[i.name] : defaultValue(i); });
          busy(add, true, 'Сравниваем…');
          API.teamModelAddFact(modelId, { inputs: cond, measured: measured, note: (document.getElementById('factNote') || {}).value || '' })
            .then(function () { toast('Замер сохранён рядом с прогнозом', 'ok'); loadFacts(); })
            .catch(function (err) { toast(err.message, 'err'); })
            .then(function () { busy(add, false); });
        });
        body.appendChild(add);
      }
    }).catch(function () {
      var body = document.getElementById('mdlFactsBody');
      if (body) body.textContent = 'Не удалось загрузить замеры.';
    });
  }

  function loadData() {
    API.teamModelData(modelId, versionParam()).then(function (res) {
      state.data = res;
      var body = document.getElementById('mdlDataBody');
      if (!body) return;
      body.className = '';
      var excluded = {};
      (res.excluded || []).forEach(function (e) { excluded[e.row] = e.reason; });
      var rows = res.rows || [];
      var html = '<div class="mdl-table-wrap mdl-data-wrap"><table class="mdl-table mdl-data"><thead><tr>' + (can('write') ? '<th>Исключить</th>' : '') + '<th>#</th>' +
        res.columns.map(function (c) { return '<th>' + esc(c) + '</th>'; }).join('') + '</tr></thead><tbody>' +
        rows.slice(0, 500).map(function (r, i) {
          var ex = excluded[i] !== undefined;
          return '<tr' + (ex ? ' class="mdl-row-excluded" title="' + esc(excluded[i]) + '"' : '') + '>' + (can('write') ? '<td><input type="checkbox" data-row="' + i + '"' + (ex ? ' checked' : '') + '></td>' : '') +
            '<td>' + (i + 1) + '</td>' + r.map(function (v) { return '<td>' + esc(v) + '</td>'; }).join('') + '</tr>';
        }).join('') + '</tbody></table></div>' + (rows.length > 500 ? '<p class="mdl-muted">Показаны первые 500 строк из ' + rows.length + '.</p>' : '');
      body.innerHTML = html;
      if (can('write')) {
        var reason = document.createElement('input');
        reason.type = 'text';
        reason.placeholder = 'Почему исключены отмеченные строки (например: ошибка прибора)';
        reason.className = 'mdl-exclude-reason';
        body.appendChild(reason);
        var ex = button('Переобучить без отмеченных строк', 'btn-register', function () {
          var list = [];
          body.querySelectorAll('input[data-row]').forEach(function (c) {
            if (c.checked) {
              var i = parseInt(c.dataset.row, 10);
              list.push({ row: i, reason: excluded[i] !== undefined && !reason.value ? excluded[i] : (reason.value || 'исключено автором') });
            }
          });
          busy(ex, true, 'Модель учится…');
          var before = skillOf(state.view);
          API.teamModelRetrain(modelId, { mode: 'same', note: list.length ? 'исключено строк: ' + list.length : 'все строки возвращены', spec_patch: JSON.stringify({ excluded: list }) })
            .then(function (res) {
              state.prevSkill = res.made_current ? before : null;
              toast(res.made_current ? 'Версия ' + res.version.version + ' обучена' : (res.message || 'Версия сохранена кандидатом'), res.made_current ? 'ok' : 'warn');
              load(null, 'learn');
            })
            .catch(function (err) { toast(err.message, 'err'); })
            .then(function () { busy(ex, false); });
        });
        body.appendChild(ex);
      }
    }).catch(function (err) {
      var body = document.getElementById('mdlDataBody');
      if (body) body.textContent = err.message || 'Не удалось загрузить данные.';
    });
  }

  /* ---------- вкладки и загрузка ---------- */

  function switchTab(name) {
    state.tab = name;
    document.querySelectorAll('.mdl-tab').forEach(function (b) {
      var on = b.dataset.tab === name;
      b.classList.toggle('mdl-tab--active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    ['calc', 'optimize', 'learn', 'data'].forEach(function (t) {
      document.getElementById('tab-' + t).hidden = t !== name;
    });
    if (name === 'calc') renderCalc();
    if (name === 'optimize') renderOptimize();
    if (name === 'learn') {
      if (!state.facts && kind() !== 'kinetics') {
        API.teamModelFacts(modelId).then(function (res) { state.facts = res; if (state.tab === 'learn') renderLearn(); }).catch(function () {});
      }
      renderLearn();
    }
    if (name === 'data') renderData();
  }

  document.querySelectorAll('.mdl-tab').forEach(function (b) {
    b.addEventListener('click', function () { switchTab(b.dataset.tab); });
  });

  function load(version, tab) {
    state.version = version || null;
    return API.teamModelGet(modelId, version || undefined).then(function (view) {
      state.view = view;
      state.facts = null;
      renderHead();
      switchTab(tab || state.tab || 'calc');
    }).catch(function (err) {
      document.getElementById('mdlName').textContent = 'Модель недоступна';
      document.getElementById('mdlDesc').textContent = err.status === 404
        ? 'Модели нет или она ещё не опубликована. Если это ваша модель — войдите.'
        : err.status === 403
          ? 'Раздел «Модели» доступен только администраторам платформы.'
          : (err.message || 'Не удалось загрузить модель');
      document.querySelectorAll('.mdl-tabs, .mdl-panel').forEach(function (el) { el.hidden = true; });
    });
  }

  if (!modelId) {
    document.getElementById('mdlName').textContent = 'Модель не указана';
    return;
  }
  load(null);
  window.addEventListener('aios-auth-change', function () { state.lastPred = null; load(state.version); });
})();
