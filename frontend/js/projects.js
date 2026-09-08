/* ============================================================
   «Мои проекты»: карточки проектов текущего пользователя.

   Данные — GET /api/projects (только собственные карточки, идентификатор
   берётся из Bearer-токена). Карточки создаёт навык «Онбординг проекта»
   в рабочем чате; здесь их читают, правит руководитель проекта
   (PATCH /api/projects/{id}) и удаляет администратор (DELETE — роль
   «Администратор»). Гость видит предложение войти.
   ============================================================ */
(function () {
  var API = window.AIOS_API;
  var AUTH_KEY = 'aios-auth-v1';

  var guest = document.getElementById('prjGuest');
  var guestMsg = document.getElementById('prjGuestMsg');
  var guestLogin = document.getElementById('prjGuestLogin');
  var wrap = document.getElementById('prjWrap');
  var grid = document.getElementById('prjGrid');
  var msg = document.getElementById('prjMsg');
  var emptyActions = document.getElementById('prjEmptyActions');
  if (!guest || !wrap || !grid) return;

  function getSession() {
    try {
      var raw = localStorage.getItem(AUTH_KEY);
      var s = raw ? JSON.parse(raw) : null;
      return s && s.token ? s : null;
    } catch (e) {
      return null;
    }
  }

  function showMsg(text, kind) {
    if (!msg) return;
    msg.textContent = text || '';
    msg.className = 'login-msg login-msg--' + (kind || 'error');
    msg.hidden = !text;
  }

  function fmtDate(ts) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '';
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear();
  }

  /* ---------- карточка в сетке ---------- */

  var FIELD_TITLES = {
    title: 'Название',
    goal: 'Цель',
    description: 'Описание',
    team_size: 'Участников',
    participants: 'Участники (ФИО)',
    departments: 'Подразделения',
    budget: 'Бюджет',
    groundwork: 'Задел',
    presentation: 'Презентация'
  };

  var LIST_TITLES = {
    groundwork_links: 'Ссылки задела',
    groundwork_files: 'Файлы задела (.pdf)',
    presentation_files: 'Файлы презентации (.pdf)'
  };

  function makeCard(p) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'prj-card' + (p.status === 'draft' ? ' prj-card--draft' : '');
    var head = document.createElement('span');
    head.className = 'prj-card-head';
    var title = document.createElement('span');
    title.className = 'prj-card-title';
    title.textContent = p.title || 'Без названия';
    var status = document.createElement('span');
    status.className = 'prj-status prj-status--' + (p.status === 'active' ? 'active' : 'draft');
    status.textContent = p.status === 'active' ? 'активен' : 'черновик';
    head.appendChild(title);
    head.appendChild(status);
    var sub = document.createElement('span');
    sub.className = 'prj-card-sub';
    var bits = [];
    if (p.goal) bits.push(p.goal.length > 60 ? p.goal.slice(0, 60) + '…' : p.goal);
    if (p.team_size) bits.push('участников: ' + p.team_size);
    if (p.budget) bits.push('бюджет: ' + p.budget);
    sub.textContent = bits.join(' · ');
    var desc = document.createElement('span');
    desc.className = 'prj-card-desc';
    var text = p.description || 'Описание пока не заполнено — продолжите онбординг в рабочем чате.';
    desc.textContent = text.length > 180 ? text.slice(0, 180) + '…' : text;
    var foot = document.createElement('span');
    foot.className = 'prj-card-foot';
    foot.textContent = 'обновлено: ' + fmtDate(p.updated_at);
    card.appendChild(head);
    card.appendChild(sub);
    card.appendChild(desc);
    card.appendChild(foot);
    card.addEventListener('click', function () { openDetails(p); });
    return card;
  }

  /* ---------- модальное окно с деталями ---------- */

  var overlay = null;
  var currentCard = null;

  function isAdmin() {
    var session = getSession();
    return !!session && session.role === 'Администратор';
  }

  function appendListRow(fields, title, items) {
    if (!items || !items.length) return;
    var row = document.createElement('div');
    row.className = 'skill-meta-row';
    var dt = document.createElement('dt');
    dt.textContent = title;
    var dd = document.createElement('dd');
    dd.textContent = items.join('; ');
    row.appendChild(dt);
    row.appendChild(dd);
    fields.appendChild(row);
  }

  function ensureModal() {
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="modal modal--project" role="dialog" aria-modal="true" aria-labelledby="prjModalTitle">' +
      '  <button class="modal-close" id="prjModalClose" type="button" aria-label="Закрыть">✕</button>' +
      '  <h2 id="prjModalTitle"></h2>' +
      '  <dl class="skill-meta" id="prjModalFields"></dl>' +
      '  <div class="prj-modal-actions">' +
      '    <button class="btn-primary mm-btn" id="prjModalChat" type="button">Продолжить в рабочем чате</button>' +
      '    <button class="btn-primary mm-btn prj-btn-ghost" id="prjModalEdit" type="button">Редактировать</button>' +
      '    <button class="btn-primary mm-btn prj-btn-danger" id="prjModalDelete" type="button">Удалить</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) closeDetails();
    });
    var close = document.getElementById('prjModalClose');
    if (close) close.addEventListener('click', closeDetails);
    var chat = document.getElementById('prjModalChat');
    if (chat) chat.addEventListener('click', function () {
      var btn = document.getElementById('btnNavWorkspace') || document.getElementById('btnWorkspace');
      closeDetails();
      if (btn) btn.click();
    });
    var editBtn = document.getElementById('prjModalEdit');
    if (editBtn) editBtn.addEventListener('click', openEditModal);
    var delBtn = document.getElementById('prjModalDelete');
    if (delBtn) delBtn.addEventListener('click', deleteCurrent);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && overlay && !overlay.hidden) closeDetails();
    });
    return overlay;
  }

  function deleteCurrent() {
    var delBtn = document.getElementById('prjModalDelete');
    if (!currentCard || !API || !API.projectsDelete || !delBtn) return;
    var name = currentCard.title || currentCard.id;
    if (!window.confirm('Удалить проект «' + name + '»? Действие необратимо.')) return;
    delBtn.disabled = true;
    API.projectsDelete(getSession().token, currentCard.id)
      .then(function () {
        closeDetails();
        load();
      })
      .catch(function (err) {
        showMsg('Не удалось удалить: ' + (err && err.message ? err.message : 'ошибка сети'));
      })
      .then(function () { delBtn.disabled = false; });
  }

  function closeDetails() {
    if (overlay) {
      overlay.hidden = true;
      document.body.classList.remove('modal-open');
    }
  }

  function openDetails(p) {
    ensureModal();
    currentCard = p;
    var title = document.getElementById('prjModalTitle');
    var fields = document.getElementById('prjModalFields');
    title.textContent = p.title || 'Карточка проекта';
    fields.innerHTML = '';
    Object.keys(FIELD_TITLES).forEach(function (key) {
      var value = p[key];
      var row = document.createElement('div');
      row.className = 'skill-meta-row';
      var dt = document.createElement('dt');
      dt.textContent = FIELD_TITLES[key];
      var dd = document.createElement('dd');
      if (key === 'presentation') {
        dd.textContent = value ? 'есть' : 'нет';
      } else {
        dd.textContent = String(value || '').trim() || '—';
      }
      row.appendChild(dt);
      row.appendChild(dd);
      fields.appendChild(row);
    });
    Object.keys(LIST_TITLES).forEach(function (key) {
      appendListRow(fields, LIST_TITLES[key], p[key]);
    });
    var delBtn = document.getElementById('prjModalDelete');
    if (delBtn) delBtn.hidden = !isAdmin();
    overlay.hidden = false;
    document.body.classList.add('modal-open');
  }

  /* ---------- правка карточки (руководитель проекта) ---------- */

  var editOverlay = null;

  var EDIT_FIELDS = [
    ['title', 'Название', 'input'],
    ['goal', 'Цель (SMART)', 'textarea'],
    ['description', 'Описание', 'textarea'],
    ['team_size', 'Количество участников', 'input'],
    ['participants', 'ФИО участников', 'textarea'],
    ['departments', 'Структурные подразделения', 'input'],
    ['budget', 'Бюджет запуска', 'input'],
    ['groundwork', 'Задел (публикации, патенты и т.п.)', 'textarea'],
    ['groundwork_links', 'Ссылки задела (по одной на строку)', 'textarea'],
    ['groundwork_files', 'Файлы задела .pdf (по одному названию на строку)', 'textarea'],
    ['presentation_files', 'Файлы презентации .pdf (по одному названию на строку)', 'textarea']
  ];

  function ensureEditModal() {
    if (editOverlay) return editOverlay;
    editOverlay = document.createElement('div');
    editOverlay.className = 'modal-overlay';
    editOverlay.hidden = true;
    var html =
      '<div class="modal modal--project" role="dialog" aria-modal="true" aria-labelledby="prjEditTitle">' +
      '  <button class="modal-close" id="prjEditClose" type="button" aria-label="Закрыть">✕</button>' +
      '  <h2 id="prjEditTitle">Правка карточки проекта</h2>' +
      '  <form class="prj-edit-form" id="prjEditForm">';
    EDIT_FIELDS.forEach(function (f) {
      html += '    <label><span>' + f[1] + '</span>' +
        (f[2] === 'textarea'
          ? '<textarea rows="3" name="' + f[0] + '"></textarea>'
          : '<input type="text" name="' + f[0] + '">') +
        '</label>';
    });
    html +=
      '    <label class="prj-edit-check"><input type="checkbox" name="presentation"> Нужна презентация проекта</label>' +
      '    <p class="login-msg" id="prjEditMsg" hidden></p>' +
      '    <button class="btn-primary" id="prjEditSave" type="submit">Сохранить</button>' +
      '  </form>' +
      '</div>';
    editOverlay.innerHTML = html;
    document.body.appendChild(editOverlay);
    editOverlay.addEventListener('click', function (e) {
      if (e.target === editOverlay) closeEditModal();
    });
    var close = document.getElementById('prjEditClose');
    if (close) close.addEventListener('click', closeEditModal);
    document.getElementById('prjEditForm').addEventListener('submit', function (e) {
      e.preventDefault();
      saveEdit();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && editOverlay && !editOverlay.hidden) closeEditModal();
    });
    return editOverlay;
  }

  function closeEditModal() {
    if (editOverlay) editOverlay.hidden = true;
  }

  function showEditMsg(text, kind) {
    var el = document.getElementById('prjEditMsg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'login-msg login-msg--' + (kind || 'error');
    el.hidden = !text;
  }

  function openEditModal() {
    if (!currentCard) return;
    ensureEditModal();
    var form = document.getElementById('prjEditForm');
    EDIT_FIELDS.forEach(function (f) {
      var value = currentCard[f[0]];
      form.elements[f[0]].value = Array.isArray(value) ? value.join('\n') : String(value || '');
    });
    form.elements.presentation.checked = !!currentCard.presentation;
    showEditMsg('');
    editOverlay.hidden = false;
  }

  function saveEdit() {
    if (!currentCard || !API || !API.projectsUpdate) return;
    var form = document.getElementById('prjEditForm');
    var patch = {};
    EDIT_FIELDS.forEach(function (f) {
      patch[f[0]] = form.elements[f[0]].value.trim();
    });
    patch.presentation = form.elements.presentation.checked;
    var btn = document.getElementById('prjEditSave');
    if (btn) btn.disabled = true;
    API.projectsUpdate(getSession().token, currentCard.id, patch)
      .then(function () {
        closeEditModal();
        load();
      })
      .catch(function (err) {
        showEditMsg('Не удалось сохранить: ' + (err && err.message ? err.message : 'ошибка сети'));
      })
      .then(function () { if (btn) btn.disabled = false; });
  }

  /* ---------- загрузка и режимы страницы ---------- */

  function renderState(authed, guestText) {
    guest.hidden = !!authed;
    wrap.hidden = !authed;
    if (guestMsg) {
      guestMsg.textContent = guestText || 'Карточки проектов доступны только после входа в AIOS.';
    }
  }

  function load() {
    var session = getSession();
    if (!session || !API || !API.projectsList) {
      renderState(false);
      return;
    }
    renderState(true);
    showMsg('');
    API.projectsList(session.token)
      .then(function (data) {
        var items = (data && data.items) || [];
        grid.innerHTML = '';
        if (emptyActions) emptyActions.hidden = items.length > 0;
        if (!items.length) {
          var empty = document.createElement('div');
          empty.className = 'models-empty';
          empty.textContent = 'Проектов пока нет. Запустите навык «Онбординг проекта» в рабочем чате — он соберёт карточку за несколько вопросов.';
          grid.appendChild(empty);
          return;
        }
        items.forEach(function (p) {
          grid.appendChild(makeCard(p));
        });
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          try { localStorage.removeItem(AUTH_KEY); } catch (e) { /* приватный режим */ }
          renderState(false, 'Сессия истекла — войдите заново.');
        } else {
          showMsg('Не удалось загрузить проекты: ' + (err && err.message ? err.message : 'ошибка сети'));
        }
      });
  }

  if (guestLogin) {
    guestLogin.addEventListener('click', function () {
      var btnLogin = document.getElementById('btnLogin');
      if (btnLogin) btnLogin.click(); /* модалку входа показывает auth.js */
    });
  }

  /* «Запустить онбординг»: навык активируется в рабочем чате
     (тот же механизм, что и кнопка в модалке навыка на «Агентах») */
  var startBtn = document.getElementById('prjStartOnboarding');
  if (startBtn) {
    startBtn.addEventListener('click', function () {
      try {
        localStorage.setItem('aios-workspace-skill-v1', JSON.stringify({
          skill_id: 'project-onboarding',
          name: 'Онбординг проекта',
          open: true
        }));
      } catch (e) { /* приватный режим */ }
      var btn = document.getElementById('btnNavWorkspace') || document.getElementById('btnWorkspace');
      if (btn) btn.click();
    });
  }

  window.addEventListener('aios-auth-change', load);

  load();
})();
