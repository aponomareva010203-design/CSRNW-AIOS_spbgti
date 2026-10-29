/* ============================================================
   «Проекты»: карточки проектов всех команд.

   Данные — GET /api/projects (активные карточки всех владельцев
   плюс свои черновики; каждая карточка помечена owner — ФИО
   руководителя — и is_own). Карточки создаёт навык «Онбординг
   проекта» в рабочем чате; здесь их читают все вошедшие
   пользователи, правит руководитель проекта (PATCH
   /api/projects/{id}, только is_own) и удаляет администратор
   (DELETE — роль «Администратор»). Гость видит предложение войти.
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

  /* два типа проектов платформы проректора (api/runtime/projects.PROJECT_TYPES) */
  var TYPE_TITLES = {
    startup: 'Технологический проект / стартап',
    initiative: 'Управленческая инициатива'
  };

  var FIELD_TITLES = {
    /* название не дублируем — оно уже в заголовке модалки */
    project_type: 'Тип проекта',
    goal: 'Цель',
    description: 'Описание',
    technology: 'Технология и РИД',
    trl: 'Уровень готовности (УГТ)',
    market: 'Рынок и клиент',
    funding_stage: 'Стадия финансирования',
    commercialization: 'Путь на рынок',
    kpi: 'Целевые показатели (KPI)',
    milestones: 'Этапы и контрольные точки',
    risks: 'Риски',
    team_size: 'Участников',
    participants: 'Состав команды (ФИО)',
    departments: 'Подразделения',
    budget: 'Бюджет',
    groundwork: 'Задел',
    presentation: 'Презентация'
  };

  var TYPED_FIELDS = ['technology', 'trl', 'market', 'funding_stage', 'commercialization', 'kpi', 'milestones', 'risks'];

  var LIST_TITLES = {
    groundwork_links: 'Ссылки задела',
    groundwork_files: 'Файлы задела (.pdf)',
    presentation_files: 'Файлы презентации (.pdf)'
  };

  var ARTIFACT_NAMES = {
    rag: 'RAG и онтология',
    agents: 'Агенты',
    models: 'Модели',
    polygon: 'Виртуальный полигон',
    social: 'Социальная сеть агентов'
  };

  /* уровни доступа участников проекта — названия как на GitHub
     (read/write/admin; owner — руководитель, вне списка выдачи) */
  var LEVEL_LABELS = {
    owner: 'Руководитель проекта',
    read: 'Просмотр',
    write: 'Редактирование',
    admin: 'Полный доступ'
  };
  var LEVEL_OPTIONS = ['read', 'write', 'admin'];

  /* разделы JSON-карточки гипотез (data/hypotheses/hyp-{id}.json) */
  var HYP_SECTIONS = {
    ontology: 'Онтология',
    rag: 'RAG и поиск',
    agents: 'Агенты',
    models: 'Модели',
    polygons: 'Виртуальный полигон',
    dashboards: 'Дашборды'
  };

  /* у гипотезы видны заголовок, краткое пояснение и спойлер с деталями */
  var HYP_TEASER_LEN = 220;
  var HYP_DETAILS_FIELDS = {
    statement: 'Формулировка (если–то–проверим)',
    metric: 'Метрика подтверждения',
    deadline: 'Срок проверки',
    decision: 'Решение по итогам',
    as_is: 'Как есть',
    verification: 'Способ проверки',
    falsification: 'Условия опровержения',
    users: 'Для кого',
    groundwork: 'Задел'
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
    if (p.admin_only) {
      /* тестовая карточка (admin_only): на интерфейсе видна только админам */
      var test = document.createElement('span');
      test.className = 'prj-status prj-status--test';
      test.textContent = 'тест';
      head.appendChild(test);
    }
    var sub = document.createElement('span');
    sub.className = 'prj-card-sub';
    var bits = [];
    if (p.owner) bits.push('руководитель: ' + p.owner);
    if (!p.is_own && p.my_level && LEVEL_LABELS[p.my_level]) {
      bits.push('ваш доступ: ' + LEVEL_LABELS[p.my_level].toLowerCase());
    }
    if (TYPE_TITLES[p.project_type]) bits.push(TYPE_TITLES[p.project_type].toLowerCase());
    if (p.trl) bits.push('УГТ ' + p.trl);
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

  function appendListRow(fields, title, items, opts) {
    if (!items || !items.length) return;
    var row = document.createElement('div');
    row.className = 'skill-meta-row';
    var dt = document.createElement('dt');
    dt.textContent = title;
    var dd = document.createElement('dd');
    opts = opts || {};
    items.forEach(function (item, idx) {
      if (idx) dd.appendChild(document.createTextNode('; '));
      /* файлы, прикреплённые онбордингом через attach_project_file, —
         ссылки на скачивание; имена-заглушки (файла нет на сервере)
         ведут на 404, но остаются читаемым текстом */
      if (opts.projectId && opts.downloadable) {
        var link = document.createElement('a');
        link.href = '/api/projects/' + encodeURIComponent(opts.projectId) +
          '/files/' + encodeURIComponent(item);
        link.target = '_blank';
        link.textContent = item;
        dd.appendChild(link);
      } else {
        dd.appendChild(document.createTextNode(item));
      }
    });
    row.appendChild(dt);
    row.appendChild(dd);
    fields.appendChild(row);
  }

  function appendHypotheses(fields, artifacts) {
    if (!artifacts) return;
    var entries = Object.keys(ARTIFACT_NAMES).filter(function (key) {
      return artifacts[key] && artifacts[key].hypothesis;
    });
    if (!entries.length) return;
    var row = makeHypContainer('Гипотезы проекта (' + entries.length + ')');
    row.id = 'prjModalShortHyp';
    var dd = document.createElement('dd');
    dd.className = 'prj-hyp-list';
    /* тот же вид, что у полной карточки гипотез: группа на артефакт */
    entries.forEach(function (key) {
      var group = document.createElement('div');
      group.className = 'prj-hyp-group';
      var groupTitle = document.createElement('div');
      groupTitle.className = 'prj-hyp-group-title';
      groupTitle.textContent = ARTIFACT_NAMES[key];
      group.appendChild(groupTitle);
      group.appendChild(makeHypItem({
        title: ARTIFACT_NAMES[key],
        statement: artifacts[key].hypothesis
      }, {}));
      dd.appendChild(group);
    });
    row.appendChild(dd);
    fields.appendChild(row);
  }

  /* ---------- гипотезы из JSON-карточки (data/hypotheses) ---------- */

  var hypReqSeq = 0; /* защита от гонки: открыт другой проект */

  function hypRow(dt, ddText) {
    var row = document.createElement('div');
    row.className = 'skill-meta-row';
    var term = document.createElement('dt');
    term.textContent = dt;
    var dd = document.createElement('dd');
    dd.textContent = ddText;
    row.appendChild(term);
    row.appendChild(dd);
    return row;
  }

  function makeHypItem(hyp, allTitles) {
    var item = document.createElement('div');
    item.className = 'prj-hyp';
    var head = document.createElement('div');
    head.className = 'prj-hyp-head';
    var name = document.createElement('span');
    name.className = 'prj-hyp-title';
    name.textContent = hyp.title || hyp.id || 'Гипотеза';
    head.appendChild(name);
    if (hyp.skipped) {
      var skip = document.createElement('span');
      skip.className = 'prj-status prj-status--draft';
      skip.textContent = 'пропущена';
      head.appendChild(skip);
    }
    item.appendChild(head);
    /* краткое пояснение — начало формулировки гипотезы */
    var teaserText = String(hyp.statement || '').trim();
    if (teaserText) {
      if (teaserText.length > HYP_TEASER_LEN) teaserText = teaserText.slice(0, HYP_TEASER_LEN) + '…';
      var teaser = document.createElement('div');
      teaser.className = 'prj-hyp-teaser';
      teaser.textContent = teaserText;
      item.appendChild(teaser);
    }
    var extras = [];
    Object.keys(HYP_DETAILS_FIELDS).forEach(function (key) {
      if (hyp[key]) extras.push([HYP_DETAILS_FIELDS[key], hyp[key]]);
    });
    if (Array.isArray(hyp.depends_on) && hyp.depends_on.length) {
      var names = hyp.depends_on.map(function (id) {
        return allTitles[id] || id;
      });
      extras.push(['Зависит от', names.join('; ')]);
    }
    if (extras.length) {
      var details = document.createElement('details');
      details.className = 'prj-hyp-details';
      var summary = document.createElement('summary');
      summary.textContent = 'Подробнее';
      details.appendChild(summary);
      extras.forEach(function (pair) {
        details.appendChild(hypRow(pair[0], pair[1]));
      });
      item.appendChild(details);
    }
    return item;
  }

  function makeHypContainer(captionText) {
    var row = document.createElement('div');
    row.className = 'skill-meta-row';
    var term = document.createElement('dt');
    term.textContent = captionText;
    row.appendChild(term);
    return row;
  }

  function renderHypotheses(container, data) {
    container.innerHTML = '';
    var allTitles = {};
    var total = 0;
    Object.keys(HYP_SECTIONS).forEach(function (section) {
      (Array.isArray(data[section]) ? data[section] : []).forEach(function (h) {
        if (h && h.id) allTitles[h.id] = h.title || h.id;
        total += 1;
      });
    });
    if (!total) {
      container.hidden = true;
      return;
    }
    container.appendChild(makeHypContainer('Гипотезы проекта (' + total + ')'));
    var dd = document.createElement('dd');
    dd.className = 'prj-hyp-list';
    Object.keys(HYP_SECTIONS).forEach(function (section) {
      var items = Array.isArray(data[section]) ? data[section] : [];
      if (!items.length) return;
      var group = document.createElement('div');
      group.className = 'prj-hyp-group';
      var groupTitle = document.createElement('div');
      groupTitle.className = 'prj-hyp-group-title';
      groupTitle.textContent = HYP_SECTIONS[section];
      group.appendChild(groupTitle);
      items.forEach(function (h) {
        group.appendChild(makeHypItem(h || {}, allTitles));
      });
      dd.appendChild(group);
    });
    container.appendChild(dd);
    container.hidden = false;
  }

  function loadHypotheses(p) {
    var container = document.getElementById('prjModalHyp');
    if (!container) return;
    container.hidden = true;
    container.innerHTML = '';
    if (!API || !API.projectsHypotheses) return;
    var seq = ++hypReqSeq;
    API.projectsHypotheses(getSession().token, p.id)
      .then(function (data) {
        if (seq !== hypReqSeq || !data) return;
        /* полная карточка есть — краткий блок artifacts не показываем */
        var shortRow = document.getElementById('prjModalShortHyp');
        if (shortRow) shortRow.hidden = true;
        renderHypotheses(container, data);
      })
      .catch(function () {
        if (seq !== hypReqSeq) return;
        container.hidden = true; /* карточки гипотез нет — остаётся краткий блок */
      });
  }

  /* ---------- участники и права (GET/PUT/DELETE .../members) ---------- */

  var memberReqSeq = 0; /* защита от гонки: открыт другой проект */

  function canManageMembers(p) {
    /* уровни меняет руководитель, участник с «Полным доступом» и админ роли */
    return !!p && (p.is_own || p.my_level === 'admin' || isAdmin());
  }

  function memberRow(member, p) {
    var row = document.createElement('div');
    row.className = 'prj-member';
    var name = document.createElement('span');
    name.className = 'prj-member-name';
    name.textContent = member.full_name || member.user_id;
    if (member.position) name.title = member.position;
    row.appendChild(name);
    if (member.level === 'owner' || !canManageMembers(p)) {
      var chip = document.createElement('span');
      chip.className = 'prj-level prj-level--' + (member.level || 'read');
      chip.textContent = LEVEL_LABELS[member.level] || member.level || '';
      row.appendChild(chip);
      return row;
    }
    var select = document.createElement('select');
    select.className = 'prj-member-select';
    select.setAttribute('aria-label', 'Уровень доступа участника');
    select.setAttribute('data-prev', member.level);
    LEVEL_OPTIONS.forEach(function (lvl) {
      var opt = document.createElement('option');
      opt.value = lvl;
      opt.textContent = LEVEL_LABELS[lvl];
      if (member.level === lvl) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener('change', function () {
      saveMemberLevel(p, member, select);
    });
    row.appendChild(select);
    if (member.source === 'acl') {
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'prj-member-remove';
      del.setAttribute('aria-label', 'Снять уровень доступа');
      del.textContent = '✕';
      del.title = 'Снять выданный уровень (автопросмотр останется, пока человек в составе команды)';
      del.addEventListener('click', function () {
        removeMember(p, member);
      });
      row.appendChild(del);
    }
    return row;
  }

  function showMembersMsg(text) {
    var el = document.getElementById('prjMembersMsg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'login-msg login-msg--error';
    el.hidden = !text;
  }

  function saveMemberLevel(p, member, select) {
    var session = getSession();
    if (!session || !API.projectsSetMember) return;
    var prev = select.getAttribute('data-prev') || member.level;
    var wanted = select.value;
    API.projectsSetMember(session.token, p.id, member.user_id, wanted)
      .then(function () {
        select.setAttribute('data-prev', wanted);
        /* перезагружаем: источник права сменился (по ФИО → явный уровень) */
        loadMembers(p, true);
      })
      .catch(function (err) {
        select.value = prev; /* откатываем переключение */
        if (err && err.status === 401) {
          try { localStorage.removeItem(AUTH_KEY); } catch (e) { /* приватный режим */ }
          showMembersMsg('Сессия истекла — войдите заново.');
          return;
        }
        showMembersMsg('Не удалось сохранить уровень: ' +
          (err && err.message ? err.message : 'ошибка сети'));
      });
  }

  function removeMember(p, member) {
    var session = getSession();
    if (!session || !API.projectsRemoveMember) return;
    API.projectsRemoveMember(session.token, p.id, member.user_id)
      .then(function () {
        loadMembers(p, true);
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          try { localStorage.removeItem(AUTH_KEY); } catch (e) { /* приватный режим */ }
          showMembersMsg('Сессия истекла — войдите заново.');
          return;
        }
        showMembersMsg('Не удалось снять уровень: ' +
          (err && err.message ? err.message : 'ошибка сети'));
      });
  }

  function renderMembers(container, p, data) {
    container.innerHTML = '';
    var dt = document.createElement('dt');
    dt.textContent = 'Участники и права';
    var dd = document.createElement('dd');
    dd.className = 'prj-members';
    ((data && data.items) || []).forEach(function (m) {
      dd.appendChild(memberRow(m || {}, p));
    });
    var unreg = (data && data.unregistered) || [];
    if (unreg.length) {
      var note = document.createElement('div');
      note.className = 'prj-members-unreg';
      note.textContent = 'Без учётной записи (доступ откроется после регистрации): ' +
        unreg.join(', ');
      dd.appendChild(note);
    }
    var hint = document.createElement('div');
    hint.className = 'prj-members-hint';
    hint.textContent =
      'Участники из состава команды видят артефакты и черновики проекта автоматически ' +
      '(Просмотр); выданный уровень это перекрывает. Чтобы закрыть доступ полностью — ' +
      'уберите человека из «Состава команды» в правке карточки.';
    dd.appendChild(hint);
    var msgEl = document.createElement('div');
    msgEl.id = 'prjMembersMsg';
    msgEl.hidden = true;
    dd.appendChild(msgEl);
    container.appendChild(dt);
    container.appendChild(dd);
    container.hidden = false;
  }

  function loadMembers(p, keep) {
    var container = document.getElementById('prjModalMembers');
    var session = getSession();
    if (!container || !session || !API || !API.projectsMembers) return;
    if (!keep) {
      container.hidden = true;
      container.innerHTML = '';
    }
    var seq = ++memberReqSeq;
    API.projectsMembers(session.token, p.id)
      .then(function (data) {
        if (seq !== memberReqSeq || !data) return;
        renderMembers(container, p, data);
      })
      .catch(function () {
        if (seq !== memberReqSeq) return;
        container.hidden = true; /* нет доступа к списку — секция не показывается */
      });
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
      /* руководитель — непосредственно перед составом команды */
      if (key === 'participants' && p.owner) {
        var ownerRow = document.createElement('div');
        ownerRow.className = 'skill-meta-row';
        var ownerDt = document.createElement('dt');
        ownerDt.textContent = 'Руководитель проекта';
        var ownerDd = document.createElement('dd');
        ownerDd.textContent = p.owner;
        ownerRow.appendChild(ownerDt);
        ownerRow.appendChild(ownerDd);
        fields.appendChild(ownerRow);
      }
      var value = p[key];
      /* поля одного из типов проекта не показываем пустыми у другого */
      if (TYPED_FIELDS.indexOf(key) >= 0 && !String(value || '').trim()) return;
      var row = document.createElement('div');
      row.className = 'skill-meta-row';
      var dt = document.createElement('dt');
      dt.textContent = FIELD_TITLES[key];
      var dd = document.createElement('dd');
      if (key === 'project_type') {
        dd.textContent = TYPE_TITLES[value] || '—';
      } else if (key === 'presentation') {
        dd.textContent = value ? 'есть' : 'нет';
      } else {
        dd.textContent = String(value || '').trim() || '—';
      }
      row.appendChild(dt);
      row.appendChild(dd);
      fields.appendChild(row);
    });
    Object.keys(LIST_TITLES).forEach(function (key) {
      appendListRow(fields, LIST_TITLES[key], p[key], {
        projectId: p.id,
        downloadable: key !== 'groundwork_links'
      });
    });
    appendHypotheses(fields, p.artifacts);
    var hyp = document.createElement('div');
    hyp.className = 'skill-meta-row';
    hyp.id = 'prjModalHyp';
    hyp.hidden = true;
    fields.appendChild(hyp);
    loadHypotheses(p);
    var members = document.createElement('div');
    members.className = 'skill-meta-row';
    members.id = 'prjModalMembers';
    members.hidden = true;
    fields.appendChild(members);
    loadMembers(p);
    /* участник с доступом видит свой уровень; правит карточку по уровню
       «Редактирование» и выше (как и руководитель) */
    if (!p.is_own && LEVEL_LABELS[p.my_level]) {
      var access = document.createElement('div');
      access.className = 'skill-meta-row';
      var accessDt = document.createElement('dt');
      accessDt.textContent = 'Ваш доступ';
      var accessDd = document.createElement('dd');
      var chip = document.createElement('span');
      chip.className = 'prj-level prj-level--' + p.my_level;
      chip.textContent = LEVEL_LABELS[p.my_level];
      accessDd.appendChild(chip);
      access.appendChild(accessDt);
      access.appendChild(accessDd);
      fields.insertBefore(access, fields.firstChild);
    }
    var delBtn = document.getElementById('prjModalDelete');
    if (delBtn) delBtn.hidden = !isAdmin();
    var editBtn = document.getElementById('prjModalEdit');
    var canEdit = p.is_own || p.my_level === 'write' || p.my_level === 'admin';
    if (editBtn) editBtn.hidden = !canEdit;
    overlay.hidden = false;
    document.body.classList.add('modal-open');
  }

  /* ---------- правка карточки (руководитель проекта) ---------- */

  var editOverlay = null;

  var EDIT_FIELDS = [
    ['title', 'Название', 'input'],
    ['project_type', 'Тип проекта', 'select'],
    ['goal', 'Цель (SMART)', 'textarea'],
    ['description', 'Описание', 'textarea'],
    ['technology', 'Технология и РИД (стартап)', 'textarea'],
    ['trl', 'УГТ 1–9 (стартап)', 'input'],
    ['market', 'Рынок, клиент, подтверждение спроса (стартап)', 'textarea'],
    ['funding_stage', 'Стадия финансирования (стартап)', 'input'],
    ['commercialization', 'Путь на рынок: лицензия, МТК, стартап (стартап)', 'input'],
    ['kpi', 'Целевые показатели (инициатива)', 'textarea'],
    ['milestones', 'Этапы и контрольные точки', 'textarea'],
    ['risks', 'Риски', 'textarea'],
    ['team_size', 'Количество участников', 'input'],
    ['participants', 'ФИО участников', 'textarea'],
    ['departments', 'Структурные подразделения', 'input'],
    ['budget', 'Бюджет запуска', 'input'],
    ['groundwork', 'Задел (публикации, патенты и т.п.)', 'textarea'],
    ['groundwork_links', 'Ссылки задела (по одной на строку)', 'textarea']
  ];

  /* файлы прикрепляются вложениями (multipart), а не названиями */
  var EDIT_FILE_FIELDS = [
    ['groundwork_files', 'Файлы задела (.pdf)'],
    ['presentation_files', 'Файлы презентации (.pdf)']
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
          : f[2] === 'select'
            ? '<select name="' + f[0] + '"><option value="">— не выбран —</option>' +
              Object.keys(TYPE_TITLES).map(function (k) { return '<option value="' + k + '">' + TYPE_TITLES[k] + '</option>'; }).join('') +
              '</select>'
            : '<input type="text" name="' + f[0] + '">') +
        '</label>';
    });
    EDIT_FILE_FIELDS.forEach(function (f) {
      html += '    <label><span>' + f[1] + '</span>' +
        '<input type="file" name="' + f[0] + '" multiple accept=".pdf,application/pdf"></label>';
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
    var files = [];
    EDIT_FILE_FIELDS.forEach(function (f) {
      Array.prototype.forEach.call(form.elements[f[0]].files || [], function (file) {
        files.push({ kind: f[0], file: file });
      });
    });
    API.projectsUpdate(getSession().token, currentCard.id, patch)
      .then(function () {
        /* новые файлы прикрепляются к сохранённой карточке по одному */
        return files.reduce(function (chain, item) {
          return chain.then(function () {
            return API.projectsUploadFile(getSession().token, currentCard.id, item.kind, item.file);
          });
        }, Promise.resolve());
      })
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
        /* глубокая ссылка из каталогов (knowledge.html и др.):
           projects.html?id=prj-… открывает карточку этого проекта */
        var wanted = new URLSearchParams(window.location.search).get('id');
        if (wanted) {
          var target = items.find(function (p) { return p.id === wanted; });
          if (target) openDetails(target);
        }
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

  window.addEventListener('aios-auth-change', load);

  load();
})();
