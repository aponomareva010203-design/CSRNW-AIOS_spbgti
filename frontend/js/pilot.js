/* ============================================================
   Статус пилотного проекта — интерактивная таблица прогресса.
   Режим редактирования включается автоматически для учётных
   записей со статусом «Администратор» (сессия в localStorage,
   ключ aios-auth-v1). Остальные посетители видят таблицу
   только для просмотра.
   Администратор может:
     · менять статусы артефактов кликом (0→1→2→3→0);
     · редактировать тематику и ФИО руководителя;
     · добавлять новые строки (№ присваивается автоматически).
   Данные хранятся в localStorage этого браузера.
   ============================================================ */

(function () {
  var body = document.getElementById('pilotBody');
  if (!body) return;

  /* исходные проектные группы */
  var DEFAULT_PROJECTS = [
    { id: 1, lead: 'Сладковский Дмитрий Андреевич',  topic: 'Катализ, переработка полимерных отходов, гидроконверсия гудрона, метан в полимеры' },
    { id: 2, lead: 'Аликин Михаил Борисович',        topic: 'Наукоёмкие композиционные полимеры: ЛКМ, эпоксидные композиции, PMI-пены, аддитивная печать' },
    { id: 3, lead: 'Дроздов Евгений Олегович',       topic: 'Молекулярное наслаивание, функциональные наноматериалы, квантовохимическое моделирование' },
    { id: 4, lead: 'Полякова Людмила Сергеевна',     topic: 'Электрохимия и электрохимическая энергетика (литий-ион)' },
    { id: 5, lead: 'Сторожек Георгий Олегович',      topic: 'Механофизика полимеров, компаунды' },
    { id: 6, lead: 'Григорьева Татьяна Алексеевна',  topic: 'Молекулярная фармакология, противоопухолевые препараты, PROTAC' },
    { id: 7, lead: 'Светлов Станислав Дмитриевич',   topic: 'Микрофлюидика и микрореакторные технологии: чипы, монодисперсные эмульсии, газожидкостные течения' },
    { id: 8, lead: 'Пушкарев Михаил Алексеевич',     topic: 'Микробиологический синтез, биокатализ; в проекте Шамцяна — белковая биомасса из метана' }
  ];

  var ARTEFACTS = ['rag', 'models', 'agents', 'polygon', 'social'];
  var ARTEFACT_NAMES = {
    rag: 'RAG и онтология',
    models: 'Модели',
    agents: 'Агенты',
    polygon: 'Виртуальный полигон',
    social: 'Социальная сеть агентов'
  };

  var STATUS_NAMES = ['не начато', 'начато', 'в работе', 'готово'];
  var STATUS_KEY = 'aios-pilot-status-v1';
  var DATA_KEY = 'aios-pilot-data-v1';
  var AUTH_KEY = 'aios-auth-v1';

  /* ---------- хранилище ---------- */

  function loadJSON(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) { return fallback; }
  }
  function saveJSON(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {}
  }

  var state = loadJSON(STATUS_KEY, {});
  var projects = loadJSON(DATA_KEY, null);
  if (!Array.isArray(projects) || !projects.length) {
    projects = DEFAULT_PROJECTS.map(function (p) {
      return { id: p.id, lead: p.lead, topic: p.topic };
    });
  }

  /* ---------- роль пользователя ---------- */

  var session = loadJSON(AUTH_KEY, null);
  var isAdmin = !!(session && session.role === 'Администратор');

  /* ---------- helpers ---------- */

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function statusOf(pid, art) {
    var row = state[pid];
    if (!row) return 0;
    var v = row[art];
    return (v >= 0 && v <= 3) ? v : 0;
  }

  function projectById(pid) {
    for (var i = 0; i < projects.length; i++) {
      if (String(projects[i].id) === String(pid)) return projects[i];
    }
    return null;
  }

  /* ---------- подсказка и кнопка добавления ---------- */

  var hint = document.getElementById('adminHint');
  var addBtn = document.getElementById('btnAddRow');

  function renderHint() {
    if (!hint) return;
    if (isAdmin) {
      hint.textContent = 'режим администратора: клик по статусу меняет его, тематика и ФИО редактируются';
    } else {
      hint.textContent = 'только просмотр · статусы и поля обновляет администратор';
    }
    if (addBtn) addBtn.hidden = !isAdmin;
  }

  /* ---------- отрисовка таблицы ---------- */

  function render() {
    renderHint();
    var html = '';
    projects.forEach(function (p, idx) {
      html += '<tr>' +
        '<td class="pt-num">' + (idx + 1) + '</td>' +
        '<td class="pt-topic' + (isAdmin ? ' pt-edit' : '') + '"' +
          (isAdmin ? ' contenteditable="true" spellcheck="false" data-field="topic" data-id="' + p.id + '" title="Нажмите, чтобы редактировать тематику"' : '') +
          '>' + esc(p.topic) + '</td>' +
        '<td class="pt-lead' + (isAdmin ? ' pt-edit' : '') + '"' +
          (isAdmin ? ' contenteditable="true" spellcheck="false" data-field="lead" data-id="' + p.id + '" title="Нажмите, чтобы редактировать ФИО"' : '') +
          '>' + esc(p.lead) + '</td>';
      ARTEFACTS.forEach(function (a) {
        var st = statusOf(p.id, a);
        html += '<td class="pt-cell" data-p="' + p.id + '" data-a="' + a + '">' +
          '<span class="pt-pill pt-pill--' + st + (isAdmin ? ' pt-pill--editable' : '') + '"' +
          (isAdmin ? ' title="Нажмите, чтобы сменить статус: ' + esc(ARTEFACT_NAMES[a]) + ' — ' + esc(p.lead) + '"' : '') + '>' +
          '<span class="pt-dot"></span>' + STATUS_NAMES[st] + '</span>' +
          '</td>';
      });
      html += '</tr>';
    });
    body.innerHTML = html;
  }

  /* ---------- клик по статусу: цикл 0→1→2→3→0 ---------- */

  body.addEventListener('click', function (e) {
    if (!isAdmin) return;
    var cell = e.target.closest('.pt-cell');
    if (!cell) return;
    var pid = cell.getAttribute('data-p');
    var art = cell.getAttribute('data-a');
    var cur = statusOf(pid, art);
    if (!state[pid]) state[pid] = {};
    state[pid][art] = (cur + 1) % 4;
    saveJSON(STATUS_KEY, state);
    render();
  });

  /* ---------- редактирование текстовых полей ---------- */

  body.addEventListener('keydown', function (e) {
    if (!isAdmin) return;
    var cell = e.target.closest('.pt-edit');
    if (!cell) return;
    if (e.key === 'Enter') {
      e.preventDefault();
      cell.blur();
    } else if (e.key === 'Escape') {
      var p = projectById(cell.getAttribute('data-id'));
      if (p) cell.textContent = p[cell.getAttribute('data-field')];
      cell.blur();
    }
  });

  body.addEventListener('blur', function (e) {
    if (!isAdmin) return;
    var cell = e.target.closest ? e.target.closest('.pt-edit') : null;
    if (!cell) return;
    var p = projectById(cell.getAttribute('data-id'));
    if (!p) return;
    var field = cell.getAttribute('data-field');
    var value = cell.textContent.replace(/\s+/g, ' ').trim();
    if (!value) value = p[field];
    if (value !== p[field]) {
      p[field] = value;
      saveJSON(DATA_KEY, projects);
    }
    cell.textContent = p[field];
  }, true);

  /* ---------- добавление строки ---------- */

  if (addBtn) {
    addBtn.addEventListener('click', function () {
      var maxId = 0;
      projects.forEach(function (p) { if (p.id > maxId) maxId = p.id; });
      var fresh = {
        id: maxId + 1,
        lead: 'ФИО руководителя',
        topic: 'Тематика нового проекта'
      };
      projects.push(fresh);
      saveJSON(DATA_KEY, projects);
      render();
      var cell = body.querySelector('.pt-topic[data-id="' + fresh.id + '"]');
      if (cell) {
        cell.focus();
        var range = document.createRange();
        range.selectNodeContents(cell);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }
    });
  }

  render();
})();
