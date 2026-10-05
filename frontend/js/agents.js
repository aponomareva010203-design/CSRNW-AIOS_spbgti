/* ============================================================
   Страница «Агенты».
   Карточки агентов приходят из data/skills.json (в коде не
   хардкодятся) и раскладываются по трём разделам по полю
   category: general — общие, special — специальные,
   service — служебные. Данные грузятся лениво при первом
   разворачивании спойлера.

   Обратная связь (звёзды, комментарии, применение в проектах) —
   из /api/catalog/agents* (api.js): счётчики подтягиваются в
   карточки, подробности и форма — в модалке агента. Чтение
   публично; звезда и комментарий — после входа (aios-auth-v1).
   ============================================================ */

(function () {
  var SECTIONS = [
    { spoiler: 'spoilerGeneral', grid: 'gridGeneral', category: 'general' },
    { spoiler: 'spoilerSpecial', grid: 'gridSpecial', category: 'special' },
    { spoiler: 'spoilerService', grid: 'gridService', category: 'service' }
  ];

  var AUTH_KEY = 'aios-auth-v1';

  /* ---------- утилиты ---------- */

  function fetchJSON(url) {
    return fetch(url, { headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      });
  }

  function authSession() {
    try {
      var raw = localStorage.getItem(AUTH_KEY);
      var s = raw ? JSON.parse(raw) : null;
      return s && s.token ? s : null;
    } catch (e) {
      return null;
    }
  }

  function authToken() {
    var s = authSession();
    return s ? s.token : null;
  }

  function fmtDate(ts) {
    if (!ts) return '';
    try {
      var d = new Date(ts * 1000);
      return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
    } catch (e) {
      return '';
    }
  }

  function plural(n, one, few, many) {
    var mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
    return many;
  }

  function showSkeletons(grid, count) {
    grid.innerHTML = '';
    for (var i = 0; i < count; i++) {
      var sk = document.createElement('div');
      sk.className = 'skill-card skill-card--skeleton';
      sk.setAttribute('aria-hidden', 'true');
      grid.appendChild(sk);
    }
  }

  function showError(grid, retry) {
    grid.innerHTML = '';
    var box = document.createElement('div');
    box.className = 'models-error';
    var text = document.createElement('span');
    text.textContent = 'Не удалось загрузить данные. Проверьте соединение и попробуйте ещё раз.';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-register models-retry';
    btn.textContent = 'Повторить';
    btn.addEventListener('click', retry);
    box.appendChild(text);
    box.appendChild(btn);
    grid.appendChild(box);
  }

  function showEmpty(grid, message) {
    grid.innerHTML = '';
    var box = document.createElement('div');
    box.className = 'models-empty';
    box.textContent = message;
    grid.appendChild(box);
  }

  /* ---------- модальное окно агента ---------- */

  function bindModal(overlay, closeBtn) {
    function close() {
      overlay.hidden = true;
      document.body.classList.remove('modal-open');
    }
    closeBtn.addEventListener('click', close);
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) close();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !overlay.hidden) close();
    });
    return {
      open: function () {
        overlay.hidden = false;
        document.body.classList.add('modal-open');
        closeBtn.focus();
      },
      close: close
    };
  }

  var modalOverlay = document.getElementById('skillModal');
  var skillModal = modalOverlay
    ? bindModal(modalOverlay, document.getElementById('skillModalClose'))
    : null;

  var currentAgent = null;   /* карточка открытого в модалке агента */
  var currentFeedback = null; /* последний ответ /feedback по нему */

  /* ---------- обратная связь: карточки ---------- */

  var overviewPromise = null;

  function overview() {
    if (!window.AIOS_API || !window.AIOS_API.catalogAgentsOverview) {
      return Promise.resolve({ items: {} });
    }
    if (!overviewPromise) {
      overviewPromise = window.AIOS_API.catalogAgentsOverview().catch(function () {
        return { items: {} }; /* счётчики не критичны — карточки работают и без них */
      });
    }
    return overviewPromise;
  }

  function renderCardStats(card, agent) {
    var stats = card.querySelector('.skill-card-stats');
    if (!stats) return;
    stats.innerHTML = '';
    overview().then(function (data) {
      var counts = ((data || {}).items || {})[agent.id];
      if (!counts) return;
      [
        { cls: 'is-star', label: 'звёзды', value: counts.stars, sign: '★' },
        { cls: 'is-comments', label: 'комментарии', value: counts.comments, sign: '✎' },
        { cls: 'is-projects', label: 'применение в проектах', value: counts.projects, sign: '◇' }
      ].forEach(function (item) {
        if (!item.value) return;
        var chip = document.createElement('span');
        chip.className = 'skill-stat ' + item.cls;
        chip.title = item.value + ' ' + plural(item.value, 'единица', 'единицы', 'единиц') + ': ' + item.label;
        chip.setAttribute('aria-label', item.label + ': ' + item.value);
        var sign = document.createElement('span');
        sign.className = 'skill-stat-sign';
        sign.textContent = item.sign;
        var value = document.createElement('span');
        value.textContent = item.value;
        chip.appendChild(sign);
        chip.appendChild(value);
        stats.appendChild(chip);
      });
    });
  }

  /* ---------- обратная связь: модалка ---------- */

  function el(id) { return document.getElementById(id); }

  function requireLogin(hint) {
    /* гостю вместо ошибки — окно входа (тот же механизм, что кнопка
       «Войти» в сайдбаре); после входа aios-auth-change перерисует форму */
    if (hint) {
      var note = el('agfbCommentsLoginNote');
      if (note) note.hidden = false;
    }
    var btnLogin = el('btnLogin');
    if (btnLogin) btnLogin.click();
  }

  function renderStar(data) {
    var btn = el('agfbStarBtn');
    var label = el('agfbStarLabel');
    var count = el('agfbStarCount');
    if (!btn || !label || !count) return;
    count.textContent = data.stars || 0;
    var mine = !!data.starred_by_me;
    btn.classList.toggle('is-starred', mine);
    btn.setAttribute('aria-pressed', mine ? 'true' : 'false');
    label.textContent = mine ? 'Звезда ваша' : 'Поставить звезду';
  }

  function renderProjects(data) {
    var list = el('agfbProjects');
    var empty = el('agfbProjectsEmpty');
    if (!list) return;
    list.innerHTML = '';
    var projects = data.projects || [];
    if (empty) empty.hidden = projects.length > 0;
    projects.forEach(function (p) {
      var li = document.createElement('li');
      li.className = 'agfb-project';
      var head = document.createElement('span');
      head.className = 'agfb-project-name';
      head.textContent = p.title;
      var meta = document.createElement('span');
      meta.className = 'agfb-project-meta';
      var bits = [];
      if (p.runs) {
        bits.push(p.runs + ' ' + plural(p.runs, 'применение', 'применения', 'применений'));
      }
      if (p.last_at) bits.push('последнее ' + fmtDate(p.last_at));
      if (p.status === 'draft') bits.push('черновик');
      meta.textContent = bits.join(' · ');
      li.appendChild(head);
      li.appendChild(meta);
      list.appendChild(li);
    });
  }

  function renderComments(data) {
    var list = el('agfbComments');
    var empty = el('agfbCommentsEmpty');
    var form = el('agfbCommentForm');
    var loginNote = el('agfbCommentsLoginNote');
    if (!list) return;
    list.innerHTML = '';
    var comments = data.comments || [];
    if (empty) empty.hidden = comments.length > 0;
    var authed = !!authSession();
    if (form) form.hidden = !authed;
    if (loginNote) loginNote.hidden = authed;
    comments.forEach(function (c) {
      var li = document.createElement('li');
      li.className = 'agfb-comment';
      var head = document.createElement('div');
      head.className = 'agfb-comment-head';
      var author = document.createElement('span');
      author.className = 'agfb-comment-author';
      author.textContent = c.author || 'Пользователь';
      var date = document.createElement('span');
      date.className = 'agfb-comment-date';
      date.textContent = fmtDate(c.created_at);
      head.appendChild(author);
      head.appendChild(date);
      if (c.mine || data.can_delete_any) {
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'agfb-comment-del';
        del.textContent = 'Удалить';
        del.setAttribute('aria-label', 'Удалить свой комментарий');
        del.addEventListener('click', function () {
          if (!currentAgent || !window.AIOS_API) return;
          del.disabled = true;
          window.AIOS_API.catalogCommentDelete(currentAgent.id, c.id, authToken())
            .then(function () { return loadFeedback(currentAgent); })
            .then(function () { overviewPromise = null; renderCardStatsForCurrent(); })
            .catch(function () { del.disabled = false; });
        });
        head.appendChild(del);
      }
      var text = document.createElement('p');
      text.className = 'agfb-comment-text';
      text.textContent = c.text;
      li.appendChild(head);
      li.appendChild(text);
      list.appendChild(li);
    });
  }

  function renderFeedback(data) {
    currentFeedback = data;
    renderStar(data);
    renderProjects(data);
    renderComments(data);
  }

  function renderCardStatsForCurrent() {
    if (!currentAgent) return;
    var card = document.querySelector('.skill-card[data-agent-id="' + currentAgent.id + '"]');
    if (card) renderCardStats(card, currentAgent);
  }

  function loadFeedback(agent) {
    if (!window.AIOS_API || !window.AIOS_API.catalogAgentFeedback) {
      renderFeedback({ stars: 0, comments: [], projects: [] });
      return Promise.resolve();
    }
    return window.AIOS_API.catalogAgentFeedback(agent.id, authToken())
      .then(renderFeedback)
      .catch(function () {
        renderFeedback({ stars: 0, comments: [], projects: [] });
      });
  }

  function bindFeedbackControls() {
    var starBtn = el('agfbStarBtn');
    if (starBtn) {
      starBtn.addEventListener('click', function () {
        if (!currentAgent) return;
        var token = authToken();
        if (!token) { requireLogin(true); return; }
        starBtn.disabled = true;
        window.AIOS_API.catalogAgentStar(currentAgent.id, token)
          .then(function (res) {
            if (currentFeedback) {
              currentFeedback.stars = res.stars;
              currentFeedback.starred_by_me = !!res.stars_by_me;
            }
            renderStar(currentFeedback || {});
            overviewPromise = null;
            renderCardStatsForCurrent();
          })
          .catch(function () { /* сеть/токен — состояние не меняем */ })
          .then(function () { starBtn.disabled = false; });
      });
    }

    var form = el('agfbCommentForm');
    if (form) {
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        if (!currentAgent) return;
        var token = authToken();
        var textarea = el('agfbCommentText');
        var submit = el('agfbCommentSubmit');
        if (!token) { requireLogin(true); return; }
        var text = (textarea.value || '').trim();
        if (!text) { textarea.focus(); return; }
        if (submit) submit.disabled = true;
        window.AIOS_API.catalogCommentAdd(currentAgent.id, text, token)
          .then(function () {
            textarea.value = '';
            return loadFeedback(currentAgent);
          })
          .then(function () { overviewPromise = null; renderCardStatsForCurrent(); })
          .catch(function () { /* остаёмся с введённым текстом */ })
          .then(function () { if (submit) submit.disabled = false; });
      });
    }

    /* вход/выход перерисовывают доступные действия в открытой модалке */
    window.addEventListener('aios-auth-change', function () {
      if (currentAgent && !modalOverlay.hidden) loadFeedback(currentAgent);
    });
  }

  bindFeedbackControls();

  /* ---------- карточка и модалка агента ---------- */

  function sessionToken() {
    try {
      var s = JSON.parse(localStorage.getItem('aios-auth-v1') || 'null');
      return s && s.token ? s.token : null;
    } catch (e) { return null; }
  }

  /* Право администратора — по ответу сервера, не по снимку роли в
     localStorage (устаревал и прятал кнопки от действующего админа).
     Кэшируем на страницу: роль за жизнь вкладки не меняется. */
  var adminRolePromise = null;

  function isAdminSession() {
    var token = sessionToken();
    if (!token || !window.AIOS_API || typeof window.AIOS_API.request !== 'function') {
      return Promise.resolve(false);
    }
    if (!adminRolePromise) {
      adminRolePromise = window.AIOS_API.request('GET', '/auth/me', undefined, token)
        .then(function (me) { return !!(me && me.role === 'Администратор'); })
        .catch(function () { return false; });
    }
    return adminRolePromise;
  }

  function openAgent(agent) {
    if (!skillModal) return;
    currentAgent = agent;
    document.getElementById('skillModalName').textContent = agent.name;
    document.getElementById('skillModalDomain').textContent = agent.domain || '—';
    document.getElementById('skillModalDesc').textContent = agent.description_ru || '';
    document.getElementById('skillModalAuthor').textContent = agent.author || '—';
    document.getElementById('skillModalModel').textContent = agent.recommended_model || '—';
    renderFeedback({ stars: 0, comments: [], projects: [] }); /* скелет-состояние до ответа */
    var dl = document.getElementById('skillModalDownload');
    if (agent.file_url) {
      dl.href = agent.file_url;
      dl.style.display = '';
    } else {
      dl.style.display = 'none';
    }
    /* «Запустить в рабочем чате»: панель workspace.js уже подключена
       на этой же странице (плавающая панель, есть на любой странице AIOS) —
       поэтому не нужен localStorage-флаг «на следующую загрузку», как было
       раньше (баг: чат не открывался сам, пока страницу не перезагрузят).
       Диспатчим aios-launch-skill — тот же путь, что и кнопка «Запустить
       онбординг проекта» в cabinet.js: чат откроется и стартует сразу. */
    var launch = document.getElementById('skillModalLaunch');
    if (launch) {
      launch.onclick = function () {
        skillModal.close();
        document.dispatchEvent(new CustomEvent('aios-launch-skill', {
          detail: { skill_id: agent.id, name: agent.name || '' }
        }));
      };
    }
    /* «Удалить агента» — модерационное право администратора. Карточка
       каталога = агент: сервер удалит карточку skills.json, .md-файл и
       runtime-экземпляр agt-{id} с данными. Роль спрашиваем у сервера
       (/api/auth/me) — снимок localStorage на момент логина устаревает. */
    var del = document.getElementById('skillModalDeleteAgent');
    if (del) {
      del.style.display = 'none';
      del.disabled = false;
      isAdminSession().then(function (yes) {
        if (!yes || currentAgent !== agent) return; /* модалка уже про другого */
        del.style.display = '';
      });
      del.onclick = function () {
        if (del.disabled) return;
        var name = agent.name || agent.id;
        if (!confirm('Удалить агента «' + name + '» безвозвратно? Уйдут карточка каталога, файл агента и, если есть, его запущенный экземпляр с накопленными данными.')) return;
        del.disabled = true;
        window.AIOS_API.request(
          'DELETE', '/catalog/agents/' + encodeURIComponent(agent.id), undefined, sessionToken()
        ).then(function () {
          skillModal.close();
          var node = document.querySelector('.skill-card[data-agent-id="' + agent.id + '"]');
          if (node) node.remove();
        }).catch(function (err) {
          del.disabled = false;
          alert('Не удалось удалить агента: ' + (err && err.message ? err.message : 'ошибка'));
        });
      };
    }
    skillModal.open();
    loadFeedback(agent);
  }

  function makeAgentCard(agent) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'skill-card';
    card.setAttribute('data-agent-id', agent.id);
    var icon = document.createElement('span');
    icon.className = 'skill-card-icon';
    icon.setAttribute('aria-hidden', 'true');
    /* Сгенерированный логотип агента (artifact_icons: /data/icons/{id}.svg)
       заполняет бейдж; нет файла — остаётся обычный CSS-глиф документа */
    if (agent.image_url) {
      var logo = document.createElement('img');
      logo.src = agent.image_url;
      logo.alt = '';
      logo.loading = 'lazy';
      logo.addEventListener('error', function () {
        icon.classList.remove('skill-card-icon--logo');
        logo.remove();
      });
      logo.addEventListener('load', function () {
        icon.classList.add('skill-card-icon--logo');
      });
      icon.appendChild(logo);
    }
    var name = document.createElement('span');
    name.className = 'skill-card-name';
    name.textContent = agent.name;
    var domain = document.createElement('span');
    domain.className = 'skill-card-domain';
    domain.textContent = agent.domain || '';
    var stats = document.createElement('span');
    stats.className = 'skill-card-stats';
    card.appendChild(icon);
    card.appendChild(name);
    card.appendChild(domain);
    card.appendChild(stats);
    renderCardStats(card, agent);
    card.addEventListener('click', function () { openAgent(agent); });
    return card;
  }

  /* ---------- загрузка каталога и разделы ---------- */

  var catalogPromise = null;

  function catalog() {
    if (!catalogPromise) {
      catalogPromise = fetchJSON('/data/skills.json')
        .then(function (data) { return (data && data.items) || []; })
        .catch(function () {
          catalogPromise = null;
          throw new Error('catalog load failed');
        });
    }
    return catalogPromise;
  }

  function loadSection(section) {
    var grid = document.getElementById(section.grid);
    if (!grid) return;
    showSkeletons(grid, 3);
    /* роль — у сервера: admin-only агенты (сейчас «Конструктор моделей»)
       видны в каталоге только администраторам */
    Promise.all([catalog(), isAdminSession()]).then(function (res) {
      var admin = res[1];
      var agents = res[0].filter(function (a) {
        return a.category === section.category && (!a.admin_only || admin);
      });
      if (!agents.length) {
        showEmpty(grid, 'В этом разделе пока нет агентов — скоро здесь появятся карточки.');
        return;
      }
      grid.innerHTML = '';
      agents.forEach(function (agent) {
        grid.appendChild(makeAgentCard(agent));
      });
    }).catch(function () {
      showError(grid, function () { loadSection(section); });
    });
  }

  var loaded = {};

  function toggleSpoiler(spoiler) {
    var btn = spoiler.querySelector('.spoiler-toggle');
    var panel = spoiler.querySelector('.spoiler-body');
    if (!btn || !panel) return;
    var open = panel.hidden;
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    spoiler.classList.toggle('spoiler--open', open);
    if (open && !loaded[spoiler.id]) {
      loaded[spoiler.id] = true;
      var section = SECTIONS.find(function (s) { return s.spoiler === spoiler.id; });
      if (section) loadSection(section);
    }
  }

  document.querySelectorAll('.spoiler').forEach(function (spoiler) {
    var btn = spoiler.querySelector('.spoiler-toggle');
    if (btn) {
      btn.addEventListener('click', function () { toggleSpoiler(spoiler); });
    }
  });

})();
