/* ============================================================
   Страница «Автоматизации».

   Пользователи собирают автоматизации из блоков (триггер + линейная
   цепочка шагов); бэкенд переводит их в воркфлоу self-hosted n8n
   и исполняет. Отдельного входа в n8n нет: запуск кнопкой, включение,
   история выполнений и правка — всё здесь (GET|POST|PUT|DELETE
   /api/automations*). Гость видит предложение войти; админу доступна
   свернутая секция «Все автоматизации платформы» (?all=1).
   ============================================================ */

(function () {
  var API = window.AIOS_API;
  var AUTH_KEY = 'aios-auth-v1';

  var guest = document.getElementById('autoGuest');
  var guestLogin = document.getElementById('autoGuestLogin');
  var wrap = document.getElementById('autoWrap');
  var listEl = document.getElementById('autoList');
  var listAllEl = document.getElementById('autoListAll');
  var msg = document.getElementById('autoMsg');
  var spoilerAll = document.getElementById('spoilerAll');
  var runBox = document.getElementById('autoRunBox');
  var runBody = document.getElementById('autoRunBody');
  var runTitle = document.getElementById('autoRunTitle');
  if (!guest || !wrap || !listEl) return;

  var builderModal = document.getElementById('builderModal');
  var builderForm = document.getElementById('builderForm');
  var builderStepsEl = document.getElementById('builderSteps');
  var builderMsg = document.getElementById('builderMsg');
  var builderInterval = document.getElementById('builderInterval');
  var execModal = document.getElementById('execModal');
  var execRows = document.getElementById('execRows');
  var execSub = document.getElementById('execSub');

  /* состояние конструктора: null — список, объект — правка/создание */
  var editing = null; // {id: ''|auto-…, trigger: {…}, steps: […]}
  var saving = false;

  var TRIGGER_LABELS = {
    manual: 'кнопка в AIOS',
    schedule: 'расписание',
    webhook: 'внешний вызов'
  };
  var STEP_LABELS = {
    agent: 'Агент AIOS',
    http: 'HTTP-запрос',
    email: 'Письмо'
  };

  function getSession() {
    try {
      var raw = localStorage.getItem(AUTH_KEY);
      var s = raw ? JSON.parse(raw) : null;
      return s && s.token ? s : null;
    } catch (e) {
      return null;
    }
  }

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function showMsg(text) {
    if (!msg) return;
    msg.textContent = text || '';
    msg.hidden = !text;
  }

  function fmtTs(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear() +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /* ---------- список ---------- */

  function triggerLabel(a) {
    var t = (a.trigger || {}).type;
    if (t === 'schedule') {
      return 'каждые ' + ((a.trigger || {}).interval_minutes || '?') + ' мин';
    }
    return TRIGGER_LABELS[t] || t || '';
  }

  function stepsSummary(a) {
    return (a.steps || []).map(function (s) {
      return STEP_LABELS[s.type] || s.type;
    }).join(' → ');
  }

  function card(a, allView) {
    var el = document.createElement('div');
    el.className = 'auto-card';
    var status = a.active
      ? '<span class="tag-chip tag-chip--on">включена</span>'
      : '<span class="tag-chip">выключена</span>';
    var hook = '';
    if ((a.trigger || {}).type === 'webhook' && a.webhook_path) {
      hook = '<code class="auto-hook">…/webhook/' + esc(a.webhook_path) + '</code>';
    }
    el.innerHTML =
      '<div class="auto-card-head"><strong>' + esc(a.name) + '</strong>' + status + '</div>' +
      (a.description ? '<p class="auto-card-desc">' + esc(a.description) + '</p>' : '') +
      '<div class="auto-card-meta">' +
        '<span class="tag-chip">' + esc(triggerLabel(a)) + '</span>' +
        '<span class="auto-steps-line">' + esc(stepsSummary(a)) + '</span>' +
        hook +
      '</div>' +
      '<div class="auto-card-actions"></div>';
    var actions = el.querySelector('.auto-card-actions');

    if (!allView) {
      var run = mkBtn('Запустить', 'btn-primary', function () { runAutomation(a); });
      var hist = mkBtn('История', 'adm-btn', function () { openExecutions(a); });
      var edit = mkBtn('Править', 'adm-btn', function () { openBuilder(a); });
      var toggle = mkBtn(a.active ? 'Выключить' : 'Включить', 'adm-btn', function () {
        toggleAutomation(a);
      });
      var del = mkBtn('Удалить', 'adm-btn adm-btn--danger', function () { removeAutomation(a); });
      actions.append(run, hist, edit, toggle, del);
    } else {
      var editAll = mkBtn('Править', 'adm-btn', function () { openBuilder(a); });
      actions.append(editAll);
    }
    return el;
  }

  function mkBtn(text, cls, handler) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = cls;
    b.textContent = text;
    b.addEventListener('click', handler);
    return b;
  }

  function renderList(items, target, allView) {
    target.innerHTML = '';
    if (!items.length) {
      var empty = document.createElement('div');
      empty.className = 'models-empty';
      empty.textContent = allView
        ? 'Автоматизаций пока нет ни у кого.'
        : 'У вас пока нет автоматизаций — создайте первую кнопкой выше.';
      target.appendChild(empty);
      return;
    }
    items.forEach(function (a) { target.appendChild(card(a, allView)); });
  }

  function load() {
    /* фича скрыта (js/api.js → AIOS_FEATURES.automations): прямой заход
       по URL показывает заглушку вместо раздела */
    if (!(window.AIOS_FEATURES && window.AIOS_FEATURES.automations)) {
      guest.hidden = false;
      wrap.hidden = true;
      var guestMsg = document.getElementById('autoGuestMsg');
      if (guestMsg) guestMsg.textContent = 'Раздел временно скрыт: конструктор автоматизаций переделывается (см. BACKLOG.md).';
      if (guestLogin) guestLogin.hidden = true;
      return;
    }
    if (guestLogin) guestLogin.hidden = false;
    var s = getSession();
    guest.hidden = !!s;
    wrap.hidden = !s;
    if (!s) return;
    if (spoilerAll) spoilerAll.hidden = s.role !== 'Администратор';
    showMsg('');
    listEl.innerHTML = '';
    for (var i = 0; i < 3; i++) {
      var sk = document.createElement('div');
      sk.className = 'auto-card auto-card--skeleton';
      sk.setAttribute('aria-hidden', 'true');
      listEl.appendChild(sk);
    }
    API.automationsList(s.token)
      .then(function (res) { renderList((res && res.items) || [], listEl, false); })
      .catch(function (err) {
        listEl.innerHTML = '';
        showMsg(err && err.message ? err.message : 'Не удалось загрузить автоматизации');
      });
    if (s.role === 'Администратор') {
      API.automationsList(s.token, true)
        .then(function (res) { renderList((res && res.items) || [], listAllEl, true); })
        .catch(function () { /* личный список уже показал ошибку */ });
    }
  }

  /* ---------- действия ---------- */

  function runAutomation(a) {
    var s = getSession();
    if (!s) return;
    showMsg('');
    runTitle.textContent = 'Запуск: ' + a.name;
    runBody.textContent = 'Выполняется…';
    runBox.hidden = false;
    API.automationsRun(s.token, a.id)
      .then(function (res) {
        var text;
        try {
          text = typeof res.result === 'string' ? res.result : JSON.stringify(res.result, null, 2);
        } catch (e) { text = String(res.result); }
        runBody.textContent = text || '(пустой результат)';
        load(); /* статус/время запуска могли измениться */
      })
      .catch(function (err) {
        runBox.hidden = true;
        showMsg(err && err.message ? err.message : 'Запуск не удался');
      });
  }

  function toggleAutomation(a) {
    var s = getSession();
    if (!s) return;
    showMsg('');
    API.automationsPublish(s.token, a.id, !a.active)
      .then(function () { load(); })
      .catch(function (err) { showMsg(err && err.message ? err.message : 'Не удалось переключить'); });
  }

  function removeAutomation(a) {
    var s = getSession();
    if (!s) return;
    if (!window.confirm('Удалить автоматизацию «' + a.name + '»? Воркфлоу в n8n будет удалён.')) return;
    showMsg('');
    API.automationsDelete(s.token, a.id)
      .then(function () { load(); })
      .catch(function (err) { showMsg(err && err.message ? err.message : 'Не удалось удалить'); });
  }

  /* ---------- история запусков ---------- */

  function openExecutions(a) {
    var s = getSession();
    if (!s) return;
    execSub.textContent = a.name;
    execRows.innerHTML = '<tr><td colspan="5">Загрузка…</td></tr>';
    openModal(execModal);
    API.automationsExecutions(s.token, a.id)
      .then(function (res) {
        var items = (res && res.items) || [];
        execRows.innerHTML = '';
        if (!items.length) {
          execRows.innerHTML = '<tr><td colspan="5">Запусков ещё не было.</td></tr>';
          return;
        }
        items.forEach(function (e) {
          var tr = document.createElement('tr');
          var statusCls = e.status === 'success' ? 'tag-chip tag-chip--on' : 'tag-chip';
          tr.innerHTML =
            '<td><span class="' + statusCls + '">' + esc(e.status || '') + '</span></td>' +
            '<td>' + esc(fmtTs(e.startedAt)) + '</td>' +
            '<td>' + esc(fmtTs(e.stoppedAt)) + '</td>' +
            '<td>' + esc(e.mode || '') + '</td>' +
            '<td></td>';
          if (e.status === 'error' || e.status === 'crashed') {
            var retry = mkBtn('Повторить', 'adm-btn', function () {
              retry.disabled = true;
              API.automationsRetryExecution(s.token, a.id, e.id)
                .then(function () { openExecutions(a); })
                .catch(function (err) {
                  retry.disabled = false;
                  window.alert(err && err.message ? err.message : 'Не удалось повторить запуск');
                });
            });
            tr.lastElementChild.appendChild(retry);
          }
          execRows.appendChild(tr);
        });
      })
      .catch(function (err) {
        execRows.innerHTML = '<tr><td colspan="5">' +
          esc(err && err.message ? err.message : 'Не удалось загрузить историю') + '</td></tr>';
      });
  }

  /* ---------- конструктор ---------- */

  function openBuilder(a) {
    editing = a
      ? {
          id: a.id,
          trigger: JSON.parse(JSON.stringify(a.trigger || { type: 'manual' })),
          steps: JSON.parse(JSON.stringify(a.steps || []))
        }
      : { id: '', trigger: { type: 'manual' }, steps: [] };
    builderForm.elements.name.value = a ? (a.name || '') : '';
    builderForm.elements.description.value = a ? (a.description || '') : '';
    document.getElementById('builderTitle').textContent = a
      ? 'Правка автоматизации'
      : 'Новая автоматизация';
    hideBuilderMsg();
    renderTriggerChoice();
    renderSteps();
    openModal(builderModal);
  }

  function renderTriggerChoice() {
    var kind = editing.trigger.type;
    var buttons = document.querySelectorAll('#builderTriggers .auto-trigger');
    buttons.forEach(function (b) {
      b.classList.toggle('auto-trigger--active', b.getAttribute('data-trigger') === kind);
    });
    if (builderInterval) builderInterval.hidden = kind !== 'schedule';
    if (kind === 'schedule') {
      builderForm.elements.interval_minutes.value = editing.trigger.interval_minutes || 30;
    }
  }

  function renderSteps() {
    builderStepsEl.innerHTML = '';
    editing.steps.forEach(function (step, idx) {
      builderStepsEl.appendChild(stepCard(step, idx));
    });
    if (!editing.steps.length) {
      var hint = document.createElement('p');
      hint.className = 'auto-hint';
      hint.textContent = 'Шагов пока нет — добавьте хотя бы один.';
      builderStepsEl.appendChild(hint);
    }
  }

  function stepCard(step, idx) {
    var el = document.createElement('div');
    el.className = 'auto-step';
    el.setAttribute('data-idx', String(idx));

    var head = document.createElement('div');
    head.className = 'auto-step-head';
    var title = document.createElement('strong');
    title.textContent = STEP_LABELS[step.type] || step.type;
    head.appendChild(title);

    var controls = document.createElement('span');
    controls.className = 'auto-step-ctrl';
    [['↑', -1], ['↓', 1]].forEach(function (pair) {
      var b = mkBtn(pair[0], 'auto-step-btn', function () {
        moveStep(idx, pair[1]);
      });
      controls.appendChild(b);
    });
    var del = mkBtn('✕', 'auto-step-btn auto-step-btn--del', function () {
      editing.steps.splice(idx, 1);
      renderSteps();
    });
    controls.appendChild(del);
    head.appendChild(controls);
    el.appendChild(head);

    var body = document.createElement('div');
    body.className = 'auto-step-body';
    if (step.type === 'agent') {
      body.appendChild(field('Промпт для агента', textarea(step, 'prompt',
        'Например: сделай краткую сводку по {{prev}}'), true));
    } else if (step.type === 'http') {
      var method = document.createElement('label');
      method.className = 'org-field';
      method.innerHTML = '<span class="org-field-title">Метод</span>';
      var sel = document.createElement('select');
      ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].forEach(function (m) {
        var o = document.createElement('option');
        o.value = m; o.textContent = m;
        if ((step.method || 'GET') === m) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener('change', function () { step.method = sel.value; });
      method.appendChild(sel);
      body.appendChild(method);

      body.appendChild(field('URL', input(step, 'url', 'https://…')));
      body.appendChild(headersField(step));
      body.appendChild(field('Тело (JSON)', textarea(step, 'body', '{"key": "value"}')));
    } else if (step.type === 'email') {
      body.appendChild(field('Получатели (через запятую)', input(step, 'to', 'user@example.com')));
      body.appendChild(field('Тема', input(step, 'subject', 'Сводка')));
      body.appendChild(field('Текст письма', textarea(step, 'text', 'Здравствуйте! {{prev}}'), true));
    }
    el.appendChild(body);
    return el;
  }

  function field(titleText, control, grow) {
    var label = document.createElement('label');
    label.className = 'org-field' + (grow ? ' org-field--grow' : '');
    var title = document.createElement('span');
    title.className = 'org-field-title';
    title.textContent = titleText;
    label.append(title, control);
    return label;
  }

  function input(step, key, placeholder) {
    var el = document.createElement('input');
    el.type = 'text';
    el.value = step[key] || '';
    el.placeholder = placeholder || '';
    el.addEventListener('input', function () { step[key] = el.value; });
    return el;
  }

  function textarea(step, key, placeholder) {
    var el = document.createElement('textarea');
    el.rows = 3;
    el.value = step[key] || '';
    el.placeholder = placeholder || '';
    el.addEventListener('input', function () { step[key] = el.value; });
    return el;
  }

  function headersField(step) {
    var wrapEl = document.createElement('div');
    wrapEl.className = 'org-field';
    var title = document.createElement('span');
    title.className = 'org-field-title';
    title.textContent = 'Заголовки';
    wrapEl.appendChild(title);

    var rows = document.createElement('div');
    rows.className = 'auto-headers';
    if (!step.headers) step.headers = [];
    step.headers.forEach(function (h) { rows.appendChild(headerRow(step, h)); });
    wrapEl.appendChild(rows);

    var add = mkBtn('+ заголовок', 'adm-btn', function () {
      var h = { name: '', value: '' };
      step.headers.push(h);
      rows.appendChild(headerRow(step, h));
    });
    add.className = 'adm-btn auto-headers-add';
    wrapEl.appendChild(add);
    return wrapEl;
  }

  function headerRow(step, h) {
    var row = document.createElement('div');
    row.className = 'auto-header-row';
    var name = document.createElement('input');
    name.type = 'text';
    name.placeholder = 'Имя';
    name.value = h.name || '';
    name.addEventListener('input', function () { h.name = name.value; });
    var value = document.createElement('input');
    value.type = 'text';
    value.placeholder = 'Значение';
    value.value = h.value || '';
    value.addEventListener('input', function () { h.value = value.value; });
    var del = mkBtn('✕', 'auto-step-btn auto-step-btn--del', function () {
      var i = step.headers.indexOf(h);
      if (i >= 0) step.headers.splice(i, 1);
      row.remove();
    });
    row.append(name, value, del);
    return row;
  }

  function moveStep(idx, delta) {
    var to = idx + delta;
    if (to < 0 || to >= editing.steps.length) return;
    var moved = editing.steps.splice(idx, 1)[0];
    editing.steps.splice(to, 0, moved);
    renderSteps();
  }

  function addStep(kind) {
    if (editing.steps.length >= 10) {
      showBuilderMsg('Слишком много шагов (до 10)');
      return;
    }
    var defaults = {
      agent: { type: 'agent', prompt: '' },
      http: { type: 'http', method: 'GET', url: '' },
      email: { type: 'email', to: '', subject: '', text: '' }
    };
    editing.steps.push(defaults[kind]);
    renderSteps();
  }

  function showBuilderMsg(text) {
    if (!builderMsg) return;
    builderMsg.textContent = text || '';
    builderMsg.hidden = !text;
  }

  function hideBuilderMsg() { showBuilderMsg(''); }

  function saveBuilder(e) {
    e.preventDefault();
    if (saving) return;
    var s = getSession();
    if (!s) return;
    hideBuilderMsg();

    var payload = {
      name: builderForm.elements.name.value.trim(),
      description: builderForm.elements.description.value.trim(),
      trigger: { type: editing.trigger.type },
      steps: editing.steps.map(function (step) {
        var copy = {};
        Object.keys(step).forEach(function (k) { copy[k] = step[k]; });
        if (copy.headers) {
          copy.headers = copy.headers.filter(function (h) { return (h.name || '').trim(); });
          if (!copy.headers.length) delete copy.headers;
        }
        return copy;
      })
    };
    if (payload.trigger.type === 'schedule') {
      payload.trigger.interval_minutes = parseInt(builderForm.elements.interval_minutes.value, 10) || 30;
    }

    saving = true;
    var done = function () { saving = false; };
    var call = editing.id
      ? API.automationsUpdate(s.token, editing.id, payload)
      : API.automationsCreate(s.token, payload);
    call
      .then(function () {
        done();
        closeModal(builderModal);
        load();
      })
      .catch(function (err) {
        done();
        showBuilderMsg(err && err.message ? err.message : 'Не удалось сохранить автоматизацию');
      });
  }

  /* ---------- модальные окна ---------- */

  function openModal(overlay) {
    overlay.hidden = false;
    document.body.classList.add('modal-open');
  }

  function closeModal(overlay) {
    overlay.hidden = true;
    document.body.classList.remove('modal-open');
  }

  function bindModal(overlay, closeBtn) {
    if (!overlay) return;
    closeBtn.addEventListener('click', function () { closeModal(overlay); });
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) closeModal(overlay);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !overlay.hidden) closeModal(overlay);
    });
  }

  bindModal(builderModal, document.getElementById('builderClose'));
  bindModal(execModal, document.getElementById('execClose'));

  /* ---------- события ---------- */

  if (guestLogin) {
    guestLogin.addEventListener('click', function () {
      var btnLogin = document.getElementById('btnLogin');
      if (btnLogin) btnLogin.click(); /* модалку входа показывает auth.js */
    });
  }

  document.getElementById('btnNewAutomation').addEventListener('click', function () { openBuilder(null); });
  document.getElementById('btnReloadAutomations').addEventListener('click', load);
  builderForm.addEventListener('submit', saveBuilder);
  document.getElementById('builderCancel').addEventListener('click', function () {
    closeModal(builderModal);
  });

  document.querySelectorAll('#builderTriggers .auto-trigger').forEach(function (b) {
    b.addEventListener('click', function () {
      editing.trigger = { type: b.getAttribute('data-trigger') };
      renderTriggerChoice();
    });
  });

  document.querySelectorAll('.auto-addstep [data-step]').forEach(function (b) {
    b.addEventListener('click', function () { addStep(b.getAttribute('data-step')); });
  });

  var runClose = document.getElementById('autoRunClose');
  if (runClose) runClose.addEventListener('click', function () { runBox.hidden = true; });

  /* спойлер «Все автоматизации» — сворачивается как и остальные */
  var spoilerToggle = spoilerAll ? spoilerAll.querySelector('.spoiler-toggle') : null;
  if (spoilerToggle) {
    spoilerToggle.addEventListener('click', function () {
      var body = spoilerAll.querySelector('.spoiler-body');
      var open = spoilerAll.classList.toggle('spoiler--open');
      body.hidden = !open;
      spoilerToggle.setAttribute('aria-expanded', String(open));
    });
  }

  load();
  window.addEventListener('aios-auth-change', load);
})();
