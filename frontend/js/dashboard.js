/* ============================================================
   «Рабочий стол проректора по инновациям» (index.html).

   Данные — data/dashboard.json (тестовые данные вуза). Участник
   сессии меняет их под свой университет двумя способами:
     1) правит data/dashboard.json в репозитории (видят все);
     2) кнопкой «Загрузить свои данные» подставляет свой JSON той же
        структуры — он хранится только в этом браузере (localStorage).

   Виджеты:
     - Патенты и РИД: количество, перспективность, затраты на поддержку;
     - Портфель по УГТ: столбцы 1–9, правило «на одном уровне
       stale_years лет и дольше → красный, кандидат на закрытие»;
     - Цепочка финансирования: РНФ → Гранты → Стартап-студия → Венчур;
     - Стартап-коллективы: грантовые / внутренние / госзадание;
     - Венчурный билдер: трекер обязательств стартапов.
   ============================================================ */

(function () {
  var DATA_URL = '/data/dashboard.json';
  var LOCAL_KEY = 'aios-dashboard-data-v1';

  var PERSPECTIVE = { high: 'Высокая', low: 'Низкая' };
  var TEAM_FUNDING = [
    { key: 'grant', title: 'Грантовые' },
    { key: 'internal', title: 'Внутреннее финансирование' },
    { key: 'state', title: 'Госзадание' }
  ];
  var VENTURE_STATUS = {
    on_track: 'В графике',
    risk: 'Риск',
    overdue: 'Просрочено',
    done: 'Выполнено'
  };

  var state = {
    data: null,
    source: 'demo',
    patentFilter: 'all',
    trlFilter: null,
    staleOnly: false,
    fundingStage: null,
    teamFilter: null,
    ventureFilter: 'all'
  };

  /* ---------- утилиты ---------- */

  function el(id) { return document.getElementById(id); }

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function money(thousands) {
    var n = Number(thousands) || 0;
    if (n >= 1000) return (n / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + ' млн ₽';
    return n.toLocaleString('ru-RU') + ' тыс. ₽';
  }

  function plural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
    return many;
  }

  function fmtDate(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return '—';
    return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' });
  }

  function today() {
    var asOf = state.data && state.data.as_of ? new Date(state.data.as_of) : null;
    return asOf && !isNaN(asOf) ? asOf : new Date();
  }

  /* Сколько лет проект стоит на текущем УГТ */
  function yearsOnLevel(p) {
    var since = new Date(p.trl_since);
    if (isNaN(since)) return 0;
    return (today() - since) / (365.25 * 24 * 3600 * 1000);
  }

  function staleYears() {
    var n = Number(state.data && state.data.stale_years);
    return n > 0 ? n : 2;
  }

  function isStale(p) { return yearsOnLevel(p) >= staleYears(); }

  /* Статус обязательства: явный из данных, иначе — по прогрессу и сроку */
  function ventureStatus(row) {
    if (row.status && VENTURE_STATUS[row.status]) return row.status;
    if ((Number(row.progress) || 0) >= 100) return 'done';
    var due = new Date(row.due);
    if (!isNaN(due) && due < today()) return 'overdue';
    return (Number(row.progress) || 0) < 30 ? 'risk' : 'on_track';
  }

  function arr(key) {
    var v = state.data && state.data[key];
    return Array.isArray(v) ? v : [];
  }

  /* ---------- тултип ---------- */

  var tip = el('dashTip');

  function showTip(evt, html) {
    if (!tip) return;
    tip.innerHTML = html;
    tip.hidden = false;
    var x = evt.clientX + 14, y = evt.clientY + 14;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - 14;
    if (y + h > window.innerHeight - 8) y = evt.clientY - h - 14;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }

  function hideTip() { if (tip) tip.hidden = true; }

  /* ---------- KPI ---------- */

  function renderKpi() {
    var patents = arr('patents');
    var projects = arr('projects');
    var teams = arr('teams');
    var high = patents.filter(function (p) { return p.perspective === 'high'; }).length;
    var cost = patents.reduce(function (s, p) { return s + (Number(p.support_cost) || 0); }, 0);
    var lowCost = patents.filter(function (p) { return p.perspective === 'low'; })
      .reduce(function (s, p) { return s + (Number(p.support_cost) || 0); }, 0);
    var stale = projects.filter(isStale).length;
    var overdue = arr('venture_tracker').filter(function (r) { return ventureStatus(r) === 'overdue'; }).length;

    var tiles = [
      { label: 'Патенты и РИД', value: patents.length, note: high + ' с высокой перспективностью' },
      { label: 'Затраты на поддержку РИД', value: money(cost), note: 'из них ' + money(lowCost) + ' — низкая перспективность' },
      { label: 'Проекты в портфеле', value: projects.length, note: 'средний УГТ ' + avgTrl(projects) },
      { label: 'Кандидаты на закрытие', value: stale, note: 'на одном УГТ ' + staleYears() + ' ' + plural(staleYears(), 'год', 'года', 'лет') + ' и дольше', alert: stale > 0 },
      { label: 'Стартап-коллективы', value: teams.length, note: teams.reduce(function (s, t) { return s + (Number(t.members) || 0); }, 0) + ' человек в командах' },
      { label: 'Просроченные обязательства', value: overdue, note: 'по трекеру венчурного билдера', alert: overdue > 0 }
    ];
    el('kpiRow').innerHTML = tiles.map(function (t) {
      return '<div class="kpi' + (t.alert ? ' kpi--alert' : '') + '">' +
        '<div class="kpi-label">' + esc(t.label) + '</div>' +
        '<div class="kpi-value">' + esc(t.value) + '</div>' +
        '<div class="kpi-note">' + esc(t.note) + '</div></div>';
    }).join('');
  }

  function avgTrl(projects) {
    if (!projects.length) return '—';
    var s = projects.reduce(function (a, p) { return a + (Number(p.trl) || 0); }, 0);
    return (s / projects.length).toLocaleString('ru-RU', { maximumFractionDigits: 1 });
  }

  /* ---------- Патенты и РИД ---------- */

  function renderPatents() {
    var patents = arr('patents');
    var high = patents.filter(function (p) { return p.perspective === 'high'; });
    var low = patents.filter(function (p) { return p.perspective === 'low'; });
    var sum = function (list) { return list.reduce(function (s, p) { return s + (Number(p.support_cost) || 0); }, 0); };
    var total = patents.length || 1;

    el('patentsSplit').innerHTML =
      '<div class="split-track">' +
        '<div class="split-seg split-seg--high" style="flex:' + high.length + '" data-tip="high"></div>' +
        '<div class="split-seg split-seg--low" style="flex:' + low.length + '" data-tip="low"></div>' +
      '</div>' +
      '<div class="split-labels">' +
        '<span><i class="sw sw--high"></i>Высокая: <b>' + high.length + '</b> (' + Math.round(high.length / total * 100) + '%) · ' + money(sum(high)) + '/год</span>' +
        '<span><i class="sw sw--low"></i>Низкая: <b>' + low.length + '</b> (' + Math.round(low.length / total * 100) + '%) · ' + money(sum(low)) + '/год</span>' +
      '</div>';

    el('patentsSplit').querySelectorAll('.split-seg').forEach(function (seg) {
      var key = seg.getAttribute('data-tip');
      var list = key === 'high' ? high : low;
      seg.addEventListener('mousemove', function (e) {
        showTip(e, '<b>' + PERSPECTIVE[key] + ' перспективность</b><br>' + list.length + ' РИД · ' + money(sum(list)) + ' в год');
      });
      seg.addEventListener('mouseleave', hideTip);
      seg.addEventListener('click', function () { setChip('patents', key); });
    });

    var rows = patents.filter(function (p) {
      return state.patentFilter === 'all' || p.perspective === state.patentFilter;
    }).sort(function (a, b) { return (b.support_cost || 0) - (a.support_cost || 0); });

    el('patentsTable').innerHTML =
      '<thead><tr><th>РИД</th><th>Тип / год</th><th>Перспективность</th><th class="num">Поддержка, в год</th></tr></thead><tbody>' +
      rows.map(function (p) {
        return '<tr><td><div class="cell-title">' + esc(p.title) + '</div><div class="cell-sub">' + esc(p.id) + ' · ' + esc(p.department) +
          (p.licensee ? ' · лицензиат: ' + esc(p.licensee) : '') + '</div></td>' +
          '<td>' + esc(p.type) + '<div class="cell-sub">' + esc(p.year) + '</div></td>' +
          '<td><span class="pill pill--' + (p.perspective === 'high' ? 'ok' : 'warn') + '">' + esc(PERSPECTIVE[p.perspective] || '—') + '</span></td>' +
          '<td class="num">' + money(p.support_cost) + '</td></tr>';
      }).join('') +
      (rows.length ? '' : '<tr><td colspan="4" class="empty">Нет РИД по выбранному фильтру</td></tr>') +
      '</tbody>';
  }

  /* ---------- Портфель по УГТ ---------- */

  function renderTrl() {
    var projects = arr('projects');
    el('staleYears').textContent = staleYears();
    var levels = [];
    for (var i = 1; i <= 9; i++) {
      var on = projects.filter(function (p) { return Number(p.trl) === i; });
      levels.push({ trl: i, ok: on.filter(function (p) { return !isStale(p); }).length, stale: on.filter(isStale).length });
    }
    var max = Math.max.apply(null, levels.map(function (l) { return l.ok + l.stale; }).concat([1]));

    el('trlChart').innerHTML = levels.map(function (l) {
      var total = l.ok + l.stale;
      var active = state.trlFilter === l.trl;
      return '<button type="button" class="trl-col' + (active ? ' is-active' : '') + (state.trlFilter && !active ? ' is-dim' : '') + '" data-trl="' + l.trl + '" aria-label="УГТ ' + l.trl + ': ' + total + ' проектов">' +
        '<span class="trl-count">' + (total || '') + '</span>' +
        '<span class="trl-bar" style="height:' + (total / max * 100) + '%">' +
          (l.stale ? '<span class="trl-seg trl-seg--stale" style="flex:' + l.stale + '"></span>' : '') +
          (l.ok ? '<span class="trl-seg trl-seg--ok" style="flex:' + l.ok + '"></span>' : '') +
        '</span>' +
        '<span class="trl-label">УГТ ' + l.trl + '</span></button>';
    }).join('');

    el('trlChart').querySelectorAll('.trl-col').forEach(function (col) {
      var lvl = Number(col.getAttribute('data-trl'));
      var l = levels[lvl - 1];
      col.addEventListener('mousemove', function (e) {
        showTip(e, '<b>УГТ ' + lvl + '</b><br>В развитии: ' + l.ok + '<br>Кандидаты на закрытие: ' + l.stale);
      });
      col.addEventListener('mouseleave', hideTip);
      col.addEventListener('click', function () {
        state.trlFilter = state.trlFilter === lvl ? null : lvl;
        renderTrl();
      });
    });

    el('trlHint').textContent = state.trlFilter
      ? 'Фильтр: УГТ ' + state.trlFilter + ' — нажмите на столбец ещё раз, чтобы сбросить'
      : 'Нажмите на столбец, чтобы отфильтровать таблицу';

    var rows = projects.filter(function (p) {
      if (state.trlFilter && Number(p.trl) !== state.trlFilter) return false;
      if (state.staleOnly && !isStale(p)) return false;
      return true;
    }).sort(function (a, b) { return yearsOnLevel(b) - yearsOnLevel(a); });

    var stages = stageTitles();
    el('projectsTable').innerHTML =
      '<thead><tr><th>Проект</th><th class="num">УГТ</th><th>На уровне</th><th>Стадия финансирования</th><th class="num">Бюджет</th></tr></thead><tbody>' +
      rows.map(function (p) {
        var y = yearsOnLevel(p);
        var stale = isStale(p);
        return '<tr class="' + (stale ? 'row-stale' : '') + '"><td><div class="cell-title">' + esc(p.title) + '</div><div class="cell-sub">' + esc(p.id) + ' · ' + esc(p.department) + '</div></td>' +
          '<td class="num"><b>' + esc(p.trl) + '</b></td>' +
          '<td>' + y.toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + ' ' + plural(Math.floor(y), 'год', 'года', 'лет') +
            '<div class="cell-sub">с ' + fmtDate(p.trl_since) + '</div>' +
            (stale ? '<span class="pill pill--bad">⚠ кандидат на закрытие</span>' : '') + '</td>' +
          '<td>' + esc(stages[p.funding_stage] || '—') + '</td>' +
          '<td class="num">' + money(p.budget) + '</td></tr>';
      }).join('') +
      (rows.length ? '' : '<tr><td colspan="5" class="empty">Нет проектов по выбранному фильтру</td></tr>') +
      '</tbody>';
  }

  function stageTitles() {
    var map = {};
    arr('funding_stages').forEach(function (s) { map[s.key] = s.title; });
    return map;
  }

  /* ---------- Цепочка финансирования ---------- */

  function renderFunding() {
    var stages = arr('funding_stages');
    var projects = arr('projects');
    var counts = stages.map(function (s) {
      var list = projects.filter(function (p) { return p.funding_stage === s.key; });
      return { stage: s, list: list, budget: list.reduce(function (a, p) { return a + (Number(p.budget) || 0); }, 0) };
    });
    /* «дошло до стадии» — проекты на этой стадии и дальше по цепочке */
    var reached = counts.map(function (c, i) {
      return counts.slice(i).reduce(function (a, x) { return a + x.list.length; }, 0);
    });

    el('fundingChain').innerHTML = counts.map(function (c, i) {
      var conv = i > 0 && reached[i - 1] ? Math.round(reached[i] / reached[i - 1] * 100) : null;
      return (i > 0 ? '<div class="funnel-arrow" aria-hidden="true"><span>' + conv + '%</span></div>' : '') +
        '<button type="button" class="funnel-step' + (state.fundingStage === c.stage.key ? ' is-active' : '') + '" data-stage="' + esc(c.stage.key) + '">' +
          '<span class="funnel-n">' + (i + 1) + '</span>' +
          '<span class="funnel-title">' + esc(c.stage.title) + '</span>' +
          '<span class="funnel-value">' + c.list.length + ' ' + plural(c.list.length, 'проект', 'проекта', 'проектов') + '</span>' +
          '<span class="funnel-money">' + money(c.budget) + '</span>' +
          '<span class="funnel-sub">' + esc(c.stage.subtitle || '') + '</span>' +
        '</button>';
    }).join('');

    el('fundingChain').querySelectorAll('.funnel-step').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var key = btn.getAttribute('data-stage');
        state.fundingStage = state.fundingStage === key ? null : key;
        renderFunding();
      });
    });

    var detail = el('fundingDetail');
    var sel = counts.find(function (c) { return c.stage.key === state.fundingStage; });
    if (!sel) {
      detail.innerHTML = '<p class="widget-sub">Процент над стрелкой — доля проектов, дошедших до следующей стадии. Нажмите на стадию, чтобы увидеть её проекты.</p>';
      return;
    }
    detail.innerHTML = '<h3>' + esc(sel.stage.title) + ': проекты</h3><ul class="chip-list">' +
      sel.list.map(function (p) {
        return '<li class="' + (isStale(p) ? 'is-stale' : '') + '">' + esc(p.title) + ' <span>УГТ ' + esc(p.trl) + ' · ' + money(p.budget) + '</span></li>';
      }).join('') + (sel.list.length ? '' : '<li>Проектов на этой стадии нет — узкое место цепочки</li>') + '</ul>';
  }

  /* ---------- Стартап-коллективы ---------- */

  function renderTeams() {
    var teams = arr('teams');
    var projects = {};
    arr('projects').forEach(function (p) { projects[p.id] = p; });
    var total = teams.length || 1;

    el('teamTiles').innerHTML = TEAM_FUNDING.map(function (f) {
      var list = teams.filter(function (t) { return t.funding === f.key; });
      var active = state.teamFilter === f.key;
      return '<button type="button" class="team-tile' + (active ? ' is-active' : '') + '" data-funding="' + f.key + '">' +
        '<span class="team-value">' + list.length + '</span>' +
        '<span class="team-title">' + f.title + '</span>' +
        '<span class="team-bar"><span style="width:' + (list.length / total * 100) + '%"></span></span>' +
        '</button>';
    }).join('');

    el('teamTiles').querySelectorAll('.team-tile').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var key = btn.getAttribute('data-funding');
        state.teamFilter = state.teamFilter === key ? null : key;
        renderTeams();
      });
    });

    var titles = {};
    TEAM_FUNDING.forEach(function (f) { titles[f.key] = f.title; });
    var list = teams.filter(function (t) { return !state.teamFilter || t.funding === state.teamFilter; });
    el('teamList').innerHTML = list.map(function (t) {
      var p = projects[t.project];
      return '<li><b>' + esc(t.name) + '</b><span>' + esc(t.members) + ' ' + plural(Number(t.members) || 0, 'человек', 'человека', 'человек') +
        ' · ' + esc(titles[t.funding] || t.funding) + (p ? ' · ' + esc(p.title) + ', УГТ ' + esc(p.trl) : '') + '</span></li>';
    }).join('') || '<li class="empty">Нет команд</li>';
  }

  /* ---------- Венчурный билдер ---------- */

  function renderVenture() {
    var rows = arr('venture_tracker').map(function (r) {
      return Object.assign({}, r, { _status: ventureStatus(r) });
    }).filter(function (r) {
      return state.ventureFilter === 'all' || r._status === state.ventureFilter;
    }).sort(function (a, b) { return new Date(a.due) - new Date(b.due); });

    var pillClass = { on_track: 'ok', risk: 'warn', overdue: 'bad', done: 'done' };
    el('ventureTable').innerHTML =
      '<thead><tr><th>Стартап</th><th>Обязательство</th><th>Срок</th><th>Выполнение</th><th>Статус</th></tr></thead><tbody>' +
      rows.map(function (r) {
        var pr = Math.max(0, Math.min(100, Number(r.progress) || 0));
        return '<tr><td><div class="cell-title">' + esc(r.startup) + '</div><div class="cell-sub">' + esc(r.owner || '') + '</div></td>' +
          '<td>' + esc(r.commitment) + '</td>' +
          '<td>' + fmtDate(r.due) + '</td>' +
          '<td><div class="progress-mini"><span class="pm-' + pillClass[r._status] + '" style="width:' + pr + '%"></span></div><div class="cell-sub">' + pr + '%</div></td>' +
          '<td><span class="pill pill--' + pillClass[r._status] + '">' + VENTURE_STATUS[r._status] + '</span></td></tr>';
      }).join('') +
      (rows.length ? '' : '<tr><td colspan="5" class="empty">Нет обязательств с таким статусом</td></tr>') +
      '</tbody>';
  }

  /* ---------- фильтры-чипы ---------- */

  function setChip(group, value) {
    var box = document.querySelector('.chips[data-filter="' + group + '"]');
    if (box) {
      box.querySelectorAll('.chip').forEach(function (c) {
        c.classList.toggle('is-on', c.getAttribute('data-value') === value);
      });
    }
    if (group === 'patents') { state.patentFilter = value; renderPatents(); }
    if (group === 'venture') { state.ventureFilter = value; renderVenture(); }
  }

  document.querySelectorAll('.chips[data-filter]').forEach(function (box) {
    box.addEventListener('click', function (e) {
      var chip = e.target.closest('.chip');
      if (chip) setChip(box.getAttribute('data-filter'), chip.getAttribute('data-value'));
    });
  });

  var staleBox = el('staleOnly');
  if (staleBox) {
    staleBox.addEventListener('change', function () {
      state.staleOnly = staleBox.checked;
      renderTrl();
    });
  }

  /* ---------- загрузка и свои данные ---------- */

  function renderAll() {
    var d = state.data || {};
    el('dashUniversity').textContent = (d.university || 'Университет') + ' · инновационные проекты, РИД, стартапы и МТК';
    el('dashSource').textContent = state.source === 'local'
      ? 'Показаны ваши данные (загружены в этом браузере)'
      : 'Показаны тестовые данные из data/dashboard.json — замените их данными своего вуза';
    el('dashReset').hidden = state.source !== 'local';
    renderKpi();
    renderPatents();
    renderTrl();
    renderFunding();
    renderTeams();
    renderVenture();
  }

  function loadLocal() {
    try {
      var raw = localStorage.getItem(LOCAL_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  function load() {
    var local = loadLocal();
    if (local) {
      state.data = local;
      state.source = 'local';
      renderAll();
      return;
    }
    fetch(DATA_URL, { headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        state.data = data;
        state.source = 'demo';
        renderAll();
      })
      .catch(function () {
        el('dashSource').textContent = 'Не удалось загрузить data/dashboard.json — запустите платформу через сервер (см. README).';
      });
  }

  var upload = el('dashUpload');
  if (upload) {
    upload.addEventListener('change', function () {
      var file = upload.files && upload.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        try {
          var data = JSON.parse(reader.result);
          if (!data || typeof data !== 'object' || !Array.isArray(data.projects)) {
            throw new Error('нет массива projects');
          }
          try { localStorage.setItem(LOCAL_KEY, JSON.stringify(data)); } catch (e) {}
          state.data = data;
          state.source = 'local';
          state.trlFilter = null;
          state.fundingStage = null;
          state.teamFilter = null;
          renderAll();
        } catch (err) {
          alert('Файл не похож на данные дашборда: ' + err.message + '. Возьмите за основу «Скачать шаблон данных».');
        }
        upload.value = '';
      };
      reader.readAsText(file, 'utf-8');
    });
  }

  var reset = el('dashReset');
  if (reset) {
    reset.addEventListener('click', function () {
      try { localStorage.removeItem(LOCAL_KEY); } catch (e) {}
      load();
    });
  }

  var pathLogin = el('pathLogin');
  if (pathLogin) {
    pathLogin.addEventListener('click', function (e) {
      e.preventDefault();
      var btn = el('btnLogin');
      if (btn && !btn.hidden && btn.offsetParent !== null) btn.click();
      else window.location.href = 'cabinet.html';
    });
  }

  load();
})();
