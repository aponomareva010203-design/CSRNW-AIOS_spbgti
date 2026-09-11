/* ============================================================
   Статус пилотного проекта — таблица прогресса по карточкам
   пользователей. Строки формируются на сервере из активных
   карточек «Моих проектов» (GET /api/pilot/projects, публичный
   эндпоинт): тематика и ФИО руководителя берутся из карточки,
   статусы и гипотезы артефактов — из её блока artifacts.

   Статусы артефакта (поле status в карточке проекта):
     0 — не начато; 1 — гипотеза сформулирована;
     2 — в процессе создания; 3 — завершено.

   В ячейке — только цветная точка (цвет = статус, расшифровка в легенде
   над таблицей). Наведение на точку показывает формулировку гипотезы
   (тултип), клик открывает пояснение со статусом и гипотезой.
   Режим администратора (роль «Администратор», сессия в
   localStorage, ключ aios-auth-v1): в том же пояснении можно
   сменить статус артефакта — PATCH /api/pilot/projects/{id},
   изменение сразу видно всем посетителям.
   ============================================================ */

(function () {
  var body = document.getElementById('pilotBody');
  if (!body) return;

  var ARTEFACTS = ['rag', 'models', 'agents', 'polygon', 'social'];
  var ARTEFACT_NAMES = {
    rag: 'RAG и онтология',
    models: 'Модели',
    agents: 'Агенты',
    polygon: 'Виртуальный полигон',
    social: 'Социальная сеть агентов'
  };
  /* порядок совпадает со статусами 0–3 в карточке проекта */
  var STATUS_NAMES = ['не начато', 'гипотеза сформулирована', 'в процессе создания', 'завершено'];
  var AUTH_KEY = 'aios-auth-v1';

  var rows = [];          // строки с сервера (последняя загрузка)
  var session = loadJSON(AUTH_KEY, null);
  var isAdmin = !!(session && session.token && session.role === 'Администратор');

  /* ---------- helpers ---------- */

  function loadJSON(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) { return fallback; }
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function rowById(pid) {
    for (var i = 0; i < rows.length; i++) {
      if (String(rows[i].id) === String(pid)) return rows[i];
    }
    return null;
  }

  function statusOf(row, art) {
    if (!row || !row.artifacts || !row.artifacts[art]) return 0;
    var v = Number(row.artifacts[art].status);
    return (v >= 0 && v <= 3) ? v : 0;
  }

  function hypothesisOf(row, art) {
    if (!row || !row.artifacts || !row.artifacts[art]) return '';
    return String(row.artifacts[art].hypothesis || '');
  }

  /* ---------- загрузка строк ---------- */

  function renderLoading() {
    body.innerHTML = '<tr><td colspan="8" class="pt-msg">Загрузка…</td></tr>';
  }

  function renderError(err) {
    body.innerHTML = '<tr><td colspan="8" class="pt-msg pt-msg--error">' +
      'Не удалось загрузить данные пилота' +
      (err && err.message ? ': ' + esc(err.message) : '') +
      ' — <button type="button" class="pt-retry">Повторить</button></td></tr>';
    var retry = body.querySelector('.pt-retry');
    if (retry) retry.addEventListener('click', load);
  }

  function load() {
    renderLoading();
    closePop();
    if (typeof window.AIOS_API === 'undefined' || !window.AIOS_API.pilotList) {
      renderError(new Error('API-клиент недоступен'));
      return;
    }
    window.AIOS_API.pilotList().then(function (data) {
      rows = (data && data.items) || [];
      render();
    }).catch(renderError);
  }

  /* ---------- отрисовка таблицы ---------- */

  function renderHint() {
    var hint = document.getElementById('adminHint');
    if (!hint) return;
    hint.textContent = isAdmin
      ? 'Вы вошли как администратор: клик по чипу открывает пояснение и смену статуса.'
      : 'Клик по чипу статуса (начиная с «гипотеза сформулирована») открывает гипотезу артефакта.';
  }

  function render() {
    renderHint();
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="8" class="pt-msg">Проектов пока нет — карточки создаются ' +
        'навыком «Онбординг проекта» в рабочем чате и появляются здесь после подтверждения.</td></tr>';
      return;
    }
    var html = '';
    rows.forEach(function (p, idx) {
      html += '<tr>' +
        '<td class="pt-num">' + (idx + 1) + '</td>' +
        '<td class="pt-topic">' + esc(p.title || 'Без названия') + '</td>' +
        '<td class="pt-lead">' + esc(p.lead || '—') + '</td>';
      ARTEFACTS.forEach(function (a) {
        var st = statusOf(p, a);
        var interactive = st > 0 || isAdmin;
        /* в ячейке — только цветная точка; формулировка гипотезы — тултипом */
        var hyp = hypothesisOf(p, a);
        var tip = (st > 0 && hyp)
          ? ' title="' + esc(hyp.length > 300 ? hyp.slice(0, 300) + '…' : hyp) + '"'
          : '';
        html += '<td class="pt-cell" data-p="' + esc(p.id) + '" data-a="' + a + '">' +
          '<span class="pt-pill pt-pill--' + st + (interactive ? ' pt-pill--clickable' : '') + '"' + tip + '>' +
          '<span class="pt-dot"></span></span>' +
          '</td>';
      });
      html += '</tr>';
    });
    body.innerHTML = html;
  }

  /* ---------- пояснение по клику на чип статуса ---------- */

  var pop = null;   // открытый поповер пояснения

  function closePop() {
    if (pop) {
      if (pop.keyHandler) document.removeEventListener('keydown', pop.keyHandler, true);
      if (pop.outsideHandler) document.removeEventListener('mousedown', pop.outsideHandler, true);
      pop.remove();
      pop = null;
    }
  }

  function popHtml(row, art, st) {
    var hyp = hypothesisOf(row, art);
    var html = '<button type="button" class="pt-pop-close" title="Закрыть">×</button>' +
      '<div class="pt-pop-head">' + esc(ARTEFACT_NAMES[art]) + '</div>' +
      '<div class="pt-pop-project">' + esc(row.title || 'Без названия') + '</div>' +
      '<div class="pt-pop-status">Статус: ' + STATUS_NAMES[st] + '</div>';
    if (hyp && st > 0) {
      html += '<div class="pt-pop-hyp">' + esc(hyp) + '</div>';
    } else {
      html += '<div class="pt-pop-hyp pt-pop-hyp--empty">Гипотеза артефакта ещё не сформулирована.</div>';
    }
    if (isAdmin) {
      html += '<div class="pt-pop-admin">' +
        '<div class="pt-pop-admin-title">Сменить статус</div>' +
        '<div class="pt-pop-statuses">';
      for (var s = 0; s < STATUS_NAMES.length; s++) {
        html += '<button type="button" class="pt-status-btn' + (s === st ? ' pt-status-btn--current' : '') +
          '" data-s="' + s + '"' + (s === st ? ' disabled' : '') + '>' + STATUS_NAMES[s] + '</button>';
      }
      html += '</div><div class="pt-pop-error" hidden></div></div>';
    }
    return html;
  }

  function positionPop(anchor) {
    var rect = anchor.getBoundingClientRect();
    var width = pop.offsetWidth || 340;
    var height = pop.offsetHeight || 200;
    var margin = 8;
    var left = Math.min(Math.max(margin, rect.left), window.innerWidth - width - margin);
    var top = rect.bottom + 8;
    if (top + height > window.innerHeight - margin) {
      top = Math.max(margin, rect.top - height - 8);
    }
    pop.style.left = Math.round(left) + 'px';
    pop.style.top = Math.round(top) + 'px';
  }

  function openPop(row, art, anchor) {
    closePop();
    var st = statusOf(row, art);
    pop = document.createElement('div');
    pop.className = 'pt-pop';
    pop.innerHTML = popHtml(row, art, st);
    pop.keyHandler = function (e) {
      if (e.key === 'Escape') { e.stopPropagation(); closePop(); }
    };
    pop.outsideHandler = function (e) {
      if (pop && !pop.contains(e.target) && !anchor.contains(e.target)) closePop();
    };
    document.addEventListener('keydown', pop.keyHandler, true);
    document.addEventListener('mousedown', pop.outsideHandler, true);
    document.body.appendChild(pop);
    positionPop(anchor);
    window.addEventListener('resize', closePop);
    window.addEventListener('scroll', closePop, true);

    var closeBtn = pop.querySelector('.pt-pop-close');
    if (closeBtn) closeBtn.addEventListener('click', closePop);

    /* смена статуса — только администратор, сохранение на сервере */
    if (isAdmin) {
      var errBox = pop.querySelector('.pt-pop-error');
      pop.querySelectorAll('.pt-status-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
          if (btn.disabled) return;
          var next = Number(btn.getAttribute('data-s'));
          btn.disabled = true;
          errBox.hidden = true;
          window.AIOS_API.pilotSetStatus(session.token, row.id, art, next).then(function (updated) {
            /* сервер вернул актуальную строку — обновляем локальные данные */
            for (var i = 0; i < rows.length; i++) {
              if (String(rows[i].id) === String(row.id)) { rows[i] = updated; break; }
            }
            render();
            var fresh = rowById(row.id);
            var cell = body.querySelector('.pt-cell[data-p="' + row.id + '"][data-a="' + art + '"] .pt-pill');
            openPop(fresh || updated, art, cell || anchor);
          }).catch(function (err) {
            btn.disabled = false;
            errBox.textContent = 'Не удалось сохранить статус: ' +
              (err && err.message ? err.message : 'ошибка сети');
            errBox.hidden = false;
          });
        });
      });
    }
  }

  body.addEventListener('click', function (e) {
    var pill = e.target.closest('.pt-pill');
    if (!pill) return;
    var cell = pill.closest('.pt-cell');
    var row = rowById(cell.getAttribute('data-p'));
    if (!row) return;
    var art = cell.getAttribute('data-a');
    var st = statusOf(row, art);
    /* «не начато» показывать нечего — гипотезы ещё нет; чип открывает
       пояснение только администратору, чтобы сменить статус */
    if (st === 0 && !isAdmin) return;
    openPop(row, art, pill);
  });

  /* ---------- старт ---------- */

  window.addEventListener('aios-auth-change', function () {
    session = loadJSON(AUTH_KEY, null);
    isAdmin = !!(session && session.token && session.role === 'Администратор');
    closePop();
    render();
  });

  load();
})();
