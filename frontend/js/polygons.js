/* ============================================================
   Страница «Полигоны» (polygons.html).

   - Список: примеры платформы (data/polygons/*.json) и свои полигоны
     (GET /api/polygons); «Создать полигон» открывает копию первого примера.
   - Сценарная модель: параметры портфеля по УГТ, сценарии и гипотеза →
     POST /api/polygons/simulate (Монте-Карло на сервере, без LLM) →
     проверка гипотезы по сценариям, таблица KPI с коридором p10–p90,
     график метрики по годам, распределение портфеля по УГТ.
   - ИИ-фокус-группа: идея + роли → POST /api/polygons/panel (нужен вход и
     подключённый LLM) → оценки, аргументы, условия, сводка.
   - Сохранение: POST /api/polygons (свой полигон; правка примера
     сохраняется как новый свой полигон), удаление — DELETE.
   Открытый полигон помнится в адресе (?id=pg-…).
   ============================================================ */

(function () {
  var API = window.AIOS_API;
  var grid = document.getElementById('pgGrid');
  var work = document.getElementById('pgWork');
  if (!API || !grid || !work) return;

  var $ = function (id) { return document.getElementById(id); };
  var COLORS = ['#5A6B82', '#1F5AA6', '#2E9D5B', '#B26A00', '#7A3FB0', '#C8372D', '#0F7C8C'];
  var PARAMS = [
    ['horizon_years', 'Горизонт, лет', 1],
    ['runs', 'Прогонов Монте-Карло', 10],
    ['intake_per_year', 'Новых проектов в год', 1],
    ['entry_level', 'УГТ новых проектов', 1],
    ['exit_level', 'Выход на рынок с УГТ', 1],
    ['annual_budget', 'Бюджет портфеля, тыс. ₽/год (0 — без лимита)', 1000],
    ['fixed_cost', 'Постоянные затраты, тыс. ₽/год', 1000],
    ['p_license', 'Доля выходов через лицензию (0–1)', 0.05],
    ['license_income', 'Доход от лицензии, тыс. ₽/год', 100],
    ['investment_per_startup', 'Инвестиции на стартап, тыс. ₽', 1000],
    ['stale_years', 'Застой: лет на одном УГТ', 1]
  ];
  var SC_FIELDS = [
    ['valley_boost', 'Шанс перехода на УГТ 3–5, %'],
    ['up_boost', 'Шанс перехода на всех УГТ, %'],
    ['drop_change', 'Закрытие проектов, %'],
    ['p_license_change', 'Доля лицензий, п.п.'],
    ['intake_per_year', 'Новых проектов в год'],
    ['annual_budget', 'Бюджет, тыс. ₽/год'],
    ['fixed_cost', 'Постоянные затраты, тыс. ₽/год']
  ];

  var state = { examples: [], items: [], metrics: {}, current: null, sim: null, chartMetric: 'startups' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmt(v, d) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    var a = Math.abs(v);
    var digits = d != null ? d : (a >= 100 ? 0 : a >= 10 ? 1 : 2);
    return Number(v).toLocaleString('ru-RU', { maximumFractionDigits: digits });
  }
  function session() {
    try { return JSON.parse(localStorage.getItem('aios-auth-v1') || 'null'); } catch (e) { return null; }
  }
  function token() { var s = session(); return s && s.token; }
  function msg(text, isError) {
    var el = $('pgMsg');
    el.textContent = text || '';
    el.classList.toggle('pg-msg--err', !!isError);
  }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }

  /* ---------- список ---------- */

  function card(pg) {
    var a = document.createElement('button');
    a.type = 'button';
    a.className = 'mdl-card imdl-card';
    var hyp = pg.hypothesis && pg.hypothesis.statement;
    a.innerHTML =
      '<span class="mdl-card-top"><span class="mdl-kind">' + (pg.example ? 'пример' : 'мой полигон') + '</span>' +
      '<span class="mdl-status ' + (pg.example ? 'mdl-status--published' : '') + '">' +
      (pg.scenarios ? (pg.scenarios.length + 1) + ' сценария' : '') + '</span></span>' +
      '<span class="mdl-card-name">' + esc(pg.name) + '</span>' +
      '<span class="mdl-card-out">' + esc(hyp ? 'Гипотеза: ' + hyp : (pg.description || '').slice(0, 160)) + '</span>' +
      '<span class="mdl-card-foot">' + esc([pg.author, (pg.tags || []).join(', ')].filter(Boolean).join(' · ')) + '</span>';
    a.addEventListener('click', function () { open(pg); });
    return a;
  }

  function loadList(initial) {
    grid.innerHTML = '<div class="model-card model-card--skeleton"></div><div class="model-card model-card--skeleton"></div>';
    return API.request('GET', '/polygons', undefined, token()).then(function (data) {
      state.examples = data.examples || [];
      state.items = data.items || [];
      state.metrics = data.metrics || {};
      grid.innerHTML = '';
      state.examples.concat(state.items).forEach(function (pg) { grid.appendChild(card(pg)); });
      var wanted = initial ? new URLSearchParams(location.search).get('id') : null;
      if (wanted) {
        var found = state.examples.concat(state.items).filter(function (p) { return p.id === wanted; })[0];
        if (found) open(found);
      }
    }).catch(function () {
      grid.innerHTML = '<div class="models-error">Не удалось загрузить полигоны.</div>';
    });
  }

  /* ---------- форма ---------- */

  function open(pg) {
    state.current = clone(pg);
    state.sim = null;
    var c = state.current;
    $('pgName').value = c.name || '';
    $('pgDesc').value = c.description || '';
    $('pgMeta').textContent = c.example
      ? 'Пример платформы · изменения сохранятся как ваш полигон'
      : 'Мой полигон' + (c.updated_at ? ' · сохранён ' + new Date(c.updated_at * 1000).toLocaleString('ru-RU') : '');
    $('pgDelete').hidden = !!c.example || !c.id;
    var h = c.hypothesis || {};
    $('hypStatement').value = h.statement || '';
    $('hypMetric').innerHTML = Object.keys(state.metrics).map(function (k) {
      return '<option value="' + k + '"' + (k === h.metric ? ' selected' : '') + '>' + esc(state.metrics[k]) + '</option>';
    }).join('');
    $('hypOp').value = h.op || '>=';
    $('hypValue').value = h.value != null ? h.value : '';
    renderParams();
    renderScenarios();
    renderRoles();
    $('pgIdea').value = c.idea || '';
    $('pgResults').innerHTML = '<div class="pg-empty">Нажмите «Запустить симуляцию» — здесь появятся проверка гипотезы, сравнение сценариев и графики.</div>';
    $('pgPanelOut').innerHTML = c.last_panel ? '' : '<div class="pg-empty">Каждый участник оценит идею по 10-балльной шкале со своей стороны: аргументы за и против, условие поддержки и главный вопрос к авторам. Нужен вход и подключённый LLM.</div>';
    if (c.last_panel) renderPanel(c.last_panel);
    msg('');
    work.hidden = false;
    if (c.id) history.replaceState(null, '', '?id=' + encodeURIComponent(c.id));
    work.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function renderParams() {
    var p = state.current.params || {};
    $('pgParams').innerHTML = PARAMS.map(function (f) {
      return '<label class="mdl-field"><span>' + esc(f[1]) + '</span><input type="number" step="' + f[2] +
        '" data-param="' + f[0] + '" value="' + (p[f[0]] != null ? p[f[0]] : '') + '"></label>';
    }).join('');
    var init = p.initial || {};
    var html = '';
    for (var l = 1; l <= 8; l++) {
      html += '<label class="pg-lvl"><span>УГТ ' + l + '</span><input type="number" min="0" step="1" data-init="' + l +
        '" value="' + (init[l] || 0) + '"></label>';
    }
    $('pgInitial').innerHTML = html;
    var lv = p.levels || {};
    var rows = '<thead><tr><th>УГТ</th><th>Переход за год</th><th>Закрытие за год</th><th>Затраты, тыс. ₽</th></tr></thead><tbody>';
    for (var k = 1; k <= 8; k++) {
      var x = lv[k] || {};
      rows += '<tr><td>' + k + ' → ' + (k + 1) + '</td>' +
        '<td><input type="number" min="0" max="1" step="0.01" data-level="' + k + '" data-key="p_up" value="' + (x.p_up != null ? +(+x.p_up).toFixed(3) : '') + '"></td>' +
        '<td><input type="number" min="0" max="1" step="0.01" data-level="' + k + '" data-key="p_drop" value="' + (x.p_drop != null ? +(+x.p_drop).toFixed(3) : '') + '"></td>' +
        '<td><input type="number" min="0" step="100" data-level="' + k + '" data-key="cost" value="' + (x.cost != null ? x.cost : '') + '"></td></tr>';
    }
    $('pgLevels').innerHTML = rows + '</tbody>';
  }

  function renderScenarios() {
    var list = state.current.scenarios || [];
    $('pgScenarios').innerHTML = list.map(function (sc, i) {
      return '<div class="pg-sc" data-sc="' + i + '" style="--pg-c:' + COLORS[(i + 1) % COLORS.length] + '">' +
        '<div class="pg-sc-head"><input class="pg-sc-name" data-f="name" value="' + esc(sc.name) + '" maxlength="120" aria-label="Название сценария">' +
        '<button type="button" class="pg-sc-del" data-del="' + i + '" aria-label="Удалить сценарий">✕</button></div>' +
        '<textarea data-f="description" rows="2" maxlength="600" placeholder="Что меняем в управлении">' + esc(sc.description || '') + '</textarea>' +
        '<div class="pg-sc-fields">' + SC_FIELDS.map(function (f) {
          return '<label class="mdl-field"><span>' + esc(f[1]) + '</span><input type="number" step="any" data-f="' + f[0] +
            '" value="' + (sc[f[0]] != null ? sc[f[0]] : '') + '" placeholder="' + (/boost|change/.test(f[0]) ? '0' : 'как в базе') + '"></label>';
        }).join('') + '</div></div>';
    }).join('');
    $('hypScenario').innerHTML = '<option value="">все сценарии</option>' + list.map(function (sc) {
      return '<option value="' + esc(sc.id) + '">' + esc(sc.name) + '</option>';
    }).join('');
    $('hypScenario').value = (state.current.hypothesis || {}).scenario_id || '';
  }

  function renderRoles() {
    var roles = state.current.panel || [];
    $('pgRoles').innerHTML = roles.map(function (r, i) {
      return '<div class="pg-role" data-role="' + i + '"><div class="pg-sc-head">' +
        '<input class="pg-sc-name" data-r="name" value="' + esc(r.name) + '" maxlength="80" aria-label="Роль">' +
        '<button type="button" class="pg-sc-del" data-rdel="' + i + '" aria-label="Удалить участника">✕</button></div>' +
        '<textarea data-r="persona" rows="2" maxlength="400">' + esc(r.persona || '') + '</textarea></div>';
    }).join('');
  }

  /* собирает полигон из формы */
  function collect() {
    var c = state.current;
    c.name = $('pgName').value.trim() || 'Полигон без названия';
    c.description = $('pgDesc').value.trim();
    var p = c.params || (c.params = {});
    work.querySelectorAll('[data-param]').forEach(function (el) {
      if (el.value !== '') p[el.dataset.param] = parseFloat(el.value);
    });
    p.initial = {};
    work.querySelectorAll('[data-init]').forEach(function (el) { p.initial[el.dataset.init] = parseInt(el.value || '0', 10); });
    p.levels = p.levels || {};
    work.querySelectorAll('[data-level]').forEach(function (el) {
      var lv = p.levels[el.dataset.level] || (p.levels[el.dataset.level] = {});
      if (el.value !== '') lv[el.dataset.key] = parseFloat(el.value);
    });
    c.scenarios = Array.prototype.map.call(work.querySelectorAll('.pg-sc'), function (box, i) {
      var sc = { id: (c.scenarios[i] && c.scenarios[i].id) || ('s' + (i + 1)) };
      box.querySelectorAll('[data-f]').forEach(function (el) {
        var f = el.dataset.f;
        if (f === 'name' || f === 'description') sc[f] = el.value.trim();
        else if (el.value !== '') sc[f] = parseFloat(el.value);
      });
      return sc;
    });
    c.hypothesis = {
      statement: $('hypStatement').value.trim(),
      metric: $('hypMetric').value,
      op: $('hypOp').value,
      value: $('hypValue').value === '' ? 0 : parseFloat($('hypValue').value),
      scenario_id: $('hypScenario').value
    };
    c.panel = Array.prototype.map.call(work.querySelectorAll('.pg-role'), function (box, i) {
      return {
        id: (c.panel[i] && c.panel[i].id) || ('r' + (i + 1)),
        name: box.querySelector('[data-r="name"]').value.trim(),
        persona: box.querySelector('[data-r="persona"]').value.trim()
      };
    }).filter(function (r) { return r.name; });
    c.idea = $('pgIdea').value.trim();
    return c;
  }

  /* ---------- симуляция ---------- */

  function run() {
    var c = collect();
    var btn = $('pgRun');
    btn.disabled = true;
    msg('Считаем ' + (c.params.runs || 400) + ' прогонов по ' + (c.scenarios.length + 1) + ' сценариям…');
    API.request('POST', '/polygons/simulate', { params: c.params, scenarios: c.scenarios, hypothesis: c.hypothesis })
      .then(function (sim) {
        state.sim = sim;
        c.last_run = { at: Math.floor(Date.now() / 1000), summary: summaryOf(sim) };
        renderResults();
        msg('Готово за ' + fmt(sim.elapsed_ms / 1000, 1) + ' с. Сохраните полигон, чтобы вернуться к нему.');
      })
      .catch(function (e) { msg((e && e.message) || 'Не удалось запустить симуляцию', true); })
      .then(function () { btn.disabled = false; });
  }

  function summaryOf(sim) {
    return sim.results.map(function (r) {
      return { id: r.id, name: r.name, hypothesis_probability: r.hypothesis_probability,
        startups: r.final.startups.mean, licenses: r.final.licenses.mean, roi: r.final.roi.mean };
    });
  }

  function renderResults() {
    var sim = state.sim;
    var hyp = sim.hypothesis;
    var out = '';
    if (hyp.statement || hyp.value) {
      out += '<div class="pg-hyp"><div class="pg-hyp-title">Проверка гипотезы</div>' +
        '<div class="pg-hyp-text">«' + esc(hyp.statement || (sim.metrics[hyp.metric] + ' ' + hyp.op + ' ' + hyp.value)) + '»</div>' +
        '<div class="pg-hyp-rule">' + esc(sim.metrics[hyp.metric]) + (hyp.op === '>=' ? ' не меньше ' : ' не больше ') + fmt(hyp.value) +
        ' к концу горизонта (' + sim.params.horizon_years + ' лет)</div>';
      sim.results.forEach(function (r, i) {
        var pr = r.hypothesis_probability || 0;
        var focus = hyp.scenario_id && hyp.scenario_id === r.id;
        var verdict = pr >= 0.7 ? 'подтверждается' : pr >= 0.4 ? 'под вопросом' : 'не подтверждается';
        out += '<div class="pg-prob' + (focus ? ' pg-prob--focus' : '') + '"><span class="pg-prob-name"><i style="background:' + COLORS[i % COLORS.length] + '"></i>' + esc(r.name) + '</span>' +
          '<span class="pg-prob-bar"><span style="width:' + Math.round(pr * 100) + '%;background:' + COLORS[i % COLORS.length] + '"></span></span>' +
          '<span class="pg-prob-val"><b>' + Math.round(pr * 100) + ' %</b> ' + verdict + '</span></div>';
      });
      out += '<p class="pg-hint">Доля прогонов Монте-Карло, в которых цель достигнута. ≥ 70 % — гипотеза устойчива к случайностям, 40–70 % — зависит от удачи, &lt; 40 % — пересмотрите решение или цель.</p></div>';
    }
    var keys = ['startups', 'licenses', 'exits', 'investments', 'income', 'cost', 'roi', 'stale', 'active'];
    out += '<div class="pg-table-wrap"><table class="pg-table"><thead><tr><th>Итог за ' + sim.params.horizon_years + ' лет</th>' +
      sim.results.map(function (r, i) { return '<th><i style="background:' + COLORS[i % COLORS.length] + '"></i>' + esc(r.name) + '</th>'; }).join('') +
      '</tr></thead><tbody>' + keys.map(function (k) {
        return '<tr><td>' + esc(sim.metrics[k]) + '</td>' + sim.results.map(function (r) {
          var s = r.final[k];
          var d = k === 'roi' ? 2 : null;
          return '<td><b>' + fmt(s.mean, d) + '</b><span>' + fmt(s.p10, d) + '–' + fmt(s.p90, d) + '</span></td>';
        }).join('') + '</tr>';
      }).join('') + '</tbody></table></div><p class="pg-hint">Среднее по прогонам и коридор p10–p90 (80 % исходов).</p>';
    out += '<div class="pg-chart-head"><span>Динамика по годам</span><select id="pgChartMetric">' +
      ['startups', 'licenses', 'exits', 'active', 'stale', 'cost', 'income', 'investments'].map(function (k) {
        return '<option value="' + k + '"' + (k === state.chartMetric ? ' selected' : '') + '>' + esc(sim.metrics[k]) + '</option>';
      }).join('') + '</select></div><div id="pgChart"></div>';
    out += '<div class="pg-chart-head"><span>Портфель в конце горизонта по УГТ (среднее)</span></div><div id="pgLevelsChart"></div>';
    $('pgResults').innerHTML = out;
    $('pgChartMetric').addEventListener('change', function (e) { state.chartMetric = e.target.value; drawLine(); });
    drawLine();
    drawLevels();
  }

  function drawLine() {
    var sim = state.sim;
    var m = state.chartMetric;
    var W = 560, H = 220, L = 48, R = 12, T = 12, B = 28;
    var years = sim.params.horizon_years;
    var max = 0;
    sim.results.forEach(function (r) { r.series[m].forEach(function (s) { max = Math.max(max, s.p90, s.mean); }); });
    max = max || 1;
    var x = function (i) { return L + (years === 1 ? (W - L - R) / 2 : i * (W - L - R) / (years - 1)); };
    var y = function (v) { return T + (H - T - B) * (1 - v / max); };
    var svg = '<svg class="pg-svg" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(sim.metrics[m]) + ' по годам">';
    for (var g = 0; g <= 4; g++) {
      var gv = max * g / 4;
      svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(gv) + '" y2="' + y(gv) + '" class="pg-grid"/>' +
        '<text x="' + (L - 6) + '" y="' + (y(gv) + 4) + '" class="pg-axis" text-anchor="end">' + fmt(gv, gv >= 100 ? 0 : 1) + '</text>';
    }
    for (var t = 0; t < years; t++) {
      svg += '<text x="' + x(t) + '" y="' + (H - 8) + '" class="pg-axis" text-anchor="middle">' + (t + 1) + '-й год</text>';
    }
    sim.results.forEach(function (r, i) {
      var c = COLORS[i % COLORS.length];
      var s = r.series[m];
      var band = s.map(function (p, k) { return x(k) + ',' + y(p.p90); }).concat(
        s.slice().reverse().map(function (p, k) { return x(years - 1 - k) + ',' + y(p.p10); }));
      svg += '<polygon points="' + band.join(' ') + '" fill="' + c + '" opacity="0.10"/>';
      svg += '<polyline points="' + s.map(function (p, k) { return x(k) + ',' + y(p.mean); }).join(' ') +
        '" fill="none" stroke="' + c + '" stroke-width="2.5" stroke-linejoin="round"/>';
      s.forEach(function (p, k) {
        svg += '<circle cx="' + x(k) + '" cy="' + y(p.mean) + '" r="3" fill="' + c + '"><title>' + esc(r.name) + ', ' + (k + 1) +
          '-й год: ' + fmt(p.mean) + ' (' + fmt(p.p10) + '–' + fmt(p.p90) + ')</title></circle>';
      });
    });
    $('pgChart').innerHTML = svg + '</svg>' + legend();
  }

  function drawLevels() {
    var sim = state.sim;
    var W = 560, H = 170, L = 30, B = 24, T = 8;
    var max = 0;
    sim.results.forEach(function (r) { r.levels.forEach(function (v) { max = Math.max(max, v); }); });
    max = max || 1;
    var groupW = (W - L) / 9;
    var barW = Math.max(3, (groupW - 8) / sim.results.length);
    var svg = '<svg class="pg-svg" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Портфель по УГТ">';
    for (var lv = 0; lv < 9; lv++) {
      var gx = L + lv * groupW + 4;
      sim.results.forEach(function (r, i) {
        var h = (H - T - B) * r.levels[lv] / max;
        svg += '<rect x="' + (gx + i * barW) + '" y="' + (H - B - h) + '" width="' + (barW - 1) + '" height="' + h +
          '" fill="' + COLORS[i % COLORS.length] + '" rx="1.5"><title>' + esc(r.name) + ', УГТ ' + (lv + 1) + ': ' + fmt(r.levels[lv]) + '</title></rect>';
      });
      svg += '<text x="' + (gx + groupW / 2 - 4) + '" y="' + (H - 8) + '" class="pg-axis" text-anchor="middle">' + (lv + 1) + '</text>';
    }
    $('pgLevelsChart').innerHTML = svg + '</svg>';
  }

  function legend() {
    return '<div class="pg-legend">' + state.sim.results.map(function (r, i) {
      return '<span><i style="background:' + COLORS[i % COLORS.length] + '"></i>' + esc(r.name) + '</span>';
    }).join('') + '<span class="pg-legend-note">линия — среднее, тень — коридор p10–p90</span></div>';
  }

  /* ---------- фокус-группа ---------- */

  function runPanel() {
    var c = collect();
    if (!token()) { msg('Чтобы собрать ИИ-фокус-группу, войдите на платформу.', true); return; }
    if ((c.idea || '').length < 10) { msg('Опишите идею хотя бы одной фразой.', true); return; }
    var btn = $('pgPanelRun');
    btn.disabled = true;
    $('pgPanelOut').innerHTML = '<div class="pg-empty">Участники читают идею и готовят мнения — это займёт до минуты…</div>';
    var body = { idea: c.idea, panel: c.panel };
    if ($('pgUseSim').checked && state.sim) body.simulation = state.sim;
    API.request('POST', '/polygons/panel', body, token())
      .then(function (res) {
        c.last_panel = res;
        renderPanel(res);
        msg('Фокус-группа завершена. Сохраните полигон, чтобы сохранить мнения.');
      })
      .catch(function (e) {
        $('pgPanelOut').innerHTML = '<div class="pg-empty pg-msg--err">' + esc((e && e.message) || 'Фокус-группа не состоялась') + '</div>';
      })
      .then(function () { btn.disabled = false; });
  }

  function renderPanel(res) {
    var s = res.summary || {};
    var votes = s.votes || {};
    var html = '<div class="pg-hyp"><div class="pg-hyp-title">Итог фокус-группы</div>' +
      '<div class="pg-hyp-text">' + esc(s.decision || '') + (s.average_score != null ? ' · средняя оценка ' + fmt(s.average_score, 1) + ' из 10' : '') + '</div>' +
      '<div class="pg-hyp-rule">поддержать — ' + (votes['поддержать'] || 0) + ', доработать — ' + (votes['доработать'] || 0) +
      ', отклонить — ' + (votes['отклонить'] || 0) + '</div></div>';
    html += (res.opinions || []).map(function (o) {
      var cls = o.verdict === 'поддержать' ? 'ok' : o.verdict === 'отклонить' ? 'bad' : 'mid';
      var list = function (arr, title) {
        return arr && arr.length ? '<div class="pg-op-list"><b>' + title + '</b><ul>' + arr.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul></div>' : '';
      };
      return '<div class="pg-op pg-op--' + cls + '"><div class="pg-op-head"><span class="pg-op-role">' + esc(o.role) + '</span>' +
        '<span class="pg-op-score">' + (o.score != null ? o.score + '/10' : '—') + (o.verdict ? ' · ' + esc(o.verdict) : '') + '</span></div>' +
        (o.error ? '<p class="pg-msg--err">' + esc(o.error) + '</p>' : '') +
        (o.text ? '<p>' + esc(o.text) + '</p>' : '') +
        list(o.pros, 'За') + list(o.cons, 'Против') +
        (o.condition ? '<p><b>Поддержу, если:</b> ' + esc(o.condition) + '</p>' : '') +
        (o.question ? '<p><b>Вопрос к авторам:</b> ' + esc(o.question) + '</p>' : '') + '</div>';
    }).join('');
    $('pgPanelOut').innerHTML = html;
  }

  /* ---------- сохранение ---------- */

  function save() {
    if (!token()) { msg('Чтобы сохранить полигон, войдите на платформу.', true); return; }
    var c = collect();
    var body = clone(c);
    if (c.example) delete body.id;
    API.request('POST', '/polygons', body, token())
      .then(function (saved) {
        state.current = saved;
        msg('Полигон сохранён в «Мои полигоны».');
        $('pgDelete').hidden = false;
        $('pgMeta').textContent = 'Мой полигон · сохранён ' + new Date(saved.updated_at * 1000).toLocaleString('ru-RU');
        history.replaceState(null, '', '?id=' + encodeURIComponent(saved.id));
        var sim = state.sim;
        return loadList().then(function () {
          state.current = saved;
          state.sim = sim;
        });
      })
      .catch(function (e) { msg((e && e.message) || 'Не удалось сохранить', true); });
  }

  function remove() {
    var c = state.current;
    if (!c || !c.id || c.example || !confirm('Удалить полигон «' + c.name + '»?')) return;
    API.request('DELETE', '/polygons/' + encodeURIComponent(c.id), undefined, token())
      .then(function () {
        work.hidden = true;
        history.replaceState(null, '', location.pathname);
        loadList();
      })
      .catch(function (e) { msg((e && e.message) || 'Не удалось удалить', true); });
  }

  /* ---------- события ---------- */

  $('pgRun').addEventListener('click', run);
  $('pgSave').addEventListener('click', save);
  $('pgDelete').addEventListener('click', remove);
  $('pgPanelRun').addEventListener('click', runPanel);
  $('pgClose').addEventListener('click', function () {
    work.hidden = true;
    history.replaceState(null, '', location.pathname);
  });
  $('pgNew').addEventListener('click', function () {
    var base = state.examples[0];
    if (!base) return;
    var fresh = clone(base);
    delete fresh.id;
    fresh.example = false;
    fresh.name = 'Новый полигон';
    fresh.description = '';
    fresh.author = '';
    fresh.tags = [];
    fresh.scenarios = [{ id: 's1', name: 'Моё решение', description: '', valley_boost: 20 }];
    fresh.hypothesis = { statement: '', metric: 'startups', op: '>=', value: 5, scenario_id: 's1' };
    fresh.idea = '';
    delete fresh.last_panel;
    open(fresh);
    $('pgName').focus();
    $('pgName').select();
  });
  $('pgAddScenario').addEventListener('click', function () {
    collect();
    var n = state.current.scenarios.length;
    if (n >= 6) { msg('Не больше шести сценариев в одном полигоне.', true); return; }
    state.current.scenarios.push({ id: 's' + Date.now().toString(36), name: 'Сценарий ' + (n + 1), description: '' });
    renderScenarios();
  });
  $('pgScenarios').addEventListener('click', function (e) {
    var del = e.target.closest('[data-del]');
    if (!del) return;
    collect();
    state.current.scenarios.splice(+del.dataset.del, 1);
    renderScenarios();
  });
  $('pgAddRole').addEventListener('click', function () {
    collect();
    if (state.current.panel.length >= 8) { msg('Не больше восьми участников.', true); return; }
    state.current.panel.push({ id: 'r' + Date.now().toString(36), name: 'Новый участник', persona: 'кто это и на что смотрит' });
    renderRoles();
  });
  $('pgRoles').addEventListener('click', function (e) {
    var del = e.target.closest('[data-rdel]');
    if (!del) return;
    collect();
    state.current.panel.splice(+del.dataset.rdel, 1);
    renderRoles();
  });
  work.querySelectorAll('.pg-tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      work.querySelectorAll('.pg-tab').forEach(function (t) { t.classList.toggle('pg-tab--on', t === tab); });
      work.querySelectorAll('.pg-pane').forEach(function (p) { p.hidden = p.dataset.pane !== tab.dataset.tab; });
    });
  });
  window.addEventListener('aios-auth-change', function () { loadList(false); });

  loadList(true);
})();
