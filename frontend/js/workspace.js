/* ============================================================
   Рабочий чат: клиент AIOS Harness. Два вида интерфейса на одном
   коде: отдельная страница chat.html (разметку инъецируем в
   #wsPageHost) и панель, наползающая справа, — на всех остальных
   страницах сайта (разметку инъецируем в body). Пункт «Рабочий
   чат» левого меню (#btnNavWorkspace) и сессии под ним открывают
   страницу; панель-поповер оставлен остальным вызовам — золотая
   плашка на главной (#btnWorkspace), язычок у края экрана, бейдж
   «Ответ готов», запуск навыка и клик по сессии в «Кабинете»
   (событие aios-open-session). Под пунктом «Рабочий чат» меню —
   разворачивающийся список сессий (названия те же, что в
   «Истории сессий» кабинета: GET /api/cabinet/sessions); длинные
   названия обрезаются, видна та часть, что влезает в меню.
   Раздел «Рабочее пространство» меню (чат, «Кабинет»,
   «Проекты») виден только при авторизации
   (auth.js диспатчит событие 'aios-auth-change').

   Диалог идёт через агентный рантайм (POST /api/chat, SSE):
   текст ассистента печатается потоково, вызовы инструментов
   показываются карточками, транскрипт восстанавливается из
   сессии при повторном открытии панели. Модель задаёт
   администратор централизованно (админ-панель →
   «Настройки внешних API»); текущий статус — GET /api/runtime.
   Кнопка «+» у поля ввода открывает меню из двух разделов: «Агенты»
   (каталог навыков /data/skills.json — в интерфейсе навыки называются
   агентами; запуск = новая сессия, skill_ids, автозапуск пайплайна)
   и «RAG-базы» (одна подключённая база, rag_ids). База подключается и
   фразой в чате — «подключи базу …» перехватывается при отправке
   (applyRagIntent), без нажатий в меню. Прикрепление
   файлов — отдельная кнопка 📎.
   ============================================================ */

(function () {
  var AUTH_KEY = 'aios-auth-v1';
  var SESSION_KEY = 'aios-workspace-session-v1';
  var SKILL_KEY = 'aios-workspace-skill-v1'; /* «агент» (раньше — навык), запущенный в чате; ставит и agents.js */
  var RAG_KEY = 'aios-workspace-rag-v1'; /* RAG-база, подключённая разделом «RAG-базы» меню кнопки «+» */

  var MIN_WIDTH = 400;
  var DEFAULT_WIDTH = 560;
  var ANIM_MS = 320;

  /* ---------- триггеры ---------- */

  var PAGE_HOST = document.getElementById('wsPageHost'); /* chat.html */
  var PAGE_MODE = !!PAGE_HOST;
  var entryBtn = document.getElementById('btnWorkspace');
  var navBtn = document.getElementById('btnNavWorkspace');
  var wsGroup = document.getElementById('wsGroup');
  var menuCabinet = document.getElementById('menuCabinet');
  var menuProjects = document.getElementById('menuProjects');
  var triggers = [];
  if (entryBtn) triggers.push(entryBtn); /* плашка на главной — поповер */
  if (!triggers.length && !navBtn && !PAGE_HOST) return;

  /* ---------- разметка панели: инъекция в DOM ---------- */

  var PANEL_HTML = [
    '<div class="ws-overlay" id="wsOverlay" hidden></div>',
    '<button class="ws-edge-tab" id="wsEdgeTab" type="button" aria-label="Открыть рабочий чат" hidden><svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2C6.48 2 2 5.94 2 10.8c0 2.72 1.47 5.14 3.79 6.76-.13.9-.5 2.1-1.44 3.44a.5.5 0 0 0 .58.77c1.94-.63 3.4-1.53 4.32-2.19.86.2 1.77.31 2.75.31 5.52 0 10-3.94 10-8.8S17.52 2 12 2Z"></path><circle cx="8.3" cy="10.8" r="1.15" fill="var(--green-deep, #2c3128)"></circle><circle cx="12" cy="10.8" r="1.15" fill="var(--green-deep, #2c3128)"></circle><circle cx="15.7" cy="10.8" r="1.15" fill="var(--green-deep, #2c3128)"></circle></svg></button>',
    '<button class="ws-ready-badge" id="wsReadyBadge" type="button" hidden aria-live="polite">✦ Ответ готов</button>',
    '<button class="ws-reminder-badge" id="wsReminderBadge" type="button" hidden aria-live="polite"></button>',
    '<div class="ws-panel" id="wsPanel" role="dialog" aria-modal="true" aria-labelledby="wsTitle" hidden>',
    '  <div class="ws-grip" id="wsGrip" title="Потяните, чтобы изменить ширину" aria-hidden="true"></div>',
    '  <aside class="ws-side" id="wsSide" aria-label="История сессий">',
    '    <button class="ws-side-close" id="wsSideClose" type="button" title="Свернуть историю" aria-label="Свернуть историю"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="4"></rect><path d="M9 4v16"></path><path d="M15 9l-3 3 3 3"></path></svg></button>',
    '    <div class="ws-side-logo"><span class="ws-side-sigil"></span><span class="ws-side-logo-text">AI OS</span></div>',
    '    <button class="ws-side-new" id="wsSideNew" type="button" title="Новый чат"><span class="ws-side-new-plus">+</span><span class="ws-side-new-text">Новый чат</span></button>',
    '    <button class="ws-side-search" id="wsSideSearch" type="button" title="Поиск по чатам" aria-label="Поиск по чатам"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path></svg></button>',
    '    <button class="ws-side-toggle" id="wsSideToggle" type="button" aria-label="Свернуть сайдбар"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="4"></rect><path d="M9 4v16"></path><path d="M15 9l-3 3 3 3"></path></svg></button>',
    '    <button class="ws-side-hist" id="wsSideHist" type="button" title="История сессий" aria-expanded="false"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg></button>',
    '    <div class="ws-side-list" id="wsSideList"></div>',
    '    <div class="ws-side-user" id="wsSideUser" hidden><span class="ws-side-avatar" id="wsSideAvatar"></span><span class="ws-side-username" id="wsSideUsername"></span></div>',
    '  </aside>',
    '  <header class="ws-head">',
    '    <div class="ws-head-title">',
    '      <h2 id="wsTitle">Рабочий чат</h2>',
    '      <span class="ws-model-label" id="wsModelLabel">LLM не выбрана</span>',
    '    </div>',
    '    <div class="ws-head-actions">',
    '      <span class="ws-live-dot" id="wsLiveDot" hidden title="Модель отвечает…"></span>',
    '      <button class="ws-btn" id="wsNewBtn" type="button" aria-label="Новый диалог" data-tooltip="Новый диалог"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.5 8.5 0 0 1-8.9 8.49 9.3 9.3 0 0 1-3.61-.72L3 21l1.73-5.49A8.5 8.5 0 1 1 21 11.5Z"></path><line x1="12" y1="8" x2="12" y2="14"></line><line x1="9" y1="11" x2="15" y2="11"></line></svg></button>',
    '      <button class="ws-btn" id="wsExpandBtn" type="button" aria-label="Во весь экран" data-tooltip="Во весь экран">⛶</button>',
    '      <button class="ws-btn" id="wsCloseBtn" type="button" aria-label="Закрыть" data-tooltip="Закрыть панель">✕</button>',
    '    </div>',
    '  </header>',
    '  <div class="ws-messages" id="wsMessages" aria-live="polite"></div>',
    '  <form class="ws-composer" id="wsComposer" onsubmit="return false">',
    '    <div class="ws-suggest" id="wsSuggest" hidden></div>',
    '    <div class="ws-composer-row">',
    '    <div class="ws-skillpick" id="wsCtxPick">',
    '      <button class="ws-skillpick-btn" id="wsCtxBtn" type="button" aria-haspopup="menu" aria-expanded="false" aria-label="Агенты и RAG-базы" data-tooltip="Агенты и RAG-базы"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><line x1="12" y1="8" x2="12" y2="16"></line><line x1="8" y1="12" x2="16" y2="12"></line></svg></button>',
    '      <div class="ws-skillpick-menu" id="wsCtxMenu" role="menu" aria-label="Агенты и RAG-базы" hidden></div>',
    '    </div>',
    '    <button class="ws-skillpick-btn" id="wsAttachBtn" type="button" aria-label="Прикрепить файл" data-tooltip="Прикрепить файл (.pdf, .txt, .md, .docx)"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg></button>',
    '    <input type="file" id="wsFileInput" multiple accept=".pdf,.txt,.md,.docx" hidden>',
    '    <textarea id="wsInput" rows="1" placeholder="Сообщение рабочему чату…" aria-label="Сообщение"></textarea>',
    '    <button class="ws-send" type="submit" aria-label="Отправить"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg></button>',
    '    </div>',
    '    <div class="ws-attach-strip" id="wsAttachStrip" hidden></div>',
    '  </form>',
    '</div>'
  ].join('\n');

  if (PAGE_HOST) PAGE_HOST.insertAdjacentHTML('beforeend', PANEL_HTML);
  else document.body.insertAdjacentHTML('beforeend', PANEL_HTML);

  /* «язычок» у правого края экрана — всегда виден (пока доступен рабочий
     чат), тянет открыть панель, даже когда её ещё не открывали ни разу.
     Видимость по авторизации — вместе с остальными triggers (syncEntry).
     На странице чата язычок не нужен — чат уже перед глазами. */
  var edgeTab = document.getElementById('wsEdgeTab');
  if (edgeTab && !PAGE_MODE) triggers.push(edgeTab);

  /* Пункт «Рабочий чат» левого меню открывает страницу чата, а не
     поповер. На самой странице кнопка лишь подсвечивает текущий
     раздел (класс active в chat.html) — перезагружать диалог не надо. */
  if (navBtn) {
    navBtn.addEventListener('click', function () {
      if (PAGE_MODE) return;
      location.assign('chat.html');
    });
  }

  /* ---------- спойлер сессий в левом меню ---------- */

  /* Под кнопкой «Рабочий чат» — сворачивающийся список сессий
     пользователя: названия те же, что в «Истории сессий» кабинета
     (GET /api/cabinet/sessions). Клик по сессии открывает страницу
     чата с ней (?session=…). Длинное название обрезается CSS-ом —
     видна та часть, что влезает в меню; полное — в подсказке. */
  var NAV_SESSIONS_OPEN_KEY = 'aios-ws-nav-sessions-open-v1';
  var navSessionsBox = null;
  var navSessionsToggle = null;
  var navSessionsList = null;
  var navSessions; /* undefined — ещё грузим, null — ошибка, [] — пусто */

  if (navBtn) {
    navBtn.insertAdjacentHTML('afterend', [
      '<div class="ws-nav-sessions" id="wsNavSessions" hidden>',
      '  <button class="ws-nav-sessions-toggle" id="wsNavSessionsToggle" type="button" aria-expanded="false" aria-controls="wsNavSessionsList">',
      '    <span class="ws-nav-sessions-ico" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 6h11M8.5 12h11M8.5 18h11"/><path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01"/></svg></span>Сессии',
      '    <span class="ws-nav-sessions-caret" aria-hidden="true">&#9656;</span>',
      '  </button>',
      '  <div class="ws-nav-sessions-list" id="wsNavSessionsList" hidden></div>',
      '</div>'
    ].join('\n'));
    navSessionsBox = document.getElementById('wsNavSessions');
    navSessionsToggle = document.getElementById('wsNavSessionsToggle');
    navSessionsList = document.getElementById('wsNavSessionsList');
  }

  function navSessionsOpen() {
    try {
      return localStorage.getItem(NAV_SESSIONS_OPEN_KEY) === '1';
    } catch (e) {
      return false;
    }
  }

  function applyNavSessionsOpen(open) {
    if (!navSessionsToggle || !navSessionsList) return;
    navSessionsToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    navSessionsList.hidden = !open;
  }

  function navSessionsNote(text) {
    navSessionsList.innerHTML = '';
    var note = document.createElement('span');
    note.className = 'ws-nav-sessions-note';
    note.textContent = text;
    navSessionsList.appendChild(note);
  }

  /* Читаемая метка сессии для узкой колонки меню. Автозаголовки запуска
     навыка («Запусти навык «X»: работай строго по его алгоритму…»)
     сжимаем до имени навыка — иначе у половины сессий строки начинаются
     одинаково и их не отличить; полное название остаётся в подсказке. */
  function navSessionLabel(title) {
    var raw = String(title || '').trim();
    if (!raw) return 'Без названия';
    var m = raw.match(/^Запусти(?:ть)?\s+навык\s+«([^»]{1,60})»/);
    if (m) return m[1].trim();
    m = raw.match(/^Запусти(?:ть)?\s+навык\s+([^:«»]{2,60}?)(?:\s*:|$)/);
    if (m) return m[1].trim();
    return raw;
  }

  /* Дата сессии: «24.09 13:40», в прошлом году — с годом */
  function navSessionDate(ts) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '';
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    var now = new Date();
    var head = p(d.getDate()) + '.' + p(d.getMonth() + 1);
    if (d.getFullYear() !== now.getFullYear()) return head + '.' + d.getFullYear();
    return head + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function renderNavSessions() {
    if (!navSessionsList) return;
    if (navSessions === undefined) { navSessionsNote('Загрузка…'); return; }
    if (navSessions === null) { navSessionsNote('Не удалось загрузить список'); return; }
    if (!navSessions.length) { navSessionsNote('Сессий пока нет'); return; }
    navSessionsList.innerHTML = '';
    var activeId = getSessionId();
    var lastGroup = -1;
    navSessions.forEach(function (s) {
      var gi = sideGroupOf(s.updated_at);
      if (gi !== lastGroup) {
        lastGroup = gi;
        var gh = document.createElement('div');
        gh.className = 'ws-nav-sessions-group';
        gh.textContent = SIDE_GROUPS[gi];
        navSessionsList.appendChild(gh);
      }
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ws-nav-sessions-item' +
        (s.id && s.id === activeId ? ' ws-nav-sessions-item--active' : '');
      btn.title = s.title || 'Без названия'; /* полное название — в подсказке */
      var name = document.createElement('span');
      name.className = 'ws-nav-sessions-name';
      name.textContent = navSessionLabel(s.title);
      var meta = [];
      var date = navSessionDate(s.updated_at);
      if (date) meta.push(date);
      if (s.message_count) meta.push(s.message_count + ' сообщ.');
      var metaEl = document.createElement('span');
      metaEl.className = 'ws-nav-sessions-meta';
      metaEl.textContent = meta.join(' · ');
      btn.appendChild(name);
      btn.appendChild(metaEl);
      btn.addEventListener('click', function () {
        location.assign('chat.html?session=' + encodeURIComponent(s.id));
      });
      navSessionsList.appendChild(btn);
    });
  }

  function loadNavSessions() {
    var session = getAuthSession();
    if (!session || !session.token || !window.AIOS_API || !window.AIOS_API.cabinetListSessions) return;
    if (navSessions === undefined) renderNavSessions(); /* «Загрузка…» */
    window.AIOS_API.cabinetListSessions(session.token)
      .then(function (data) {
        navSessions = ((data && data.items) || []).slice().sort(function (x, y) {
          return Number(y.updated_at) - Number(x.updated_at);
        });
        renderNavSessions();
      })
      .catch(function () {
        navSessions = null;
        renderNavSessions();
      });
  }

  if (navSessionsToggle) {
    navSessionsToggle.addEventListener('click', function () {
      var open = navSessionsToggle.getAttribute('aria-expanded') !== 'true';
      try {
        if (open) localStorage.setItem(NAV_SESSIONS_OPEN_KEY, '1');
        else localStorage.removeItem(NAV_SESSIONS_OPEN_KEY);
      } catch (e) { /* приватный режим */ }
      applyNavSessionsOpen(open);
      if (open && navSessions === undefined) loadNavSessions();
    });
    applyNavSessionsOpen(navSessionsOpen());
  }

  /* ---------- элементы панели ---------- */

  var overlay = document.getElementById('wsOverlay');
  var panel = document.getElementById('wsPanel');
  var grip = document.getElementById('wsGrip');
  var newBtn = document.getElementById('wsNewBtn');
  var expandBtn = document.getElementById('wsExpandBtn');
  var closeBtn = document.getElementById('wsCloseBtn');
  var modelLabel = document.getElementById('wsModelLabel');
  var messagesEl = document.getElementById('wsMessages');
  var composer = document.getElementById('wsComposer');
  var sendBtn = composer.querySelector('.ws-send');
  var ctxPick = document.getElementById('wsCtxPick');
  var ctxBtn = document.getElementById('wsCtxBtn');
  var ctxMenu = document.getElementById('wsCtxMenu');
  var input = document.getElementById('wsInput');
  var attachBtn = document.getElementById('wsAttachBtn');
  var fileInput = document.getElementById('wsFileInput');
  var attachStrip = document.getElementById('wsAttachStrip');
  var suggestEl = document.getElementById('wsSuggest');
  var liveDot = document.getElementById('wsLiveDot');
  var readyBadge = document.getElementById('wsReadyBadge');
  var reminderBadge = document.getElementById('wsReminderBadge');

  /* ---------- утилиты ---------- */

  function isAuthed() {
    try {
      return !!localStorage.getItem(AUTH_KEY);
    } catch (e) {
      return false;
    }
  }

  function getSessionId() {
    try {
      return localStorage.getItem(SESSION_KEY) || null;
    } catch (e) {
      return null;
    }
  }

  function saveSessionId(id) {
    try {
      if (id) localStorage.setItem(SESSION_KEY, id);
      else localStorage.removeItem(SESSION_KEY);
    } catch (e) { /* приватный режим */ }
  }

  /* ---------- активный навык (кнопка «Запустить в рабочем чате») ---------- */

  function readSkill() {
    try {
      var raw = localStorage.getItem(SKILL_KEY);
      var s = raw ? JSON.parse(raw) : null;
      return s && s.skill_id ? s : null;
    } catch (e) {
      return null;
    }
  }

  function saveSkill(skill, open) {
    try {
      localStorage.setItem(SKILL_KEY, JSON.stringify({
        skill_id: skill.skill_id,
        name: skill.name || '',
        prompt: skill.prompt || '', /* автозапуск: первое сообщение навыка */
        open: !!open
      }));
    } catch (e) { /* приватный режим */ }
  }

  function clearSkill() {
    try { localStorage.removeItem(SKILL_KEY); } catch (e) { /* приватный режим */ }
  }

  /* ---------- подключённая RAG-база (раздел «RAG-базы» меню кнопки «+») ---------- */

  function readRags() {
    try {
      var raw = localStorage.getItem(RAG_KEY);
      var list = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(list)) return [];
      /* в хранилище могла остаться тройка баз от старой версии кнопки —
         выживает первая подключённая */
      return list.filter(function (r) { return r && r.rag_id; }).slice(0, 1);
    } catch (e) {
      return [];
    }
  }

  function saveRags(list) {
    try { localStorage.setItem(RAG_KEY, JSON.stringify(list)); } catch (e) { /* приватный режим */ }
  }

  /* база всегда одна: выбор новой заменяет предыдущую, клик по подключённой
     отключает (синхронизировано с ChatRequest.rag_ids max_length=1 на сервере) */
  function toggleRag(rag) {
    var list = readRags();
    if (list.length && list[0].rag_id === rag.id) {
      saveRags([]);
    } else {
      saveRags([{ rag_id: rag.id, name: rag.name || rag.id }]);
    }
  }

  /* Запущенный агент (навык) и подключённая RAG-база видны в заголовке
     панели: «Рабочий чат: <агент> · база: <имя>». Оба уезжают на сервер
     с каждым сообщением (skill_ids и rag_ids), поэтому переживают новый
     диалог. */
  function syncSkillbar() {
    var title = document.getElementById('wsTitle');
    var skill = readSkill();
    var rags = readRags();
    var parts = [];
    if (skill) parts.push(skill.name || skill.skill_id);
    if (rags.length) parts.push('база: ' + (rags[0].name || rags[0].rag_id));
    if (title) title.textContent = parts.length ? 'Рабочий чат: ' + parts.join(' · ') : 'Рабочий чат';
    /* пока в чате запущен агент, раздел «Агенты» меню закрыт для выбора
       (сменить агента можно только новым диалогом); RAG-базы остаются
       доступны при любом режиме */
    if (skill) closeCtxMenu();
    if (ctxBtn) ctxBtn.classList.toggle('ws-skillpick-btn--active', !!(skill || rags.length));
  }

  /* ---------- каталог навыков и автозапуск ---------- */

  /* Каталог навыков (/data/skills.json) нужен, чтобы распознать запрос
     пользователя «запустить навык …» (в т.ч. клик по варианту из
     ```aios-choices) и запустить навык автоматически: новая сессия,
     навык подключён, пайплайн идёт с первого шага. Никаких подтверждений
     администратора для этого не требуется. */
  var SKILL_CATALOG = null;
  var SKILL_CATALOGPromise = null;

  function skillsCatalog() {
    if (SKILL_CATALOG) return Promise.resolve(SKILL_CATALOG);
    if (!SKILL_CATALOGPromise) {
      SKILL_CATALOGPromise = fetch('/data/skills.json')
        .then(function (r) { return r.json(); })
        .then(function (d) {
          SKILL_CATALOG = (d && d.items) || [];
          return SKILL_CATALOG;
        })
        .catch(function () { SKILL_CATALOG = []; return SKILL_CATALOG; });
    }
    return SKILL_CATALOGPromise;
  }

  /* Ищет в тексте запрос упоминание навыка из каталога. Возвращает навык
     или null. Достаточно подстроки: варианты из aios-choices пишут
     «Вызвать навык "Формулировка гипотез"», пользователь — «запусти
     онбординг» (достаточно значимого слова названия, от 6 символов). */
  function findSkillMention(text, items) {
    var t = String(text || '').toLowerCase();
    if (!t) return null;
    /* первый проход: точное совпадение полного названия навыка —
       приоритет важен, когда у нескольких навыков совпадает первое слово
       (например, «Конструктор онтологий» и «Конструктор RAG»). */
    for (var i = 0; i < items.length; i++) {
      var name = String(items[i].name || '').toLowerCase().trim();
      if (name && t.indexOf(name) !== -1) return items[i];
    }
    /* второй проход: запасной вариант — совпадение по первому слову
       названия, только если полного совпадения не нашлось ни у кого. */
    for (var j = 0; j < items.length; j++) {
      var name2 = String(items[j].name || '').toLowerCase().trim();
      var key = (name2.split(/\s+/)[0] || '').replace(/[^\p{L}\p{N}-]/gu, '');
      if (key.length >= 6 && t.indexOf(key) !== -1) return items[j];
    }
    return null;
  }

  var LAUNCH_VERBS_RE = /(запуст|подключ|включ|откр|вызов|запуск|начни|начать|перехо|перекл)/i;

  /* Запрос на запуск навыка? — упоминание навыка из каталога + глагол
     запуска (клик по варианту может быть без глагола, если вариант —
     чистое название навыка). */
  function findLaunchIntent(text, items) {
    var t = String(text || '');
    var low = t.toLowerCase();
    var mentioned = findSkillMention(t, items);
    if (!mentioned) return null;
    var active = readSkill();
    if (active && active.skill_id === mentioned.id) return null; /* навык уже активен */
    if (low.indexOf(mentioned.name.toLowerCase()) !== -1 && t.trim().toLowerCase() === mentioned.name.toLowerCase()) {
      return mentioned; /* вариант-кнопка с точным названием навыка */
    }
    return LAUNCH_VERBS_RE.test(t) ? mentioned : null;
  }

  /* Запуск навыка «любым способом»: всегда новая сессия, навык
     подключается, пайплайн стартует первым сообщением (автозапуск). */
  function launchSkill(skill, prompt) {
    var autoPrompt = prompt ||
      'Запусти навык «' + (skill.name || skill.id) + '»: работай строго по его алгоритму, начиная с первого шага.';
    hideSuggestions();
    sessionStage = null;
    saveSkill({ skill_id: skill.id, name: skill.name || '', prompt: autoPrompt }, false);
    saveSessionId(null); /* навык всегда начинает новую сессию */
    clearTranscript();
    syncSkillbar();
    openPanel(null);
    /* ждём окончания текущей отправки, если такая идёт (retry);
       стартуем после анимации открытия панели */
    setTimeout(function autoStart() {
      if (sending) { setTimeout(autoStart, 300); return; }
      send(autoPrompt);
    }, ANIM_MS);
  }

  /* ---------- автоподключение RAG-базы из чата («подключи базу …») ---------- */

  /* Запрос «подключи базу PROTAC» не отправляет пользователя в меню «+»:
     база из каталога (/data/rag_bases.json) подключается к этому же
     сообщению — rag_ids уходят на сервер вместе с текстом, заголовок
     панели обновляется, модель отвечает уже с подключённой базой в
     контексте. «Отключи базу …» работает так же. */
  var RAG_CONNECT_VERBS_RE = /(подключ|подсоедин|включ|активир|запуст|откр|перекл|вызов|использ)/i;
  var RAG_DISCONNECT_RE = /(отключ|выключ)/i;
  /* вопросы («какую базу подключить?») и фразы про навыков/агентов не
     перехватываются — на них отвечает модель */
  var RAG_INTENT_SKIP_RE = /(какую|какая|какие|каких|как подключить|можно ли|сколько|посоветуй|порекомендуй|сравни|чем отлич|навык|агент|онтолог|пайплайн|skill)/i;

  /* Ищет в тексте упоминание RAG-базы из каталога. Сначала — точное
     вхождение полного названия или id; иначе — совпадение по значимым
     словам карточки (название, домен, теги, описание): «подключи базу
     PROTAC» находит «База знаний PROTAC». Слова, встречающиеся во многих
     карточках («база», «знаний», «система»), весят меньше (1/df) — иначе
     «подключи базу» цепляло бы первую попавшуюся карточку. */
  function findRagMention(text, items) {
    var t = String(text || '').toLowerCase();
    if (!t) return null;
    for (var i = 0; i < items.length; i++) {
      var name = String(items[i].name || '').toLowerCase().trim();
      if (name && t.indexOf(name) !== -1) return items[i];
      var rid = String(items[i].id || '').toLowerCase();
      if (rid && t.indexOf(rid) !== -1) return items[i];
    }
    var df = {};
    var tokenSets = items.map(function (rag) {
      var seen = {};
      var blob = [rag.name, rag.domain, rag.description_ru, (rag.tags || []).join(' '), rag.id].join(' ');
      String(blob).toLowerCase().split(/[^a-zа-яё0-9]+/).forEach(function (w) {
        if (w.length >= 5 && !seen[w]) { seen[w] = 1; df[w] = (df[w] || 0) + 1; }
      });
      return Object.keys(seen);
    });
    var best = null;
    var bestScore = 0;
    items.forEach(function (rag, idx) {
      var score = 0;
      tokenSets[idx].forEach(function (w) {
        if (t.indexOf(w) !== -1) score += 1 / df[w];
      });
      if (score >= 0.5 && score > bestScore) { best = rag; bestScore = score; }
    });
    return best;
  }

  /* Применяет команду подключения/отключения базы из текста сообщения,
     если она там есть. Возвращает true при изменении состояния; сообщение
     в любом случае уходит в диалог — модель ответит с учётом нового
     состояния (блок подключённой базы — в системном промпте сервером). */
  function applyRagIntent(text, items) {
    if (!items || !items.length) return false;
    var t = String(text || '');
    if (!t || RAG_INTENT_SKIP_RE.test(t)) return false;
    var rag = findRagMention(t, items);
    if (!rag) return false;
    var connected = readRags();
    var isOn = !!connected.length && connected[0].rag_id === rag.id;
    if (RAG_DISCONNECT_RE.test(t)) {
      if (!isOn) return false;
      saveRags([]);
    } else {
      if (isOn || !RAG_CONNECT_VERBS_RE.test(t)) return false;
      saveRags([{ rag_id: rag.id, name: rag.name || rag.id }]);
    }
    syncSkillbar();
    return true;
  }

  /* ---------- подсказки навыков при вводе (фразы-триггеры) ---------- */

  /* Пока пользователь печатает сообщение без активного навыка, показываем
     чипы навыков, чьи триггер-фразы встретились в тексте (бэкенд матчит
     по YAML-frontmatter навыка, GET /api/chat/skills-suggest). Предложение
     не навязчивое: клик по чипу запускает навык (новая сессия), игнорирование
     просто оставляет чип до изменения текста. */
  var suggestTimer = null;
  var suggestSeq = 0;

  function hideSuggestions() {
    if (!suggestEl) return;
    suggestEl.hidden = true;
    suggestEl.innerHTML = '';
  }

  function refreshSuggestions() {
    if (!suggestEl || !window.AIOS_API || !window.AIOS_API.skillsSuggest) return;
    var text = (input.value || '').trim();
    if (text.length < 8 || readSkill() || sending) {
      hideSuggestions();
      return;
    }
    var seq = ++suggestSeq;
    window.AIOS_API.skillsSuggest(text, []).then(function (d) {
      if (seq !== suggestSeq) return; /* пришёл более свежий ввод */
      var items = (d && d.items) || [];
      if (!items.length) {
        hideSuggestions();
        return;
      }
      suggestEl.innerHTML = '';
      items.slice(0, 3).forEach(function (item) {
        var chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'ws-suggest-chip';
        chip.title = item.description_ru || '';
        chip.textContent = '⚡ ' + (item.name || item.id);
        chip.addEventListener('click', function () {
          hideSuggestions();
          launchSkill({ id: item.id, name: item.name }, '');
        });
        suggestEl.appendChild(chip);
      });
      suggestEl.hidden = false;
    }).catch(function () {
      if (seq === suggestSeq) hideSuggestions();
    });
  }

  input.addEventListener('input', function () {
    if (suggestTimer) clearTimeout(suggestTimer);
    suggestTimer = setTimeout(refreshSuggestions, 1200);
  });

  /* ---------- меню кнопки «+»: разделы «Агенты» и «RAG-базы» ---------- */

  /* Одна кнопка слева от поля ввода открывает меню из двух разделов.
     «Агенты» — каталог навыков (/data/skills.json): то, что раньше
     называлось навыками, в интерфейсе теперь называется агентами.
     Выбор запускает агента: новая сессия, skill_ids, автозапуск
     пайплайна с первого шага. «RAG-базы» — опубликованные базы,
     подключается ровно одна. Прикрепление файлов — отдельная кнопка 📎,
     к этому меню отношения не имеет. */
  function closeCtxMenu() {
    if (!ctxMenu) return;
    ctxMenu.hidden = true;
    ctxBtn.setAttribute('aria-expanded', 'false');
  }

  function menuHead(text) {
    var head = document.createElement('div');
    head.className = 'ws-skillpick-head';
    head.textContent = text;
    return head;
  }

  function menuNote(text) {
    var note = document.createElement('div');
    note.className = 'ws-skillpick-empty';
    note.textContent = text;
    return note;
  }

  function renderCtxMenu(agents, rags) {
    ctxMenu.innerHTML = '';
    /* --- Агенты --- */
    ctxMenu.appendChild(menuHead('Агенты'));
    if (readSkill()) {
      ctxMenu.appendChild(menuNote('Агент уже запущен — начните новый диалог, чтобы выбрать другого'));
    } else if (!agents.length) {
      ctxMenu.appendChild(menuNote('Агентов пока нет'));
    } else {
      agents.forEach(function (agent) {
        var opt = document.createElement('button');
        opt.type = 'button';
        opt.className = 'ws-skillpick-item';
        opt.setAttribute('role', 'menuitem');
        opt.textContent = agent.name || agent.id;
        opt.title = agent.description_ru || agent.description || '';
        opt.addEventListener('click', function () {
          closeCtxMenu();
          launchSkill(agent, '');
        });
        ctxMenu.appendChild(opt);
      });
    }
    /* --- RAG-базы --- */
    ctxMenu.appendChild(menuHead('RAG-базы'));
    if (!rags.length) {
      ctxMenu.appendChild(menuNote('Опубликованных RAG-баз пока нет'));
      return;
    }
    var connected = readRags();
    rags.forEach(function (rag) {
      var isOn = !!connected.length && connected[0].rag_id === rag.id;
      var opt = document.createElement('button');
      opt.type = 'button';
      opt.className = 'ws-skillpick-item' + (isOn ? ' ws-skillpick-item--on' : '');
      opt.setAttribute('role', 'menuitem');
      opt.setAttribute('aria-selected', isOn ? 'true' : 'false');
      opt.textContent = (isOn ? '✓ ' : '') + (rag.name || rag.id);
      opt.title = rag.description_ru || '';
      opt.addEventListener('click', function () {
        toggleRag(rag);
        syncSkillbar();
        renderCtxMenu(agents, rags);
      });
      ctxMenu.appendChild(opt);
    });
  }

  if (ctxBtn && ctxMenu) {
    ctxBtn.addEventListener('click', function () {
      if (!ctxMenu.hidden) { closeCtxMenu(); return; }
      ctxMenu.hidden = false;
      ctxBtn.setAttribute('aria-expanded', 'true');
      ctxMenu.innerHTML = '';
      ctxMenu.appendChild(menuNote('Загрузка…'));
      Promise.all([skillsCatalog(), ragCatalog()]).then(function (both) {
        if (ctxMenu.hidden) return; /* меню закрыли, пока грузились каталоги */
        renderCtxMenu(both[0], both[1]);
      });
    });

    /* закрытие: клик мимо, Escape в поле ввода, сворачивание панели */
    document.addEventListener('click', function (e) {
      if (ctxMenu.hidden) return;
      if (ctxPick.contains(e.target)) return;
      closeCtxMenu();
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !ctxMenu.hidden) {
        e.stopPropagation();
        closeCtxMenu();
      }
    });
  }

  /* ---------- каталог RAG-баз для раздела меню «RAG-базы» ---------- */

  var RAG_CATALOG = null;
  var RAG_CATALOGPromise = null;

  function ragCatalog() {
    if (RAG_CATALOG) return Promise.resolve(RAG_CATALOG);
    if (!RAG_CATALOGPromise) {
      RAG_CATALOGPromise = fetch('/data/rag_bases.json')
        .then(function (r) { return r.json(); })
        .then(function (d) {
          RAG_CATALOG = (d && d.items) || [];
          return RAG_CATALOG;
        })
        .catch(function () { RAG_CATALOG = []; return RAG_CATALOG; });
    }
    return RAG_CATALOGPromise;
  }

  /* ---------- видимость триггеров ---------- */

  function syncEntry() {
    var show = isAuthed();
    triggers.forEach(function (btn) { btn.hidden = !show; });
    if (navBtn) navBtn.hidden = !show;
    /* раздел «Рабочее пространство» меню (чат, «Кабинет»,
       «Проекты») — целиком только вошедшим */
    if (wsGroup) wsGroup.hidden = !show;
    if (menuCabinet) menuCabinet.hidden = !show;
    if (menuProjects) menuProjects.hidden = !show;
    var automationsOn = !!(window.AIOS_FEATURES && window.AIOS_FEATURES.automations);
    var menuAutomations = document.getElementById('menuAutomations');
    if (menuAutomations) menuAutomations.hidden = !show || !automationsOn;
    var tileAutomations = document.getElementById('tileAutomations');
    if (tileAutomations) tileAutomations.hidden = !show || !automationsOn;
    /* спойлер сессий — рядом с кнопкой чата, тоже только вошедшим */
    if (navSessionsBox) navSessionsBox.hidden = !show;
    if (show) {
      if (navSessions === undefined) loadNavSessions();
    } else {
      navSessions = undefined; /* после выхода список протух */
    }
  }

  syncEntry();
  window.addEventListener('aios-auth-change', syncEntry);

  /* закончился обмен в чате — в списке могла появиться новая сессия
     или обновиться название существующей */
  document.addEventListener('aios-usage-refresh', function () {
    if (isAuthed()) loadNavSessions();
  });

  /* ---------- напоминания по шагам пайплайна (плавающий бейдж) ---------- */

  /* skill_id/название/автопромпт навыка — как у аналогичных кнопок-CTA
     в cabinet.js («Создать новый проект», «Создать онтологию» и т.п.) */
  var REMINDER_SKILLS = {
    onboarding: {
      id: 'project-onboarding',
      name: 'Онбординг проекта',
      prompt: 'Запусти онбординг проекта: познакомься со мной и задай вопросы, ' +
        'нужные для карточки проекта (название, цель, команда, подразделения, бюджет, задел).'
    },
    hypotheses: { id: 'aios-hypotheses-skill', name: 'Формулировка гипотез', prompt: null },
    ontology: { id: 'ontology-engineer', name: 'Конструктор онтологий', prompt: null },
    rag: { id: 'rag-builder', name: 'Конструктор RAG', prompt: null }
  };
  var REMINDERS_POLL_MS = 120000; /* раз в 2 минуты — без инвалидации по конкретным действиям */

  /* Объект сессии auth.js: {token, login, name, role, profile_filled}
     (AUTH_KEY тот же ключ localStorage, что и isAuthed() выше). */
  function getAuthSession() {
    try {
      var raw = localStorage.getItem(AUTH_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  var lastReminderItems = null; /* кэш последнего ответа /api/reminders, чтобы
     перерисовывать бейдж при открытии/закрытии панели без лишнего запроса */

  /* Страницы, где бейдж-напоминание не показывается вовсе. Платформенная
     страница «Агенты» сама и есть следующий шаг пайплайна
     («Соберите агентов») — плавающий бейдж дублировал бы её содержимое. */
  var REMINDER_HIDDEN_PAGES = ['agents.html'];

  function reminderHiddenOnPage() {
    return REMINDER_HIDDEN_PAGES.indexOf(location.pathname.split('/').pop()) !== -1;
  }

  function renderReminderBadge(items) {
    if (!reminderBadge) return;
    if (reminderHiddenOnPage()) {
      reminderBadge.hidden = true;
      return;
    }
    if (document.body.classList.contains('modal-open')) {
      /* открыта любая модалка — чат-панель (сама ставит этот класс) или
         карточка проекта/RAG/онтологии/логина и т.п. (общий паттерн .modal,
         см. §2.8 Architecture_AIOS.md) — бейдж перекрывал бы её элементы */
      reminderBadge.hidden = true;
      return;
    }
    if (!items || !items.length) {
      reminderBadge.hidden = true;
      reminderBadge.textContent = '';
      delete reminderBadge.dataset.step;
      return;
    }
    var item = items[0]; /* sequential-режим — обычно один; all — берём первый по цепочке */
    reminderBadge.textContent = '✎ ' + item.label;
    reminderBadge.dataset.step = item.id;
    reminderBadge.hidden = false;
  }

  function loadReminders() {
    var session = getAuthSession();
    if (!session || !session.token || !window.AIOS_API || !window.AIOS_API.remindersList) {
      renderReminderBadge(null);
      return;
    }
    window.AIOS_API.remindersList(session.token)
      .then(function (data) {
        lastReminderItems = (data && data.items) || [];
        renderReminderBadge(lastReminderItems);
      })
      .catch(function () {
        lastReminderItems = null;
        renderReminderBadge(null);
      });
  }

  if (reminderBadge) {
    reminderBadge.addEventListener('click', function () {
      var stepId = reminderBadge.dataset.step;
      var info = REMINDER_SKILLS[stepId];
      reminderBadge.hidden = true;
      if (info) launchSkill({ id: info.id, name: info.name }, info.prompt || undefined);
    });
  }

  loadReminders();
  window.addEventListener('aios-auth-change', loadReminders);
  setInterval(loadReminders, REMINDERS_POLL_MS);

  /* открытие любой другой модалки (карточка проекта/RAG/онтологии/логина —
     все ставят/снимают document.body.classList 'modal-open', см. §2.8
     Architecture_AIOS.md) должно сразу прятать/возвращать бейдж, а не только
     открытие/закрытие самой чат-панели — иначе бейдж остаётся поверх модалки
     до следующего опроса /api/reminders (раз в REMINDERS_POLL_MS). */
  new MutationObserver(function () {
    renderReminderBadge(lastReminderItems);
  }).observe(document.body, { attributes: true, attributeFilter: ['class'] });

  /* ---------- открытие / закрытие панели ---------- */

  var panelOpen = false;
  var closeTimer = null;
  var lastTrigger = null;

  function welcome() {
    if (isFull) { enterFreshFull(); return; }
    welcomeShown = true;
    appendMessage('assistant',
      'Добро пожаловать в рабочий чат. Диалог идёт через агентный рантайм; ' +
      'модель задана администратором. ' +
      'Вызовы инструментов показываются карточками прямо в чате.');
  }

  function openPanel(via) {
    if (panelOpen) return;
    panelOpen = true;
    renderReminderBadge(lastReminderItems); /* панель открыта — прячем напоминание */
    lastTrigger = via || null;
    clearTimeout(closeTimer);
    if (readyBadge) readyBadge.hidden = true;
    if (PAGE_MODE) {
      /* страница чата: панель уже стоит в разметке, анимаций и
         оверлея нет — только фокус на поле ввода */
      panel.hidden = false;
      input.focus();
    } else {
      if (edgeTab) edgeTab.classList.add('ws-edge-tab--concealed');
      overlay.hidden = false;
      panel.hidden = false;
      document.body.classList.add('modal-open');
      /* двойной rAF: дать браузеру применить transform перед анимацией */
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          overlay.classList.add('ws-overlay--show');
          panel.classList.add('ws-open');
          input.focus();
        });
      });
    }
    if (!messagesEl.childElementCount) {
      var sessionId = getSessionId();
      if (sessionId) {
        restoreSession().then(function (restored) {
          if (!restored && !messagesEl.childElementCount) {
            welcome();
            scrollDown();
          }
        });
      } else {
        welcome();
        scrollDown();
      }
    }
    syncSkillbar();
  }

  function finishClose() {
    panel.hidden = true;
    overlay.hidden = true;
    document.body.classList.remove('modal-open');
    if (edgeTab) edgeTab.classList.remove('ws-edge-tab--concealed');
    if (lastTrigger && !lastTrigger.hidden) lastTrigger.focus();
  }

  function closePanel() {
    if (!panelOpen || PAGE_MODE) return; /* страницу чата не сворачиваем */
    panelOpen = false;
    renderReminderBadge(lastReminderItems); /* панель закрыта — напоминание снова уместно */
    overlay.classList.remove('ws-overlay--show');
    panel.classList.remove('ws-open');
    clearTimeout(closeTimer);
    closeTimer = setTimeout(finishClose, isFull ? Math.max(ANIM_MS, 480) : ANIM_MS);
  }

  triggers.forEach(function (btn) {
    btn.addEventListener('click', function () { openPanel(btn); });
  });
  closeBtn.addEventListener('click', closePanel);
  overlay.addEventListener('click', closePanel);

  /* ---------- свежий чат (chat.html): приветствие по центру ---------- */

  /* Пока на странице чата нет диалога — нечего восстанавливать и ничего
     не отправляли, — поле ввода стоит посередине страницы, над ним
     приветствие по времени суток и по имени-отчеству из профиля
     «Кабинета», под полем — справка «Что можем сделать сегодня»:
     краткий список возможностей чата по инструментам платформы.
     Клик по пункту подставляет стартовую формулировку в поле ввода.
     Первое же сообщение возвращает поле ввода вниз, как в обычном
     диалоге. Поле ввода не копируем, а переносим целиком (форма со
     всеми кнопками и меню «+») — поведение то же, что в диалоге. */
  var freshLive = false;
  var freshNamePromise = null;

  /* Приветствие по времени суток; «Здравствуйте» и «Приветствую» —
     универсальные варианты в любое время (как у Claude) */
  function freshGreeting() {
    var h = new Date().getHours();
    var pool;
    if (h >= 4 && h < 12) pool = ['Доброе утро', 'Доброе утро', 'Здравствуйте', 'Приветствую'];
    else if (h < 18) pool = ['Добрый день', 'Добрый день', 'Здравствуйте', 'Приветствую'];
    else if (h < 23) pool = ['Добрый вечер', 'Добрый вечер', 'Здравствуйте', 'Приветствую'];
    else pool = ['Здравствуйте', 'Приветствую']; /* поздняя ночь */
    return pool[Math.floor(Math.random() * pool.length)];
  }

  /* Имя-отчество из профиля «Кабинета»; пустой профиль — без обращения */
  function freshName() {
    if (freshNamePromise) return freshNamePromise;
    var session = getAuthSession();
    if (!session || !session.token || !window.AIOS_API || !window.AIOS_API.cabinetGetProfile) {
      freshNamePromise = Promise.resolve('');
      return freshNamePromise;
    }
    freshNamePromise = window.AIOS_API.cabinetGetProfile(session.token)
      .then(function (p) {
        return [p && p.first_name, p && p.patronymic]
          .filter(Boolean).join(' ').trim();
      })
      .catch(function () { return ''; });
    return freshNamePromise;
  }

  /* ---------- полноэкранный чат: пустой экран нового диалога ---------- */
  var welcomeShown = false;
  var freshFullLive = false;
  var freshFullEl = null;
  var freshShift = 0;
  var FRESH_TARGET = 0.55; /* центр поля ввода — чуть ниже середины окна */

  function layoutFreshFull(tries) {
    if (!freshFullLive || !freshFullEl) return;
    var pr = panel.getBoundingClientRect();
    if (pr.height < 200) {
      if ((tries || 0) < 20) setTimeout(function () { layoutFreshFull((tries || 0) + 1); }, 80);
      return;
    }
    var cr = composer.getBoundingClientRect();
    var naturalCenter = cr.top - pr.top - freshShift + cr.height / 2;
    var targetCenter = pr.height * FRESH_TARGET;
    freshShift = targetCenter - naturalCenter;
    composer.style.transform = 'translateY(' + freshShift + 'px)';
    freshFullEl.style.top = Math.max(60, targetCenter - cr.height / 2 - freshFullEl.offsetHeight - 24) + 'px';
    freshFullEl.classList.add('ws-freshfull--in');
  }

  function enterFreshFull() {
    if (freshFullLive) return;
    freshFullLive = true;
    panel.classList.add('ws-panel--freshfull');
    freshFullEl = document.createElement('div');
    freshFullEl.className = 'ws-freshfull';
    freshFullEl.innerHTML = '<span class="ws-side-sigil" aria-hidden="true"></span>' +
      '<div class="ws-freshfull-title">Рабочий чат AI OS</div>';
    panel.appendChild(freshFullEl);
    requestAnimationFrame(function () { layoutFreshFull(0); });
    try { input.focus(); } catch (e) { /* ок */ }
  }

  /* первое сообщение / очистка ленты: поле ввода плавно съезжает вниз,
     значок исчезает, заголовок «Рабочий чат» уходит в шапку */
  function leaveFreshFull() {
    if (!freshFullLive) return;
    freshFullLive = false;
    panel.classList.remove('ws-panel--freshfull');
    freshShift = 0;
    composer.style.transform = '';
    var el = freshFullEl;
    freshFullEl = null;
    if (el) {
      el.classList.remove('ws-freshfull--in');
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 400);
    }
  }
  window.addEventListener('resize', function () { layoutFreshFull(0); });

  /* «LLM: модель» в шапке полноэкранного чата не показываем; если у пользователя
     личное окружение LLM — пишем «Личные настройки LLM: модель» */
  var personalLlmEl = null;
  function loadPersonalLlm() {
    if (!modelLabel || !modelLabel.parentNode) return;
    if (!personalLlmEl) {
      personalLlmEl = document.createElement('span');
      personalLlmEl.className = 'ws-personal-llm';
      personalLlmEl.hidden = true;
      modelLabel.parentNode.appendChild(personalLlmEl);
    }
    var session = getAuthSession();
    if (!session || !session.token || !window.AIOS_API || !window.AIOS_API.settingsGetLLM) {
      personalLlmEl.hidden = true;
      return;
    }
    window.AIOS_API.settingsGetLLM(session.token).then(function (d) {
      if (d && d.active) {
        personalLlmEl.textContent = 'Личные настройки LLM' + (d.model ? ': ' + d.model : '');
        personalLlmEl.hidden = false;
      } else {
        personalLlmEl.hidden = true;
      }
    }).catch(function () { personalLlmEl.hidden = true; });
  }
  loadPersonalLlm();

  (function watchFullMode() {
    var wasFull = panel.classList.contains('ws-panel--full');
    new MutationObserver(function () {
      var now = panel.classList.contains('ws-panel--full');
      if (now === wasFull) return;
      wasFull = now;
      if (now) {
        loadPersonalLlm();
        /* в половинном режиме мог остаться только приветственный пузырь — в
           полноэкранном пустой экран заменяет его */
        if (!getSessionId() && !freshFullLive &&
            (!messagesEl.childElementCount || (welcomeShown && messagesEl.childElementCount === 1))) {
          clearTranscript();
          enterFreshFull();
        }
      } else if (freshFullLive) {
        leaveFreshFull();
        if (!messagesEl.childElementCount) welcome();
      }
    }).observe(panel, { attributes: true, attributeFilter: ['class'] });
  })();

  function enterFresh() {
    if (!PAGE_MODE || freshLive) return;
    leaveFresh();
    freshLive = true;
    panel.classList.add('ws-panel--fresh');
    var hero = document.createElement('div');
    hero.className = 'ws-fresh';
    hero.id = 'wsFresh';

    var greet = document.createElement('div');
    greet.className = 'ws-fresh-greet';
    var base = freshGreeting();
    greet.textContent = base + '!';
    freshName().then(function (name) {
      if (!freshLive) return;
      greet.textContent = name ? base + ', ' + name + '!' : base + '!';
    });
    hero.appendChild(greet);

    var hold = document.createElement('div');
    hold.className = 'ws-fresh-input';
    hero.appendChild(hold);

    /* Справка «Чем займемся сегодня» — карточки возможностей по
       инструментам платформы (api.js, AIOS_TOOL_TITLES): расчёты,
       химия, литература, RAG-базы, онтологии, документы. Иконки —
       линейные SVG в стиле платформы (штрих currentColor, скругления).
       Клик подставляет в поле ввода стартовую формулировку. */
    var tips = document.createElement('div');
    tips.className = 'ws-fresh-tips';
    var tipsTitle = document.createElement('div');
    tipsTitle.className = 'ws-fresh-tips-title';
    tipsTitle.textContent = 'Чем займемся сегодня';
    tips.appendChild(tipsTitle);
    var cards = document.createElement('div');
    cards.className = 'ws-fresh-cards';
    var SVG_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
    [
      [/* калькулятор */
        SVG_OPEN + '<rect x="5" y="3" width="14" height="18" rx="2.5"/><line x1="8.5" y1="7" x2="15.5" y2="7"/><line x1="8.5" y1="11.5" x2="8.5" y2="11.6"/><line x1="12" y1="11.5" x2="12" y2="11.6"/><line x1="15.5" y1="11.5" x2="15.5" y2="11.6"/><line x1="8.5" y1="15" x2="8.5" y2="15.1"/><line x1="12" y1="15" x2="12" y2="15.1"/><line x1="15.5" y1="15" x2="15.5" y2="15.1"/></svg>',
        'Инженерный расчёт', 'таблица и график в ответе',
        'Выполни инженерный расчёт: '],
      [/* колба */
        SVG_OPEN + '<path d="M9.5 3h5"/><path d="M10 3v5.2L4.9 17.5a2.4 2.4 0 0 0 2.1 3.6h10a2.4 2.4 0 0 0 2.1-3.6L14 8.2V3"/><path d="M7.6 14.5h8.8"/></svg>',
        'Вещество или реакция', 'свойства, структуры, план синтеза, классы опасности',
        'Разбери вещество или реакцию: '],
      [/* открытая книга */
        SVG_OPEN + '<path d="M12 6.5C10.5 5 8.5 4.5 5.5 4.5c-.8 0-1.5.5-1.5 1.3v11.2c0 .8.7 1.2 1.5 1.2 3 0 5 .5 6.5 1.8 1.5-1.3 3.5-1.8 6.5-1.8.8 0 1.5-.4 1.5-1.2V5.8c0-.8-.7-1.3-1.5-1.3-3 0-5 .5-6.5 2z"/><path d="M12 6.5V20"/></svg>',
        'Поиск в литературе', 'статьи и данные по теме вопроса',
        'Найди в мировой литературе: '],
      [/* база данных */
        SVG_OPEN + '<ellipse cx="12" cy="5.5" rx="7" ry="2.8"/><path d="M5 5.5v13c0 1.6 3.1 2.9 7 2.9s7-1.3 7-2.9v-13"/><path d="M5 12c0 1.6 3.1 2.9 7 2.9s7-1.3 7-2.9"/></svg>',
        'Система знаний', 'ответы с опорой на подключённую RAG-базу',
        'Ответь с опорой на базу знаний: '],
      [/* граф онтологии */
        SVG_OPEN + '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="8" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M8.4 6.9l7.2.9"/><path d="M6.9 8.3l4.2 7.4"/><path d="M16.9 10.3l-3.5 5.5"/></svg>',
        'Онтология', 'карта классов и связей предметной области',
        'Построй онтологию предметной области: '],
      [/* документ */
        SVG_OPEN + '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6"/><path d="M9 16.5h4"/></svg>',
        'Сборка документа', 'карточка проекта, отчёт или записка',
        'Собери документ: ']
    ].forEach(function (card) {
      var el = document.createElement('button');
      el.type = 'button';
      el.className = 'ws-fresh-card';
      var ico = document.createElement('span');
      ico.className = 'ws-fresh-card-ico';
      ico.innerHTML = card[0]; /* статичная строка-константа, не пользовательский ввод */
      var name = document.createElement('span');
      name.className = 'ws-fresh-card-name';
      name.textContent = card[1];
      var note = document.createElement('span');
      note.className = 'ws-fresh-card-note';
      note.textContent = card[2];
      el.appendChild(ico);
      el.appendChild(name);
      el.appendChild(note);
      el.addEventListener('click', function () {
        input.value = card[3]; /* стартовая формулировка — можно дописать */
        autosize();
        input.focus();
      });
      cards.appendChild(el);
    });
    tips.appendChild(cards);
    hero.appendChild(tips);

    messagesEl.innerHTML = '';
    messagesEl.appendChild(hero);
    hold.appendChild(composer);
    input.focus();
  }

  function leaveFresh() {
    leaveFreshFull();
    if (!freshLive) return;
    freshLive = false;
    panel.classList.remove('ws-panel--fresh');
    panel.appendChild(composer); /* поле ввода возвращается вниз панели */
    var hero = document.getElementById('wsFresh');
    if (hero) hero.remove();
  }

  /* Очистка ленты сообщений: сначала выходим из «свежего» состояния —
     иначе вместе с лентой удалится перенесённое в неё поле ввода */
  function clearTranscript() {
    leaveFresh();
    activityClear();
    messagesEl.innerHTML = '';
  }

  /* ---------- страница чата (chat.html) ---------- */

  /* На отдельной странице панель стоит в разметке с самого начала:
     без оверлея, язычка, растягивания и кнопки закрытия. Сессию берём
     из ?session=… (клик по сессии в меню или «Кабинете»), иначе —
     продолжаем последний диалог (localStorage общий с поповером).
     Если со страницы «Агенты» пришёл навык с автозапуском, ничего
     не восстанавливаем — его запустит handleSkillLaunch ниже. */
  if (PAGE_MODE) {
    panelOpen = true;
    panel.classList.add('ws-panel--page');
    panel.hidden = false;
    overlay.hidden = true;
    grip.hidden = true;
    expandBtn.hidden = true;
    closeBtn.hidden = true;
    var pageSkill = readSkill();
    var pageSessionId = new URLSearchParams(location.search).get('session');
    if (pageSessionId) saveSessionId(pageSessionId);
    if (!(pageSkill && pageSkill.open) && !messagesEl.childElementCount) {
      if (getSessionId()) {
        restoreSession().then(function (restored) {
          if (!restored && !messagesEl.childElementCount) enterFresh();
        });
      } else {
        enterFresh();
      }
    }
  }


  /* Открыть конкретную сессию из чужого интерфейса («Кабинет» → история
     сессий): фиксируем id, очищаем транскрипт и открываем панель —
     восстановление сессии выполняет restoreSession(). */
  document.addEventListener('aios-open-session', function (e) {
    var id = e && e.detail && e.detail.session_id;
    if (!id) return;
    saveSessionId(id);
    clearTranscript();
    if (panelOpen) {
      restoreSession().then(function (restored) {
        if (!restored && !messagesEl.childElementCount) {
          welcome();
          scrollDown();
        }
      });
    } else {
      openPanel(null);
    }
  });

  if (readyBadge) {
    readyBadge.addEventListener('click', function () {
      readyBadge.hidden = true;
      openPanel(null);
    });
  }

  /* ---------- индикация фоновой генерации ---------- */

  var generating = false;

  function setGenerating(on) {
    if (generating === on) return;
    generating = on;
    panel.classList.toggle('ws-busy', on);
    if (liveDot) liveDot.hidden = !on;
    triggers.forEach(function (btn) { btn.classList.toggle('ws-trigger-busy', on); });
  }

  /* Панель свернули, а ответ пришёл (или стрим оборвался и ответ
     догенерируется на сервере) — приглашающая плашка поверх страницы */
  function notifyReady(interrupted) {
    if (panelOpen || !readyBadge) return;
    readyBadge.textContent = interrupted ? '✦ Ответ догенерируется в фоне' : '✦ Ответ готов';
    readyBadge.hidden = false;
  }

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (panel.hidden) return;
    closePanel();
  });

  /* ---------- растягивание панели ---------- */

  var drag = null;
  var prevWidth = null;
  var isFull = false;

  function clampWidth(px) {
    return Math.min(Math.max(px, MIN_WIDTH), window.innerWidth);
  }

  grip.addEventListener('pointerdown', function (e) {
    drag = { startX: e.clientX, startW: panel.offsetWidth };
    grip.setPointerCapture(e.pointerId);
    document.body.classList.add('ws-dragging');
  });

  grip.addEventListener('pointermove', function (e) {
    if (!drag) return;
    /* из полного экрана выходим, только когда потянули вправо заметно: случайное касание ручки окно не сворачивает */
    if (isFull && (e.clientX - drag.startX) < 40) return;
    var width = clampWidth(drag.startW + (drag.startX - e.clientX));
    panel.style.width = width + 'px';
    /* ручное растягивание выходит из режима «во весь экран» */
    isFull = false;
    expandBtn.setAttribute('aria-label', 'Во весь экран');
    syncFull();
  });

  ['pointerup', 'pointercancel'].forEach(function (ev) {
    grip.addEventListener(ev, function () {
      drag = null;
      document.body.classList.remove('ws-dragging');
    });
  });

  function toggleFull() {
    if (!isFull && !PAGE_MODE) {
      /* «Во весь экран» в поповере — переход на страницу чата с тем же диалогом */
      var sid = getSessionId();
      location.assign('chat.html' + (sid ? '?session=' + encodeURIComponent(sid) : ''));
      return;
    }
    if (!isFull) {
      prevWidth = panel.style.width || DEFAULT_WIDTH + 'px';
      panel.style.width = '100vw';
      isFull = true;
      expandBtn.setAttribute('aria-label', 'Вернуть ширину');
    } else {
      panel.style.width = prevWidth;
      isFull = false;
      expandBtn.setAttribute('aria-label', 'Во весь экран');
    }
    syncFull();
  }

  expandBtn.addEventListener('click', toggleFull);
  grip.addEventListener('dblclick', toggleFull);

  /* потянули окно до левого края экрана — разворачиваем на весь экран */
  var dragStartW = 0;
  var dragReachedLeft = false;
  grip.addEventListener('pointerdown', function () {
    dragStartW = panel.offsetWidth;
    dragReachedLeft = false;
  });
  grip.addEventListener('pointermove', function (e) {
    if (!drag) return;
    dragReachedLeft = e.clientX <= 70;
  });
  grip.addEventListener('pointerup', function () {
    if (dragReachedLeft && !isFull) {
      dragReachedLeft = false;
      panel.style.width = (dragStartW || DEFAULT_WIDTH) + 'px'; /* «⛶» потом вернёт исходную ширину */
      toggleFull();
    }
    dragReachedLeft = false;
  });

  /* ---------- панель истории сессий: узкая полоса / колонка в развёрнутом виде ---------- */

  /* ---------- поиск по истории чатов (лупа) ---------- */
  var SEARCH_DELAY = 1000;
  var searchOv = null;
  var searchInput = null;
  var searchBody = null;
  var searchTimer = null;
  var searchSeq = 0;

  function searchBuild() {
    if (searchOv) return;
    searchOv = document.createElement('div');
    searchOv.className = 'ws-search-overlay';
    searchOv.hidden = true;
    searchOv.innerHTML =
      '<div class="ws-search-box" role="dialog" aria-modal="true" aria-label="Поиск по чатам">' +
      '<div class="ws-search-bar">' +
      '<span class="ws-search-lupa"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path></svg></span>' +
      '<input class="ws-search-input" type="text" placeholder="Поиск по содержимому чатов…" autocomplete="off" spellcheck="false" aria-label="Поиск по содержимому чатов">' +
      '<button class="ws-search-x" type="button" aria-label="Закрыть поиск"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>' +
      '</div>' +
      '<div class="ws-search-body" hidden></div>' +
      '</div>';
    document.body.appendChild(searchOv);
    searchInput = searchOv.querySelector('.ws-search-input');
    searchBody = searchOv.querySelector('.ws-search-body');

    /* клики и нажатия внутри окна не должны доходить до обработчиков страницы
       («клик вне окна чата» и т.п.) */
    ['mousedown', 'pointerdown', 'touchstart'].forEach(function (ev) {
      searchOv.addEventListener(ev, function (e) { e.stopPropagation(); });
    });
    searchOv.addEventListener('click', function (e) {
      e.stopPropagation();
      if (e.target === searchOv) searchClose();
    });
    searchOv.querySelector('.ws-search-x').addEventListener('click', searchClose);
    searchInput.addEventListener('input', searchOnInput);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && searchOv && !searchOv.hidden) {
        e.stopPropagation();
        searchClose();
      }
    }, true);
  }

  function searchOpen() {
    searchBuild();
    searchOv.hidden = false;
    searchInput.value = '';
    searchBody.hidden = true;
    searchBody.innerHTML = '';
    searchSeq++;
    setTimeout(function () { searchInput.focus(); }, 0);
  }

  function searchClose() {
    if (!searchOv) return;
    clearTimeout(searchTimer);
    searchSeq++;
    searchOv.hidden = true;
  }

  function searchSkeleton() {
    var html = '';
    for (var i = 0; i < 3; i++) {
      html += '<div class="ws-search-sk"><span class="ws-search-sk-c"></span>' +
              '<span class="ws-search-sk-l"><i></i><i></i></span></div>';
    }
    searchBody.hidden = false;
    searchBody.innerHTML = html;
  }

  function searchNote(text) {
    searchBody.hidden = false;
    searchBody.innerHTML = '';
    var n = document.createElement('div');
    n.className = 'ws-search-empty';
    n.textContent = text;
    searchBody.appendChild(n);
  }

  function searchOnInput() {
    var q = searchInput.value.trim();
    clearTimeout(searchTimer);
    searchSeq++;
    if (!q) {
      searchBody.hidden = true;
      searchBody.innerHTML = '';
      return;
    }
    searchSkeleton(); /* пока пользователь печатает — только бледные заглушки */
    var seq = searchSeq;
    searchTimer = setTimeout(function () { searchRun(q, seq); }, SEARCH_DELAY);
  }

  function searchRun(q, seq) {
    var session = getAuthSession();
    if (!session || !session.token || !window.AIOS_API || !window.AIOS_API.cabinetSearchSessions) {
      searchNote('Войдите, чтобы искать по чатам');
      return;
    }
    window.AIOS_API.cabinetSearchSessions(session.token, q)
      .then(function (data) {
        if (seq !== searchSeq) return;
        searchRender((data && data.items) || [], q);
      })
      .catch(function () {
        if (seq !== searchSeq) return;
        searchNote('Не удалось выполнить поиск');
      });
  }

  /* фрагмент текста с полужирными совпадениями (без innerHTML) */
  function searchMark(el, text, q) {
    var low = text.toLowerCase();
    var ql = q.replace(/\s+/g, ' ').trim().toLowerCase();
    var pos = 0;
    var i;
    if (!ql) { el.textContent = text; return; }
    while ((i = low.indexOf(ql, pos)) !== -1) {
      if (i > pos) el.appendChild(document.createTextNode(text.slice(pos, i)));
      var b = document.createElement('b');
      b.textContent = text.slice(i, i + ql.length);
      el.appendChild(b);
      pos = i + ql.length;
    }
    if (pos < text.length) el.appendChild(document.createTextNode(text.slice(pos)));
  }

  function searchDate(ts) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '';
    var opt = { day: 'numeric', month: 'short' };
    if (d.getFullYear() !== new Date().getFullYear()) opt.year = 'numeric';
    return d.toLocaleDateString('ru-RU', opt);
  }

  /* иконка «Новый чат» — та же, что в шапке половинного режима, в кружке */
  function searchIcon() {
    var span = document.createElement('span');
    span.className = 'ws-search-ico';
    var src = document.getElementById('wsNewBtn');
    var svg = src && src.querySelector('svg');
    if (svg) {
      var c = svg.cloneNode(true);
      c.setAttribute('width', '18');
      c.setAttribute('height', '18');
      span.appendChild(c);
    }
    return span;
  }

  function searchRender(items, q) {
    var _p = chatPrefs();
    items = items.filter(function (it) { return !_p.hidden[it.id]; }).map(function (it) {
      return _p.titles[it.id] ? Object.assign({}, it, { title: _p.titles[it.id] }) : it;
    });
    if (!items.length) { searchNote('Ничего не найдено'); return; }
    searchBody.hidden = false;
    searchBody.innerHTML = '';
    items.forEach(function (it) {
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'ws-search-row';
      row.appendChild(searchIcon());

      var main = document.createElement('span');
      main.className = 'ws-search-main';
      var title = document.createElement('span');
      title.className = 'ws-search-title';
      title.textContent = it.title || 'Без названия';
      main.appendChild(title);
      if (it.snippet) {
        var snip = document.createElement('span');
        snip.className = 'ws-search-snip';
        snip.appendChild(document.createTextNode('… '));
        searchMark(snip, it.snippet, q);
        snip.appendChild(document.createTextNode(' …'));
        main.appendChild(snip);
      }
      row.appendChild(main);

      var date = document.createElement('span');
      date.className = 'ws-search-date';
      date.textContent = searchDate(it.updated_at);
      row.appendChild(date);

      row.addEventListener('click', function () {
        searchClose();
        saveSessionId(it.id);
        if (typeof restoreSession === 'function') restoreSession(it.id);
        else location.assign('chat.html?session=' + encodeURIComponent(it.id));
        sideDrawer = false;
        updateSide();
        renderSide();
      });
      searchBody.appendChild(row);
    });
  }

  var sideSearchBtn = document.getElementById('wsSideSearch');
  if (sideSearchBtn) sideSearchBtn.addEventListener('click', searchOpen);

  /* подсказки data-tooltip не выходят за край экрана и окна чата: у верхнего/бокового
     края показываются под кнопкой и прижимаются к краю, чтобы текст был виден целиком */
  function placeTooltip(el) {
    var text = el.getAttribute('data-tooltip');
    if (!text) return;
    var cv = placeTooltip.cv || (placeTooltip.cv = document.createElement('canvas'));
    var c2 = cv.getContext('2d');
    c2.font = '600 12px ' + (getComputedStyle(el).fontFamily || 'sans-serif');
    var w = c2.measureText(text).width + 20;
    var r = el.getBoundingClientRect();
    var panel = el.closest('.ws-panel');
    var inSide = !!el.closest('.ws-side');
    var pr = panel && !inSide ? panel.getBoundingClientRect() : null;
    var top = pr ? Math.max(0, pr.top) : 0;
    var left = pr ? Math.max(0, pr.left) : 0;
    var right = pr ? Math.min(window.innerWidth, pr.right) : window.innerWidth;
    var cx = r.left + r.width / 2;
    el.classList.toggle('tt-below', r.top - top < 44);
    el.classList.toggle('tt-end', cx + w / 2 > right - 8);
    el.classList.toggle('tt-start', !(cx + w / 2 > right - 8) && cx - w / 2 < left + 8);
  }
  ['mouseover', 'focusin'].forEach(function (ev) {
    document.addEventListener(ev, function (e) {
      var el = e.target && e.target.closest ? e.target.closest('[data-tooltip]') : null;
      if (el) placeTooltip(el);
    }, true);
  });

  var sideEl = document.getElementById('wsSide');
  var sideList = document.getElementById('wsSideList');
  var sideNew = document.getElementById('wsSideNew');
  var sideHist = document.getElementById('wsSideHist');
  var sideDrawer = false;
  var sideItems; /* undefined — грузим, null — ошибка, [] — пусто */
  var SIDE_GROUPS = ['Сегодня', '7 дней', '30 дней', 'Ранее'];

  function sideGroupOf(ts) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return 3;
    var now = new Date();
    var a = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    var b = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    var days = Math.round((a - b) / 86400000);
    if (days <= 0) return 0;
    if (days <= 7) return 1;
    if (days <= 30) return 2;
    return 3;
  }

  function sideNote(text) {
    sideList.innerHTML = '';
    var n = document.createElement('div');
    n.className = 'ws-side-note';
    n.textContent = text;
    sideList.appendChild(n);
  }

  /* ---------- чаты в колонке: меню «⋯» (переименовать, закрепить, удалить) ----------
     Удаление — только из интерфейса: на сервере сессия остаётся. Переименование,
     закрепление и скрытие хранятся в браузере по учётной записи. */
  function chatPrefsKey() {
    var u = getAuthSession() || {};
    var uu = u.user || u.profile || {};
    var id = uu.id || u.user_id || u.id || uu.email || u.email || u.login || uu.login || 'anon';
    return 'aios.chatprefs.' + id;
  }
  function chatPrefs() {
    var p = { titles: {}, pinned: {}, hidden: {} };
    try {
      var raw = JSON.parse(localStorage.getItem(chatPrefsKey()) || 'null');
      if (raw) { p.titles = raw.titles || {}; p.pinned = raw.pinned || {}; p.hidden = raw.hidden || {}; }
    } catch (e) { /* приватный режим */ }
    return p;
  }
  function saveChatPrefs(p) {
    try { localStorage.setItem(chatPrefsKey(), JSON.stringify(p)); } catch (e) { /* ок */ }
  }

  var ICON_DOTS = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"></circle><circle cx="12" cy="12" r="1.8"></circle><circle cx="19" cy="12" r="1.8"></circle></svg>';
  var SVG_A = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
  var ICON_EDIT = SVG_A + '<path d="M4 20h4L19 9l-4-4L4 16v4z"></path><path d="M13.5 6.5l4 4"></path></svg>';
  var ICON_PIN = SVG_A + '<path d="M12 17v5"></path><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6z"></path></svg>';
  var ICON_TRASH = SVG_A + '<path d="M4 7h16"></path><path d="M9 7V4h6v3"></path><path d="M6 7l1 13h10l1-13"></path><path d="M10 11v6M14 11v6"></path></svg>';

  var chatMenu = null;
  var chatMenuItem = null;
  var chatMenuRow = null;

  function closeChatMenu() {
    if (!chatMenu) return;
    chatMenu.classList.remove('on');
    if (chatMenuRow) chatMenuRow.classList.remove('ws-side-row--menu');
    chatMenuItem = null;
    chatMenuRow = null;
  }

  function ensureChatMenu() {
    if (chatMenu) return chatMenu;
    chatMenu = document.createElement('div');
    chatMenu.className = 'ws-chat-menu';
    chatMenu.setAttribute('role', 'menu');
    chatMenu.innerHTML =
      '<button type="button" role="menuitem" data-act="rename">' + ICON_EDIT + '<span>Переименовать</span></button>' +
      '<button type="button" role="menuitem" data-act="pin">' + ICON_PIN + '<span class="ws-chat-menu-pin">Закрепить</span></button>' +
      '<button type="button" role="menuitem" class="ws-chat-menu-del" data-act="del">' + ICON_TRASH + '<span>Удалить</span></button>';
    document.body.appendChild(chatMenu);
    chatMenu.addEventListener('click', function (e) {
      e.stopPropagation();
      var b = e.target.closest ? e.target.closest('button[data-act]') : null;
      if (!b || !chatMenuItem) return;
      var act = b.getAttribute('data-act');
      var item = chatMenuItem;
      var row = chatMenuRow;
      closeChatMenu();
      chatAction(act, item, row);
    });
    document.addEventListener('mousedown', function (e) {
      if (!chatMenu.classList.contains('on')) return;
      if (chatMenu.contains(e.target)) return;
      if (e.target.closest && e.target.closest('.ws-side-dots')) return;
      closeChatMenu();
    }, true);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeChatMenu();
    }, true);
    window.addEventListener('resize', closeChatMenu);
    if (sideList) sideList.addEventListener('scroll', closeChatMenu, { passive: true });
    return chatMenu;
  }

  function openChatMenu(dots, it, row) {
    var m = ensureChatMenu();
    if (m.classList.contains('on') && chatMenuItem && chatMenuItem.id === it.id) { closeChatMenu(); return; }
    closeChatMenu();
    chatMenuItem = it;
    chatMenuRow = row;
    row.classList.add('ws-side-row--menu');
    var pinned = !!chatPrefs().pinned[it.id];
    m.querySelector('.ws-chat-menu-pin').textContent = pinned ? 'Открепить' : 'Закрепить';
    var r = dots.getBoundingClientRect();
    var w = m.offsetWidth;
    var h = m.offsetHeight;
    var left = Math.min(window.innerWidth - w - 8, Math.max(8, r.right - w));
    var top = r.bottom + 4;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
    m.style.left = left + 'px';
    m.style.top = top + 'px';
    requestAnimationFrame(function () { m.classList.add('on'); });
  }

  function chatAction(act, it, row) {
    var p = chatPrefs();
    if (act === 'pin') {
      if (p.pinned[it.id]) delete p.pinned[it.id]; else p.pinned[it.id] = Date.now();
      saveChatPrefs(p);
      renderSide();
    } else if (act === 'del') {
      p.hidden[it.id] = 1;
      delete p.pinned[it.id];
      saveChatPrefs(p);
      if (it.id && it.id === getSessionId() && sideNew) sideNew.click(); /* открытый чат убрали — начинаем новый */
      renderSide();
    } else if (act === 'rename') {
      startRename(row, it);
    }
  }

  function startRename(row, it) {
    var btn = row && row.querySelector('.ws-side-item');
    if (!btn) return;
    var cur = chatPrefs().titles[it.id] || it.title || '';
    var inp = document.createElement('input');
    inp.type = 'text';
    inp.className = 'ws-side-rename';
    inp.value = cur;
    inp.maxLength = 120;
    row.classList.add('ws-side-row--edit');
    row.replaceChild(inp, btn);
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      if (save) {
        var v = inp.value.trim();
        var p = chatPrefs();
        if (v && v !== (it.title || '')) p.titles[it.id] = v.slice(0, 120); else delete p.titles[it.id];
        saveChatPrefs(p);
      }
      renderSide();
    }
    inp.addEventListener('keydown', function (e) {
      e.stopPropagation();
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    inp.addEventListener('blur', function () { finish(true); });
    inp.focus();
    inp.select();
  }

  function buildSideRow(it, prefs, activeId) {
    var row = document.createElement('div');
    row.className = 'ws-side-row';
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'ws-side-item' + (it.id && it.id === activeId ? ' ws-side-item--active' : '');
    var shown = prefs.titles[it.id] || it.title;
    b.title = shown || 'Без названия';
    b.textContent = navSessionLabel(shown);
    b.addEventListener('click', function () {
      saveSessionId(it.id);
      if (typeof restoreSession === 'function') restoreSession(it.id);
      else location.assign('chat.html?session=' + encodeURIComponent(it.id));
      sideDrawer = false;
      updateSide();
      renderSide();
    });
    var dots = document.createElement('button');
    dots.type = 'button';
    dots.className = 'ws-side-dots';
    dots.setAttribute('aria-label', 'Действия с чатом');
    dots.setAttribute('aria-haspopup', 'menu');
    dots.innerHTML = ICON_DOTS;
    dots.addEventListener('click', function (e) {
      e.stopPropagation();
      openChatMenu(dots, it, row);
    });
    row.appendChild(b);
    row.appendChild(dots);
    return row;
  }

  function renderSide() {
    if (!sideList) return;
    if (sideItems === undefined) { sideNote('Загрузка…'); return; }
    if (sideItems === null) { sideNote('Не удалось загрузить историю'); return; }
    var prefs = chatPrefs();
    var items = sideItems.filter(function (it) { return !prefs.hidden[it.id]; });
    if (!items.length) { sideNote('Сессий пока нет'); return; }
    closeChatMenu();
    sideList.innerHTML = '';
    var activeId = getSessionId();
    var pinned = items.filter(function (it) { return prefs.pinned[it.id]; })
      .sort(function (x, y) { return prefs.pinned[y.id] - prefs.pinned[x.id]; });
    var groups = [[], [], [], []];
    items.forEach(function (it) {
      if (!prefs.pinned[it.id]) groups[sideGroupOf(it.updated_at)].push(it);
    });
    function addGroup(label, list) {
      var h = document.createElement('div');
      h.className = 'ws-side-group';
      h.textContent = label;
      sideList.appendChild(h);
      list.forEach(function (it) { sideList.appendChild(buildSideRow(it, prefs, activeId)); });
    }
    if (pinned.length) addGroup('Закреплённые', pinned);
    groups.forEach(function (list, gi) { if (list.length) addGroup(SIDE_GROUPS[gi], list); });
  }

  function loadSide() {
    var session = getAuthSession();
    if (!sideList || !session || !session.token || !window.AIOS_API || !window.AIOS_API.cabinetListSessions) return;
    if (sideItems === undefined) renderSide();
    window.AIOS_API.cabinetListSessions(session.token)
      .then(function (data) {
        sideItems = ((data && data.items) || []).slice().sort(function (x, y) {
          return Number(y.updated_at) - Number(x.updated_at);
        });
        renderSide();
      })
      .catch(function () { sideItems = null; renderSide(); });
  }

  function updateSide() {
    if (!sideEl) return;
    sideEl.classList.toggle('ws-side--wide', !!(isFull || sideDrawer));
    sideEl.classList.toggle('ws-side--open', !!(sideDrawer && !isFull));
    if (sideHist) sideHist.setAttribute('aria-expanded', sideDrawer ? 'true' : 'false');
    syncSideTips();
    syncHistBtn();
    applySideCollapsed();    syncRailTips();
  }

  /* ---------- сворачиваемая колонка истории в развёрнутом чате ---------- */
  var SIDE_COLLAPSE_KEY = 'aios.side.collapsed';
  var sideCollapsed = false;
  try { sideCollapsed = localStorage.getItem(SIDE_COLLAPSE_KEY) === '1'; } catch (e) { /* приватный режим */ }
  var collapseAnimTimer = null;

  var SIDE_TOGGLE_SVG = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="4"></rect><path d="M9 4v16"></path><path d="M15 9l-3 3 3 3"></path></svg>';

  (function buildMiniBar() {
    var panelEl = document.getElementById('wsPanel');
    if (!panelEl || document.getElementById('wsMini')) return;
    var mini = document.createElement('div');
    mini.className = 'ws-mini';
    mini.id = 'wsMini';
    mini.innerHTML =
      '<span class="ws-side-sigil" aria-hidden="true"></span>' +
      '<div class="ws-mini-pill">' +
      '<button class="ws-mini-btn ws-mini-btn--flip" id="wsMiniOpen" type="button" aria-label="Открыть сайдбар" data-tooltip="Открыть сайдбар">' + SIDE_TOGGLE_SVG + '</button>' +
      '<button class="ws-mini-btn" id="wsMiniSearch" type="button" aria-label="Поиск по чатам" data-tooltip="Поиск по чатам"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path></svg></button>' +
      '<button class="ws-mini-btn" id="wsMiniNew" type="button" aria-label="Новый чат" data-tooltip="Новый чат"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 8v8M8 12h8"></path></svg></button>' +
      '</div>';
    panelEl.insertBefore(mini, panelEl.firstChild);
    mini.querySelector('#wsMiniOpen').addEventListener('click', function () { setSideCollapsed(false); });
    mini.querySelector('#wsMiniSearch').addEventListener('click', function () {
      var b = document.getElementById('wsSideSearch'); if (b) b.click();
    });
    mini.querySelector('#wsMiniNew').addEventListener('click', function () {
      var b = document.getElementById('wsSideNew'); if (b) b.click();
    });
  })();

  function applySideCollapsed() {
    var panelEl = document.getElementById('wsPanel');
    if (panelEl) panelEl.classList.toggle('ws-panel--collapsed', sideCollapsed);
  }

  function setSideCollapsed(v) {
    sideCollapsed = !!v;
    try { localStorage.setItem(SIDE_COLLAPSE_KEY, sideCollapsed ? '1' : '0'); } catch (e) { /* ок */ }
    var panelEl = document.getElementById('wsPanel');
    if (panelEl) {
      /* плавность включаем только на время самого сворачивания, иначе при смене
         режима чата (половина ↔ во весь экран) отступы «ехали» бы */
      panelEl.classList.add('ws-side-anim');
      clearTimeout(collapseAnimTimer);
      collapseAnimTimer = setTimeout(function () { panelEl.classList.remove('ws-side-anim'); }, 450);
    }
    applySideCollapsed();
  }

  /* подсказки у кнопок колонки — только в развёрнутом режиме (в рейке остаётся обычный title) */
  function syncSideTips() {
    [['wsSideSearch', 'Поиск по чатам'], ['wsSideToggle', 'Свернуть сайдбар']].forEach(function (p) {
      var el = document.getElementById(p[0]);
      if (!el) return;
      if (isFull) { el.removeAttribute('title'); el.setAttribute('data-tooltip', p[1]); }
      else { el.removeAttribute('data-tooltip'); el.setAttribute('title', p[1]); }
    });
  }

  (function bindSideToggle() {
    var t = document.getElementById('wsSideToggle');
    if (t) t.addEventListener('click', function () { setSideCollapsed(true); });
  })();
  applySideCollapsed();

  /* ---------- тонкая полоса прокрутки истории: только при наведении, уходит через 1,5 с ---------- */
  (function overlayScrollbar() {
    var list = document.getElementById('wsSideList');
    var side = document.getElementById('wsSide');
    if (!list || !side || document.getElementById('wsSideSb')) return;
    var sb = document.createElement('div');
    sb.className = 'ws-sb';
    sb.id = 'wsSideSb';
    var thumb = document.createElement('div');
    thumb.className = 'ws-sb-thumb';
    sb.appendChild(thumb);
    side.appendChild(sb);
    var hideTimer = null;
    var drag = null;
    var metrics = { th: 0, ch: 0, sh: 0 };

    function update() {
      var ch = list.clientHeight;
      var sh = list.scrollHeight;
      var atBottom = list.scrollTop + ch >= sh - 2;
      list.classList.toggle('ws-fade-b', sh > ch + 2 && !atBottom);
      if (!ch || sh <= ch + 1) { sb.style.display = 'none'; return; }
      sb.style.display = 'block';
      var lr = list.getBoundingClientRect();
      var sr = side.getBoundingClientRect();
      sb.style.top = (lr.top - sr.top) + 'px';
      sb.style.height = ch + 'px';
      var th = Math.max(36, ch * ch / sh);
      var top = (ch - th) * list.scrollTop / (sh - ch);
      thumb.style.height = th + 'px';
      thumb.style.transform = 'translateY(' + top + 'px)';
      metrics = { th: th, ch: ch, sh: sh };
    }
    function show() {
      clearTimeout(hideTimer);
      update();
      sb.classList.add('on');
    }
    function hideLater() {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(function () { if (!drag) sb.classList.remove('on'); }, 1500);
    }

    list.addEventListener('mouseenter', show);
    list.addEventListener('mouseleave', hideLater);
    sb.addEventListener('mouseenter', show);
    sb.addEventListener('mouseleave', hideLater);
    list.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    if (window.ResizeObserver) new ResizeObserver(update).observe(list);
    if (window.MutationObserver) new MutationObserver(update).observe(list, { childList: true });

    thumb.addEventListener('pointerdown', function (e) {
      drag = { y: e.clientY, st: list.scrollTop };
      thumb.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    thumb.addEventListener('pointermove', function (e) {
      if (!drag) return;
      var room = metrics.ch - metrics.th;
      if (room <= 0) return;
      list.scrollTop = drag.st + (e.clientY - drag.y) * (metrics.sh - metrics.ch) / room;
    });
    function endDrag() { if (drag) { drag = null; hideLater(); } }
    thumb.addEventListener('pointerup', endDrag);
    thumb.addEventListener('pointercancel', endDrag);
    update();
  })();

  /* ---------- полуэкранный режим: кнопка истории — стрелка, лупа сверху ---------- */
  (function historyArrow() {
    var h = document.getElementById('wsSideHist');
    if (!h) return;
    h.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="4"></rect><path d="M9 4v16"></path><path d="M15 9l-3 3 3 3"></path></svg>';
  })();

  function syncHistBtn() {
    var h = document.getElementById('wsSideHist');
    if (!h) return;
    var t = sideDrawer ? 'Свернуть историю' : 'История сессий';
    h.title = t;
    h.setAttribute('aria-label', t);
  }

  /* ---------- «Ход работы»: карточки инструментов в панели справа (и в полном, и в половинном режиме) ---------- */
  var ACTIVITY_PREF_KEY = 'aios.activity.pref'; /* полный экран: '1' — открыта, '0' — закрыта пользователем, нет — авто */
  var activityEl = null;
  var activityListEl = null;
  var activityBtn = null;
  var activityBadge = null;
  var activityOpen = false;      /* состояние в полноэкранном режиме (запоминается) */
  var activityOpenHalf = false;  /* состояние в половинном режиме (только на время сессии страницы) */
  var activityAnimTimer = null;
  var activityGroupEl = null;      /* текущий блок вызовов */
  var activityGroupAnchor;         /* сообщение пользователя, к которому он относится */

  (function buildActivity() {
    if (document.getElementById('wsActivity')) return;
    activityEl = document.createElement('aside');
    activityEl.className = 'ws-activity';
    activityEl.id = 'wsActivity';
    activityEl.setAttribute('aria-label', 'Ход работы');
    activityEl.innerHTML =
      '<div class="ws-activity-head"><span class="ws-activity-title">Ход работы</span>' +
      '<button class="ws-activity-x" type="button" aria-label="Скрыть панель" data-tooltip="Скрыть панель"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button></div>' +
      '<div class="ws-activity-list" id="wsActivityList"></div>' +
      '<div class="ws-activity-empty">Здесь появятся вызовы инструментов: расчёты, поиск, обращения к базам знаний.</div>';
    panel.appendChild(activityEl);
    activityListEl = activityEl.querySelector('#wsActivityList');
    activityEl.querySelector('.ws-activity-x').addEventListener('click', function () { setActivityOpen(false, true); });

    var actions = document.querySelector('.ws-head-actions');
    if (actions) {
      activityBtn = document.createElement('button');
      activityBtn.className = 'ws-btn';
      activityBtn.id = 'wsActivityBtn';
      activityBtn.type = 'button';
      activityBtn.setAttribute('aria-label', 'Ход работы');
      activityBtn.setAttribute('data-tooltip', 'Ход работы');
      activityBtn.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 6h13M8 12h13M8 18h13"></path><path d="M3 6h.01M3 12h.01M3 18h.01"></path></svg><span class="ws-activity-badge" hidden></span>';
      activityBadge = activityBtn.querySelector('.ws-activity-badge');
      actions.insertBefore(activityBtn, actions.firstChild);
      activityBtn.addEventListener('click', function () { setActivityOpen(!currentActivityOpen(), true); });
    }
    try {
      activityOpen = localStorage.getItem(ACTIVITY_PREF_KEY) === '1';
    } catch (e) { /* приватный режим */ }
    applyActivity();
  })();

  function currentActivityOpen() { return isFull ? activityOpen : activityOpenHalf; }

  function applyActivity() {
    var open = currentActivityOpen();
    panel.classList.toggle('ws-activity-open', !!open);
    var head = panel.querySelector('.ws-head');
    if (activityEl && head) activityEl.style.top = head.offsetHeight + 'px';
    if (activityBtn) activityBtn.classList.toggle('ws-btn--on', !!open);
  }

  function setActivityOpen(v, explicit) {
    if (isFull) {
      activityOpen = !!v;
      if (explicit) {
        try { localStorage.setItem(ACTIVITY_PREF_KEY, activityOpen ? '1' : '0'); } catch (e) { /* ок */ }
      }
    } else {
      activityOpenHalf = !!v;
    }
    panel.classList.add('ws-act-anim');
    clearTimeout(activityAnimTimer);
    activityAnimTimer = setTimeout(function () { panel.classList.remove('ws-act-anim'); }, 450);
    applyActivity();
  }

  function activityCount() {
    return activityListEl ? activityListEl.querySelectorAll('.ws-tool').length : 0;
  }

  function activityUpdate(added) {
    var n = activityCount();
    if (activityBadge) {
      activityBadge.textContent = n > 99 ? '99+' : String(n);
      activityBadge.hidden = n === 0;
    }
    if (activityEl) activityEl.classList.toggle('ws-activity--has', n > 0);
    if (added) {
      /* полный экран: первая карточка открывает панель сама, если пользователь её не закрывал;
         в половинном окне панель сама не выезжает — она закрыла бы часть переписки */
      var pref = null;
      try { pref = localStorage.getItem(ACTIVITY_PREF_KEY); } catch (e) { /* ок */ }
      if (isFull && !activityOpen && pref !== '0') setActivityOpen(true, false);
      if (activityListEl) activityListEl.scrollTop = activityListEl.scrollHeight;
    }
  }

  function activityClear() {
    if (activityListEl) activityListEl.innerHTML = '';
    activityGroupEl = null;
    activityGroupAnchor = undefined;
    activityTurn = 0;
    activityTurnText = '';
    activityUpdate(false);
  }

  /* Граница блоков — сообщение ПОЛЬЗОВАТЕЛЯ: и набранное текстом, и отправленное нажатием на
     вариант ответа (всё идёт через appendMessage('user', …); при восстановлении сессии — так же).
     Сообщения агента границей не считаются. */
  var activityTurn = 0;
  var activityTurnText = '';
  function activityNoteUser(text) {
    activityTurn = (activityTurn || 0) + 1;
    var t = String(text == null ? '' : text).split('\n\n[Приложенный файл:')[0].replace(/\s+/g, ' ').trim();
    var words = t.split(' ');
    activityTurnText = words.length > 6 ? words.slice(0, 6).join(' ') + '…' : t;
  }

  /* блок вызовов = всё, что агент сделал в ответ на одно сообщение пользователя;
     сверху подпись — первые слова этого сообщения, между блоками — тонкая линия */
  function activityGroupFor(turn) {
    if (activityGroupEl && activityGroupAnchor === turn) return activityGroupEl;
    var g = document.createElement('div');
    g.className = 'ws-activity-group';
    if (activityTurnText) {
      var cap = document.createElement('div');
      cap.className = 'ws-activity-group-cap';
      var who = document.createElement('b');
      who.textContent = 'Вы:';
      var txt = document.createElement('span');
      txt.textContent = activityTurnText;
      cap.appendChild(who);
      cap.appendChild(txt);
      g.appendChild(cap);
    }
    activityListEl.appendChild(g);
    activityGroupEl = g;
    activityGroupAnchor = turn;
    return g;
  }

  /* карточка вызова: всегда в панель «Ход работы», не в ленту (в любом режиме) */
  function placeToolCard(card) {
    if (!activityListEl) { messagesEl.appendChild(card); return; }
    activityGroupFor(activityTurn).appendChild(card);
    activityUpdate(true);
  }

  /* смена режима: у каждого режима своё состояние панели (открыта/закрыта) */
  (function watchActivityMode() {
    var was = panel.classList.contains('ws-panel--full');
    new MutationObserver(function () {
      var now = panel.classList.contains('ws-panel--full');
      if (now === was) return;
      was = now;
      applyActivity();
    }).observe(panel, { attributes: true, attributeFilter: ['class'] });
  })();
  window.addEventListener('resize', applyActivity);

  /* ---------- подсказки левой рейки (полуэкран): рисуются поверх, рейка их не обрезает ---------- */
  var railTipEl = null;
  var railTipSuppressed = null;
  function railTipHide() { if (railTipEl) railTipEl.classList.remove('on'); }
  function railTipShow(btn) {
    var text = btn.getAttribute('data-side-tip');
    if (!text || btn === railTipSuppressed) return;
    if (!railTipEl) {
      railTipEl = document.createElement('div');
      railTipEl.className = 'ws-rail-tip';
      document.body.appendChild(railTipEl);
    }
    railTipEl.textContent = text;
    var r = btn.getBoundingClientRect();
    railTipEl.style.left = (r.right + 8) + 'px';
    railTipEl.style.top = (r.top + r.height / 2) + 'px';
    railTipEl.classList.add('on');
  }
  document.addEventListener('mouseover', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('[data-side-tip]') : null;
    if (b) railTipShow(b);
  });
  document.addEventListener('mouseout', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('[data-side-tip]') : null;
    if (!b) return;
    if (!e.relatedTarget || !b.contains(e.relatedTarget)) { railTipHide(); if (railTipSuppressed === b) railTipSuppressed = null; }
  });
  document.addEventListener('click', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('[data-side-tip]') : null;
    if (b) { railTipSuppressed = b; railTipHide(); }
  }, true);

  /* тексты подсказок рейки: в полуэкране — свои подсказки, в полноэкранном — обычные data-tooltip */
  function syncRailTips() {
    [
      ['wsSideSearch', 'Поиск по чатам'],
      ['wsSideHist', sideDrawer ? 'Свернуть историю' : 'Развернуть историю']
    ].forEach(function (p) {
      var el = document.getElementById(p[0]);
      if (!el) return;
      if (isFull) {
        el.removeAttribute('data-side-tip');
      } else {
        el.setAttribute('data-side-tip', p[1]);
        el.removeAttribute('title');
        el.setAttribute('aria-label', p[1]);
      }
    });
    railTipHide();
  }
  syncRailTips();

  /* ws-activity-outside: в полуэкранном окне «Ход работы» закрывается нажатием в любом месте вне панели
     (это первое нажатие только закрывает панель и дальше не идёт) и по Esc */
  (function activityOutsideClose() {
    function outside(e) {
      if (isFull || !activityOpenHalf) return false;
      var t = e.target;
      if (!t || !t.closest) return false;
      return !t.closest('#wsActivity') && !t.closest('#wsActivityBtn');
    }
    ['pointerdown', 'mousedown', 'touchstart'].forEach(function (ev) {
      document.addEventListener(ev, function (e) { if (outside(e)) e.stopPropagation(); }, true);
    });
    document.addEventListener('click', function (e) {
      if (!outside(e)) return;
      e.stopPropagation();
      e.preventDefault();
      setActivityOpen(false, true);
    }, true);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !isFull && activityOpenHalf) {
        e.stopPropagation();
        setActivityOpen(false, true);
      }
    }, true);
  })();

  /* ws-rail-avatar: в рейке полуэкрана внизу эмблема пользователя (без имени);
     ws-expand-icon: кнопка перехода между режимами в полноэкранном режиме — свой значок и подсказка */
  (function railAvatarAndExpandBtn() {
    var expand = document.getElementById('wsExpandBtn');
    var origHtml = expand ? expand.innerHTML : '';
    var origTip = expand ? expand.getAttribute('data-tooltip') : '';
    var SVG_MIN = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 14h6v6"></path><path d="M20 10h-6V4"></path><path d="M14 10l7-7"></path><path d="M3 21l7-7"></path></svg>';
    var last = null;
    function sync() {
      if (expand && last !== !!isFull) {
        last = !!isFull;
        if (isFull) {
          expand.innerHTML = SVG_MIN;
          expand.setAttribute('data-tooltip', 'Свернуть в окно');
          expand.setAttribute('aria-label', 'Свернуть в окно');
        } else {
          expand.innerHTML = origHtml;
          if (origTip) expand.setAttribute('data-tooltip', origTip);
          expand.setAttribute('aria-label', 'Во весь экран');
        }
      }
      try { fillSideUser(); } catch (e) { /* профиль ещё не готов */ }
    }
    sync();
    new MutationObserver(sync).observe(panel, { attributes: true, attributeFilter: ['class'] });
  })();

  /* ws-swap-buttons: в шапке «Новый чат» стоит перед «Ходом работы» */
  (function swapHeaderButtons() {
    var nb = document.getElementById('wsNewBtn');
    var ab = document.getElementById('wsActivityBtn');
    if (nb && ab && nb.parentNode === ab.parentNode) ab.parentNode.insertBefore(nb, ab);
    if (nb) {
      nb.setAttribute('data-tooltip', 'Новый чат');
      nb.setAttribute('aria-label', 'Новый чат');
    }
  })();

  function fillSideUser() {
    var box = document.getElementById('wsSideUser');
    if (!box) return;
    var name = '';
    try {
      var u = getAuthSession() || {};
      var uu = u.user || u.profile || {};
      name = u.email || u.login || u.name || uu.email || uu.login || uu.name || '';
    } catch (e) { name = ''; }
    if (!name) {
      var pill = document.querySelector('.btn-login--user');
      if (pill) name = (pill.textContent || '').trim();
    }
    document.getElementById('wsSideUsername').textContent = name;
    document.getElementById('wsSideAvatar').textContent = name ? name.charAt(0).toUpperCase() : '';
    box.hidden = !name;
  }

  function swapAnim() {
    panel.classList.remove('ws-panel--swap');
    void panel.offsetWidth; /* перезапуск анимации */
    panel.classList.add('ws-panel--swap');
    setTimeout(function () { panel.classList.remove('ws-panel--swap'); }, 450);
  }

  function syncFull() {
    var wasFull = panel.classList.contains('ws-panel--full');
    panel.classList.toggle('ws-panel--full', !!isFull);
    if (wasFull !== !!isFull) swapAnim();
    if (isFull) sideDrawer = false;
    updateSide();
    if (isFull) { loadSide(); fillSideUser(); }
  }

  if (sideEl) {
    sideNew.addEventListener('click', function () {
      var nb = document.getElementById('wsNewBtn');
      if (nb) nb.click();
      sideDrawer = false;
      updateSide();
      setTimeout(loadSide, 500);
    });
    var sideClose = document.getElementById('wsSideClose');
    if (sideClose) sideClose.addEventListener('click', function () { sideDrawer = false; updateSide(); });
    sideHist.addEventListener('click', function () {
      sideDrawer = !sideDrawer;
      updateSide();
      if (sideDrawer) { loadSide(); fillSideUser(); }
    });
    document.addEventListener('click', function (e) {
      if (sideDrawer && !sideEl.contains(e.target)) { sideDrawer = false; updateSide(); }
    });
  }

  /* ---------- модель (задаётся администратором в админ-панели) ---------- */

  function loadModelStatus() {
    if (!window.AIOS_API || !window.AIOS_API.getRuntime) return;
    window.AIOS_API.getRuntime()
      .then(function (data) {
        if (!data) return;
        if (data.mode === 'live' && data.default_model) {
          modelLabel.textContent = 'LLM: ' + data.default_model;
        } else {
          modelLabel.textContent = 'LLM не настроена · обратитесь к администратору';
          modelLabel.classList.add('ws-model-label--off');
        }
      })
      .catch(function () {
        /* статус не критичен, но дефолтная надпись вводит в заблуждение */
        modelLabel.textContent = 'LLM: статус недоступен';
        modelLabel.classList.add('ws-model-label--off');
      });
  }

  loadModelStatus();

  /* ---------- безопасный мини-рендер markdown ----------
     Зависимостей нет: сначала экранируем весь текст целиком, затем
     поверх экранированного строим только разрешённые теги. Поддержка:
     заголовки #–####, абзацы, **жирный**, *курсив*, _курсив_,
     ~~зачёркнутый~~, `inline-код`, fenced-блоки ```, списки (в т.ч.
     вложенные по отступу), нумерованные списки, цитаты, hr, таблицы
     с выравниванием и [ссылки](url). Ссылки разрешены только
     http(s)/mailto/относительные — javascript:/data: отбрасываются,
     поэтому innerHTML остаётся безопасным. */

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function safeUrl(u) {
    var s = String(u || '').trim();
    if (!s || /\s/.test(s)) return null;
    if (/^https?:\/\//i.test(s) || /^mailto:/i.test(s)) return s;
    /* относительные ссылки: /path, ./x, ../x, #якорь, ?q */
    if (/^(\.{1,2}\/|\/|#|\?)/.test(s)) return s;
    return null;
  }

  function spanMd(s) {
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?![*\w])/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^_\w])_([^_\n]+)_(?![_\w])/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
    s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, function (all, t, u) {
      var url = safeUrl(u);
      return url
        ? '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + t + '</a>'
        : t;
    });
    return s;
  }

  function inlineMd(s) {
    return String(s).split(/(`[^`\n]+`)/g).map(function (part) {
      if (part.length > 2 && part.charAt(0) === '`' && part.charAt(part.length - 1) === '`') {
        return '<code>' + part.slice(1, -1) + '</code>';
      }
      return spanMd(part);
    }).join('');
  }

  function splitRow(row) {
    var s = row.trim().replace(/^\|/, '').replace(/\|\s*$/, '');
    return s.split('|').map(function (c) { return c.trim(); });
  }

  function parseTable(lines, start, out) {
    var head = splitRow(lines[start]);
    var marks = splitRow(lines[start + 1]);
    var aligns = head.map(function (_, k) {
      var m = marks[k] || '';
      var left = m.charAt(0) === ':';
      var right = m.charAt(m.length - 1) === ':';
      if (left && right) return ' style="text-align:center"';
      if (right) return ' style="text-align:right"';
      return '';
    });
    var html = '<table><thead><tr>';
    head.forEach(function (c, k) {
      html += '<th' + aligns[k] + '>' + inlineMd(c) + '</th>';
    });
    html += '</tr></thead><tbody>';
    var i = start + 2;
    while (i < lines.length && lines[i].indexOf('|') !== -1 && /\S/.test(lines[i]) && !/^```/.test(lines[i])) {
      var row = splitRow(lines[i]);
      html += '<tr>';
      head.forEach(function (_, k) {
        html += '<td' + aligns[k] + '>' + inlineMd(row[k] || '') + '</td>';
      });
      html += '</tr>';
      i++;
    }
    html += '</tbody></table>';
    out.push(html);
    return i;
  }

  function buildList(items, pos, indent) {
    var ordered = items[pos].ordered;
    var html = ordered ? '<ol>' : '<ul>';
    var i = pos;
    while (i < items.length && items[i].indent >= indent) {
      if (items[i].indent > indent) {
        /* вложенный список пришиваем к предыдущему li */
        var sub = buildList(items, i, items[i].indent);
        html = html.replace(/<\/li>\s*$/, sub.html + '</li>');
        i = sub.next;
        continue;
      }
      html += '<li>' + inlineMd(items[i].text) + '</li>';
      i++;
    }
    html += ordered ? '</ol>' : '</ul>';
    return { html: html, next: i };
  }

  function parseList(lines, start, out) {
    var items = [];
    var i = start;
    while (i < lines.length) {
      var m = lines[i].match(/^([ \t]*)([-*+]|\d+[.)])[ \t]+(.*)$/);
      if (m) {
        items.push({
          indent: m[1].replace(/\t/g, '  ').length,
          ordered: /^\d/.test(m[2]),
          text: m[3]
        });
        i++;
        continue;
      }
      if (items.length && /^[ \t]+\S/.test(lines[i])) {
        /* строка-продолжение предыдущего пункта */
        items[items.length - 1].text += '<br>' + lines[i].replace(/^[ \t]+/, '');
        i++;
        continue;
      }
      break;
    }
    if (!items.length) return start;
    out.push(buildList(items, 0, items[0].indent).html);
    return i;
  }

  function renderBlocks(lines) {
    var out = [];
    var para = [];

    function flushPara() {
      if (para.length) {
        out.push('<p>' + para.map(inlineMd).join('<br>') + '</p>');
        para = [];
      }
    }

    var i = 0;
    while (i < lines.length) {
      var line = lines[i];

      var fence = line.match(/^```[ \t]*([^\s`]*)[ \t]*$/);
      if (fence) {
        flushPara();
        var buf = [];
        i++;
        while (i < lines.length && !/^```[ \t]*$/.test(lines[i])) {
          buf.push(lines[i]);
          i++;
        }
        if (i < lines.length) i++; /* закрывающий ``` */
        /* протокол быстрых ответов не показываем как код — его съест renderChoices */
        if (fence[1] !== 'aios-choices' && fence[1] !== 'aios-choices-multi') {
          out.push('<pre class="ws-code"><code>' + buf.join('\n') + '</code></pre>');
        }
        continue;
      }

      var h = line.match(/^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$/);
      if (h) {
        flushPara();
        var lvl = Math.min(h[1].length, 4);
        out.push('<h' + lvl + '>' + inlineMd(h[2]) + '</h' + lvl + '>');
        i++;
        continue;
      }

      if (/^[ \t]*([-*_])[ \t]*(?:[ \t]*\1[ \t]*){2,}$/.test(line)) {
        flushPara();
        out.push('<hr>');
        i++;
        continue;
      }

      if (/^[ \t]*&gt;/.test(line)) {
        flushPara();
        var q = [];
        while (i < lines.length && /^[ \t]*&gt;/.test(lines[i])) {
          q.push(lines[i].replace(/^[ \t]*&gt;[ \t]?/, ''));
          i++;
        }
        out.push('<blockquote>' + renderBlocks(q) + '</blockquote>');
        continue;
      }

      if (
        line.indexOf('|') !== -1 &&
        /^[ \t]*\|?.*\|[ \t]*$/.test(line) &&
        i + 1 < lines.length &&
        lines[i + 1].indexOf('-') !== -1 &&
        /^[ \t]*\|?[ \t:|-]+$/.test(lines[i + 1])
      ) {
        flushPara();
        i = parseTable(lines, i, out);
        continue;
      }

      if (/^[ \t]*[-*+][ \t]+/.test(line) || /^[ \t]*\d+[.)][ \t]+/.test(line)) {
        flushPara();
        i = parseList(lines, i, out);
        continue;
      }

      if (!line.trim()) {
        flushPara();
        i++;
        continue;
      }

      para.push(line);
      i++;
    }
    flushPara();
    return out.join('\n');
  }

  /* Вложения в тексте (markdown-ссылки, чипы, file_card) требуют
     Bearer-токена: наполняются через fetch → Blob URL в agent_views.js. */
  function hydrateMedia(root) {
    if (window.AiosViews && window.AiosViews.hydrateFileMedia) {
      window.AiosViews.hydrateFileMedia(root);
    }
  }

  function renderMarkdown(src) {
    var raw = String(src == null ? '' : src);
    return renderBlocks(escapeHtml(raw).split(/\r?\n/));
  }

  /* ---------- красивые карточки для ```json от навыков ---------- */

  var JSON_FIELD_LABELS = {
    id: 'ID', project_id: 'ID проекта', rag_id: 'ID базы', ontology_id: 'ID онтологии',
    hypotheses_id: 'ID таблицы', author_role: 'Роль автора', status: 'Статус',
    title: 'Название', project_title: 'Название проекта', name: 'Название',
    description: 'Описание', domain: 'Область', goal: 'Цель', author: 'Автор',
    tags: 'Теги', statement: 'Формулировка', users: 'Пользователи',
    decision: 'Решение', as_is: 'Как сейчас', inputs: 'Входы', outputs: 'Выходы',
    check_method: 'Способ проверки', success_metric: 'Метрика успеха',
    falsifier: 'Опровержение', horizon: 'Срок', depends_on: 'Зависит от',
    reuse_of: 'Переиспользование', risks: 'Риски', team_size: 'Размер команды',
    participants: 'Участники', departments: 'Подразделения', budget: 'Бюджет',
    groundwork: 'Задел', groundwork_links: 'Ссылки на задел',
    groundwork_files: 'Файлы задела', presentation: 'Есть презентация',
    presentation_files: 'Файлы презентации', corpus_types: 'Типы документов',
    corpus_volume: 'Объём корпуса', corpus_format: 'Формат корпуса',
    access_restrictions: 'Ограничения доступа', sensitive: 'Чувствительные данные',
    typical_queries: 'Типовые вопросы', citation_required: 'Ссылки обязательны',
    owner: 'Владелец', update_frequency: 'Частота обновления',
    competency_questions: 'Компетентностные вопросы', classes: 'Классы',
    relations: 'Связи', question: 'Вопрос', coverage: 'Покрытие',
    parent: 'Родитель', attributes: 'Атрибуты', synonyms: 'Синонимы',
    keywords: 'Ключевые слова'
  };

  function humanizeJsonKey(key) {
    if (Object.prototype.hasOwnProperty.call(JSON_FIELD_LABELS, key)) return JSON_FIELD_LABELS[key];
    var s = String(key).replace(/_/g, ' ');
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function isEmptyJsonValue(v) {
    if (v === null || v === undefined) return true;
    if (typeof v === 'string') return v.trim() === '';
    if (Array.isArray(v)) return v.length === 0;
    if (typeof v === 'object') return Object.keys(v).length === 0;
    return false;
  }

  function renderJsonScalar(v) {
    if (typeof v === 'boolean') return v ? 'да' : 'нет';
    return escapeHtml(String(v));
  }

  function renderJsonFields(obj) {
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return '';
    var rows = [];
    Object.keys(obj).forEach(function (key) {
      var v = obj[key];
      if (isEmptyJsonValue(v)) return;
      var label = humanizeJsonKey(key);
      var valueHtml, block = false;
      if (Array.isArray(v)) {
        if (v.every(function (x) { return !x || typeof x !== 'object'; })) {
          valueHtml = '<ul class="ws-json-list">' + v.map(function (x) {
            return '<li>' + renderJsonScalar(x) + '</li>';
          }).join('') + '</ul>';
        } else {
          block = true;
          valueHtml = v.map(function (item) {
            var inner = (item && typeof item === 'object') ? renderJsonFields(item) : renderJsonScalar(item);
            return '<div class="ws-json-item">' + inner + '</div>';
          }).join('');
        }
      } else if (typeof v === 'object') {
        block = true;
        valueHtml = renderJsonFields(v);
      } else {
        valueHtml = renderJsonScalar(v);
      }
      if (!valueHtml) return;
      rows.push(
        '<div class="ws-json-row' + (block ? ' ws-json-row--block' : '') + '">' +
        '<div class="ws-json-key">' + escapeHtml(label) + '</div>' +
        '<div class="ws-json-val">' + valueHtml + '</div>' +
        '</div>'
      );
    });
    return rows.join('');
  }

  function sanitizeSvg(raw) {
    /* Вырезаем исполняемый контент перед вставкой как innerHTML: сам
       chart_generate такого не генерирует, но блок ```svg-chart в тексте
       ответа не имеет иной защиты, кроме этой (языковая метка fence
       никуда не сохраняется, см. renderBlocks выше) — считаем содержимое
       непроверенным по умолчанию. */
    return raw
      .replace(/<script[\s\S]*?<\/script>/gi, '')
      .replace(/\son\w+\s*=\s*"[^"]*"/gi, '')
      .replace(/\son\w+\s*=\s*'[^']*'/gi, '')
      .replace(/\bxlink:href\s*=\s*"(?!#)[^"]*"/gi, '')
      .replace(/\bhref\s*=\s*"(?!#)[^"]*"/gi, '');
  }

  function enhanceChartBlocks(root) {
    if (!root || !root.querySelectorAll) return;
    var codes = root.querySelectorAll('pre.ws-code > code');
    Array.prototype.forEach.call(codes, function (code) {
      var pre = code.parentNode;
      if (!pre || !pre.parentNode) return;
      var raw = code.textContent || '';
      var trimmed = raw.trim();
      if (!/^<svg[\s>][\s\S]*<\/svg>\s*$/i.test(trimmed)) return;
      var safe = sanitizeSvg(trimmed);
      var card = document.createElement('div');
      card.className = 'ws-chart-card';
      card.innerHTML = safe;
      var svgEl = card.querySelector('svg');
      if (!svgEl) return; /* санитизация повредила структуру — не показываем как график */
      svgEl.setAttribute('style', 'max-width:100%;height:auto;');
      pre.parentNode.replaceChild(card, pre);
    });

    /* Блоки aios-view — компоненты ответа агента (agent_views.js): расчёт,
       сравнение вариантов, таблица, паспорт. Вызывается здесь, а не на
       каждой площадке отдельно: enhanceChartBlocks уже дёргается из всех
       четырёх мест, где в DOM попадает текст модели. */
    if (window.AiosViews) window.AiosViews.enhance(root);
  }

  function enhanceJsonBlocks(root) {
    if (!root || !root.querySelectorAll) return;
    var codes = root.querySelectorAll('pre.ws-code > code');
    Array.prototype.forEach.call(codes, function (code) {
      var pre = code.parentNode;
      if (!pre || !pre.parentNode) return;
      var raw = code.textContent || '';
      var trimmed = raw.trim();
      if (!trimmed || trimmed.charAt(0) !== '{') return;
      var data;
      try {
        data = JSON.parse(trimmed);
      } catch (e) {
        return;
      }
      if (!data || typeof data !== 'object' || Array.isArray(data)) return;
      /* Блок aios-view — не «технический JSON», а компонент ответа агента
         (расчёт с формулой, вердикт, сравнение). Его рисует AiosViews чуть
         ниже, в enhanceChartBlocks. Проверка нужна потому, что этот
         обработчик вызывается ПЕРВЫМ во всех пяти местах и забирает любой
         блок, начинающийся с «{»: в рабочем чате компоненты агента из-за
         этого не отрисовывались никогда — вместо расчёта человек видел
         список «Label / Value / Статус». */
      if (data.component && window.AiosViews &&
          window.AiosViews.components.indexOf(data.component) !== -1) return;
      var fieldsHtml = renderJsonFields(data);
      if (!fieldsHtml) return;
      var card = document.createElement('div');
      card.className = 'ws-json-card';
      var fields = document.createElement('div');
      fields.className = 'ws-json-fields';
      fields.innerHTML = fieldsHtml;
      card.appendChild(fields);
      var details = document.createElement('details');
      details.className = 'ws-json-raw';
      var summary = document.createElement('summary');
      summary.textContent = 'Технический вид (JSON)';
      details.appendChild(summary);
      var rawPre = document.createElement('pre');
      rawPre.className = 'ws-code';
      var rawCode = document.createElement('code');
      rawCode.textContent = trimmed;
      rawPre.appendChild(rawCode);
      details.appendChild(rawPre);
      var copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'ws-json-copy';
      copyBtn.textContent = 'Копировать';
      copyBtn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(trimmed).then(function () {
            copyBtn.textContent = 'Скопировано';
            setTimeout(function () { copyBtn.textContent = 'Копировать'; }, 1500);
          });
        }
      });
      details.appendChild(copyBtn);
      card.appendChild(details);
      pre.parentNode.replaceChild(card, pre);
    });
  }

  /* ---------- сообщения ---------- */

  function scrollDown() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function appendMessage(role, text, skill, attachments) {
    if (role === 'user') activityNoteUser(text);
    var msg = document.createElement('div');
    msg.className = 'ws-msg ws-msg--' + role;
    if (role === 'user' && skill) {
      var tag = document.createElement('span');
      tag.className = 'ws-msg-skill';
      tag.textContent = '⚡ ' + (skill.name || skill.skill_id);
      msg.appendChild(tag);
    }
    var textEl = document.createElement('span');
    textEl.className = 'ws-msg-text';
    var rawText = String(text == null ? '' : text);
    if (role === 'user') {
      /* служебный блок вложений (подмешивается сервером для модели) в
         истории не показываем — файлы отрисованы отдельными чипами */
      rawText = rawText.split('\n\n[Приложенный файл:')[0];
    }
    var bubble = { root: msg, text: textEl, raw: rawText };
    if (role === 'assistant') {
      /* ответы ассистента рисуем безопасным markdown-рендером */
      msg.classList.add('ws-msg--md');
      textEl.innerHTML = renderMarkdown(bubble.raw);
      enhanceJsonBlocks(textEl);
      enhanceChartBlocks(textEl);
      hydrateMedia(textEl);
    } else {
      textEl.textContent = bubble.raw;
    }
    (attachments || []).forEach(function (att) {
      if (!att || !att.filename) return;
      var chip = document.createElement('a');
      chip.className = 'ws-attach-chip ws-attach-file';
      chip.textContent = '📎 ' + att.filename;
      chip.title = 'Скачать файл';
      if (att.attachment_id && !att.project_file) {
        chip.href = '/api/chat/attachments/' + encodeURIComponent(att.attachment_id);
      } else if (att.download_url) {
        chip.href = att.download_url;
      } else {
        chip.href = '#';
      }
      chip.target = '_blank';
      msg.appendChild(chip);
    });
    hydrateMedia(msg);
    msg.appendChild(textEl);
    messagesEl.appendChild(msg);
    scrollDown();
    return bubble;
  }

  function appendStreamingMessage(userText) {
    var msg = document.createElement('div');
    msg.className = 'ws-msg ws-msg--assistant';
    var text = document.createElement('span');
    text.className = 'ws-msg-text';
    var cursor = document.createElement('span');
    cursor.className = 'ws-cursor';
    msg.appendChild(text);
    msg.appendChild(cursor);
    /* пока ответа нет — вместо пустого пузыря с курсором колба с подписью
       («Думаю..» и родственные формулировки), без фона сообщения; первый
     же текстовый токен меняет кадр на обычный пузырь */
    var think = window.AiosThinking ? window.AiosThinking.create(userText) : null;
    if (think) {
      msg.classList.add('ws-msg--thinking');
      cursor.style.display = 'none';
      msg.appendChild(think.el);
    }
    messagesEl.appendChild(msg);
    scrollDown();

    function beginText() {
      if (!msg.classList.contains('ws-msg--thinking')) return;
      msg.classList.remove('ws-msg--thinking');
      cursor.style.display = '';
      if (think) {
        think.stop();
        think.el.remove();
        think = null;
      }
    }

    return { root: msg, text: text, cursor: cursor, beginText: beginText,
             raw: '', mdTimer: null, mdPending: false, mdShown: false };
  }

  /* Потоковый показ ответа: сырое значение копим в bubble.raw, на экран
     выносим безопасный markdown не чаще раза в 120 мс (самая первая
     дельта — сразу). Пользовательские сообщения остаются простым текстом. */
  var MD_RENDER_MS = 120;

  function flushMd(bubble) {
    bubble.mdShown = true;
    bubble.root.classList.add('ws-msg--md');
    bubble.text.innerHTML = renderMarkdown(String(bubble.raw || '').replace(CHOICES_RE, '').replace(CHOICES_MULTI_RE, ''));
    enhanceJsonBlocks(bubble.text);
      enhanceChartBlocks(bubble.text);
      hydrateMedia(bubble.text);
    scrollDown();
  }

  function scheduleMdRender(bubble) {
    if (bubble.mdTimer) {
      bubble.mdPending = true;
      return;
    }
    bubble.mdTimer = setTimeout(function () {
      bubble.mdTimer = null;
      flushMd(bubble);
      if (bubble.mdPending) {
        bubble.mdPending = false;
        scheduleMdRender(bubble);
      }
    }, bubble.mdShown ? MD_RENDER_MS : 0);
  }

  function appendBubbleText(bubble, t) {
    if (t && !bubble.raw && bubble.beginText) bubble.beginText();
    bubble.raw += t;
    scheduleMdRender(bubble);
  }

  function pushError(bubble, msg) {
    if (bubble.beginText) bubble.beginText();
    bubble.raw = (bubble.raw ? bubble.raw + '\n' : '') + '⚠ ' + msg;
    if (bubble.mdTimer) {
      clearTimeout(bubble.mdTimer);
      bubble.mdTimer = null;
      bubble.mdPending = false;
    }
    flushMd(bubble);
  }

  /* Человеческое название инструмента: русское из общего словаря
     (api.js, window.AIOS_TOOL_TITLES), для неизвестного — техническое
     имя как есть. Техническое имя остаётся подсказкой при наведении. */
  function toolTitle(name) {
    return (window.AIOS_TOOL_TITLES && window.AIOS_TOOL_TITLES[name]) || name;
  }

  /* Карточка вызова инструмента */
  function appendChartCard(svgText) {
    var trimmed = String(svgText || '').trim();
    if (!/^<svg[\s>][\s\S]*<\/svg>\s*$/i.test(trimmed)) return null;
    var safe = sanitizeSvg(trimmed);
    var card = document.createElement('div');
    card.className = 'ws-chart-card';
    card.style.cssText = 'background:#fff;border-radius:10px;padding:10px;margin:8px 0;max-width:720px;';
    card.innerHTML = safe;
    var svgEl = card.querySelector('svg');
    if (!svgEl) return null;
    svgEl.setAttribute('style', 'max-width:100%;height:auto;');
    var dl = document.createElement('a');
    dl.textContent = 'Скачать SVG';
    dl.download = 'chart.svg';
    dl.href = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(safe);
    dl.style.cssText = 'display:inline-block;margin-top:6px;font-size:12px;color:#2563eb;';
    card.appendChild(dl);
    messagesEl.appendChild(card);
    return card;
  }

  function appendToolCard(name, args) {
    var card = document.createElement('div');
    card.className = 'ws-tool';

    var head = document.createElement('button');
    head.type = 'button';
    head.className = 'ws-tool-head';
    head.setAttribute('aria-expanded', 'false');
    head.title = name;
    var title = document.createElement('span');
    title.className = 'ws-tool-name';
    title.textContent = '⚙ ' + toolTitle(name);
    var chev = document.createElement('span');
    chev.className = 'ws-tool-chev';
    chev.textContent = '▾';
    head.appendChild(title);
    head.appendChild(chev);

    var body = document.createElement('div');
    body.className = 'ws-tool-body';
    body.hidden = true;
    var pre = document.createElement('pre');
    try {
      pre.textContent = 'Аргументы:\n' + JSON.stringify(args, null, 2);
    } catch (e) {
      pre.textContent = String(args);
    }
    body.appendChild(pre);

    head.addEventListener('click', function () {
      body.hidden = !body.hidden;
      head.setAttribute('aria-expanded', body.hidden ? 'false' : 'true');
    });

    card.appendChild(head);
    card.appendChild(body);
    placeToolCard(card);
    return { root: card, body: body, pre: pre, head: head };
  }

  function toolResultText(result) {
    if (typeof result === 'string') return result;
    try {
      return JSON.stringify(result, null, 2);
    } catch (e) {
      return String(result);
    }
  }

  /* Ошибка вызова определяется не только флагом события, но и содержимым результата:
     инструменты отвечают {"ok": false, "error": "…"}, а при восстановлении сессии флага нет вовсе */
  function toolFailed(ev) {
    if (ev && ev.ok === false) return true;
    var r = ev && ev.result;
    if (typeof r === 'string') {
      try { r = JSON.parse(r); } catch (e) { return false; }
    }
    return !!(r && typeof r === 'object' && !Array.isArray(r) && (r.ok === false || (r.error && r.ok !== true)));
  }

  function fillToolCard(card, ev) {
    card.root.classList.add(toolFailed(ev) ? 'ws-tool--err' : 'ws-tool--ok');
    var _b = document.querySelector('.ws-activity-badge');
    if (_b) _b.classList.toggle('ws-activity-badge--err', !!document.querySelector('#wsActivityList .ws-tool--err'));
    var text = toolResultText(ev.result);
    if (text.length > 4000) text = text.slice(0, 4000) + '\n…';
    card.pre.textContent = text;
  }

  /* ---------- карточки подтверждения действий (human-in-the-loop) ---------- */

  /* Действия с побочными эффектами (запись файлов, почта, сохранение
     карточек) сервер исполняет только после нажатия кнопки: цикл агента
     останавливается на approval_request и ждёт решения (без ответа —
     автоотказ). Решение уходит POST /api/chat/approvals, итог приходит
     событием approval_result. */
  var APPROVAL_DECISION_LABELS = {
    approve: 'утверждено',
    decline: 'отклонено',
    declined_by_timeout: 'истёк срок ожидания — отказ'
  };

  function appendApprovalCard(ev) {
    var card = document.createElement('div');
    card.className = 'ws-approval';
    card.setAttribute('data-approval-id', ev.id);

    var title = document.createElement('div');
    title.className = 'ws-approval-title';
    title.textContent = 'Подтвердите действие агента: ' + toolTitle(ev.name);
    title.title = ev.name;

    var args = document.createElement('pre');
    args.className = 'ws-approval-args';
    try {
      args.textContent = JSON.stringify(ev.arguments, null, 2);
    } catch (e) {
      args.textContent = String(ev.arguments);
    }

    var row = document.createElement('div');
    row.className = 'ws-approval-row';
    var okBtn = document.createElement('button');
    okBtn.type = 'button';
    okBtn.className = 'ws-approval-btn ws-approval-btn--ok';
    okBtn.textContent = 'Утвердить';
    var noBtn = document.createElement('button');
    noBtn.type = 'button';
    noBtn.className = 'ws-approval-btn ws-approval-btn--no';
    noBtn.textContent = 'Отклонить';
    var hint = document.createElement('span');
    hint.className = 'ws-approval-hint';

    var left = Math.max(1, Math.round(ev.timeout || 55));
    hint.textContent = 'без ответа — автоотказ через ' + left + ' с';
    var timer = setInterval(function () {
      left -= 1;
      if (left <= 0) {
        clearInterval(timer);
        timer = null;
        return;
      }
      hint.textContent = 'без ответа — автоотказ через ' + left + ' с';
    }, 1000);

    function decide(decision) {
      if (timer) { clearInterval(timer); timer = null; }
      okBtn.disabled = true;
      noBtn.disabled = true;
      hint.textContent = 'решение отправляется…';
      window.AIOS_API.resolveApproval(getSessionId(), ev.id, decision)
        .then(function (res) {
          /* итоговую отметку поставит событие approval_result;
             409 (запрос уже закрыт, например таймаутом) — не ошибка */
          hint.textContent = (res && res.conflict)
            ? 'запрос уже закрыт'
            : 'решение отправлено';
        })
        .catch(function (err) {
          hint.textContent = (err && err.message) || 'не удалось отправить решение';
          okBtn.disabled = false;
          noBtn.disabled = false;
        });
    }

    okBtn.addEventListener('click', function () { decide('approve'); });
    noBtn.addEventListener('click', function () { decide('decline'); });

    row.appendChild(okBtn);
    row.appendChild(noBtn);
    row.appendChild(hint);
    card.appendChild(title);
    card.appendChild(args);
    card.appendChild(row);
    messagesEl.appendChild(card);
    scrollDown();
    return {
      root: card,
      okBtn: okBtn,
      noBtn: noBtn,
      hint: hint,
      stopTimer: function () {
        if (timer) { clearInterval(timer); timer = null; }
      }
    };
  }

  function settleApprovalCard(card, decision) {
    if (!card) return;
    card.stopTimer();
    card.okBtn.disabled = true;
    card.noBtn.disabled = true;
    var label = APPROVAL_DECISION_LABELS[decision] || decision;
    card.hint.textContent = 'Решение: ' + label;
    card.root.classList.add(decision === 'approve' ? 'ws-approval--ok' : 'ws-approval--no');
  }

  /* ---------- следующий этап конвейера (карта data/stage-map.json) ---------- */

  /* Сессия навыка знает свой этап конвейера (событие session, поле stage).
     Когда обмен завершён, предлагаем следующий этап карты — его первый
     навык — одной кнопкой: продолжение пайплайна без поиска по каталогу.
     Чип отключён 23.09.2026 по решению пользователя (некорректная работа);
     вернуть — выставить true после починки и поднять кэш-бастер. */
  var STAGE_NEXT_ENABLED = false;

  var STAGE_MAP = null;
  var STAGE_MAPPromise = null;
  var sessionStage = null; /* этап текущей сессии; сбрасывается на новом чате */

  function stageMap() {
    if (STAGE_MAP) return Promise.resolve(STAGE_MAP);
    if (!STAGE_MAPPromise && window.AIOS_API && window.AIOS_API.getStageMap) {
      STAGE_MAPPromise = window.AIOS_API.getStageMap().then(function (d) {
        STAGE_MAP = (d && d.stages) || [];
        return STAGE_MAP;
      }).catch(function () {
        STAGE_MAP = [];
        return STAGE_MAP;
      });
    }
    return STAGE_MAPPromise || Promise.resolve([]);
  }

  function maybeSuggestNextStage() {
    if (!STAGE_NEXT_ENABLED) return;
    if (!sessionStage) return;
    if (messagesEl.querySelector('.ws-stage-next')) return;
    stageMap().then(function (stages) {
      var cur = null;
      var next = null;
      stages.forEach(function (s) {
        if (s.key === sessionStage) cur = s;
        if (cur && s.key === cur.next) next = s;
      });
      if (!next || !(next.skills || []).length) return;
      return skillsCatalog().then(function (items) {
        var skill = null;
        items.forEach(function (x) { if (x.id === next.skills[0]) skill = x; });
        if (!skill) return;
        var row = document.createElement('div');
        row.className = 'ws-stage-next';
        var note = document.createElement('span');
        note.className = 'ws-stage-next-note';
        note.textContent = 'Этап «' + (cur.name_ru || cur.key) + '» завершён.';
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'ws-stage-next-btn';
        btn.textContent = '▶ Следующий этап: «' + (skill.name || skill.id) + '»';
        btn.addEventListener('click', function () {
          row.remove();
          launchSkill(skill, '');
        });
        row.appendChild(note);
        row.appendChild(btn);
        messagesEl.appendChild(row);
        scrollDown();
      });
    });
  }

  /* ---------- протокол быстрых ответов (```aios-choices) ---------- */

  var CHOICES_RE = /```aios-choices[ \t]*\r?\n([\s\S]*?)```/g;

  /* Модель оформляет варианты fenced-блоком aios-choices; после завершения
     ответа убираем блок из текста (он дублирует кнопки) и рисуем кнопки.
     Нажатие отправляет текст варианта обычным сообщением пользователя. */
  function renderChoices(bubble) {
    if (!bubble || !bubble.text) return;
    var text = bubble.raw != null ? String(bubble.raw) : (bubble.text.textContent || '');
    if (text.indexOf('aios-choices') === -1) return;
    var options = [];
    text = text.replace(CHOICES_RE, function (all, body) {
      String(body || '').split(/\r?\n/).forEach(function (line) {
        line = line.trim().replace(/^[-*•]\s+/, '');
        if (line) options.push(line);
      });
      return '';
    });
    if (!options.length) return;
    bubble.raw = text.replace(/\n{3,}/g, '\n\n').trim();
    bubble.root.classList.add('ws-msg--md');
    bubble.text.innerHTML = renderMarkdown(bubble.raw);
    enhanceJsonBlocks(bubble.text);
      enhanceChartBlocks(bubble.text);
      hydrateMedia(bubble.text);
    var wrap = document.createElement('div');
    wrap.className = 'ws-choices';
    options.slice(0, 8).forEach(function (opt) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ws-choice-btn';
      btn.textContent = opt;
      btn.addEventListener('click', function () {
        Array.prototype.forEach.call(wrap.querySelectorAll('button'), function (b) {
          b.disabled = true;
        });
        /* вариант, предлагающий навык («Вызвать навык "Формулировка
           гипотез"»), запускает навык: новая сессия и автостарт
           пайплайна — без подтверждений и без ручного подключения */
        skillsCatalog().then(function (items) {
          var intent = findLaunchIntent(opt, items);
          if (intent) { launchSkill(intent, ''); return; }
          send(opt);
        });
      });
      wrap.appendChild(btn);
    });
    bubble.root.appendChild(wrap);
    scrollDown();
  }

  var CHOICES_MULTI_RE = /```aios-choices-multi[ \t]*\r?\n([\s\S]*?)```/g;

  /* Множественный выбор: тот же формат тела, что у aios-choices (по
     варианту на строку), но чекбоксы вместо кнопок и кнопка «Готово»,
     которая одним сообщением отправляет список отмеченных вариантов.
     Запуск навыка через findLaunchIntent не поддерживается — это только
     для одиночного aios-choices. */
  function renderChoicesMulti(bubble) {
    if (!bubble || !bubble.text) return;
    var text = bubble.raw != null ? String(bubble.raw) : (bubble.text.textContent || '');
    if (text.indexOf('aios-choices-multi') === -1) return;
    var options = [];
    text = text.replace(CHOICES_MULTI_RE, function (all, body) {
      String(body || '').split(/\r?\n/).forEach(function (line) {
        line = line.trim().replace(/^[-*\u2022]\s+/, '');
        if (line) options.push(line);
      });
      return '';
    });
    if (!options.length) return;
    bubble.raw = text.replace(/\n{3,}/g, '\n\n').trim();
    bubble.root.classList.add('ws-msg--md');
    bubble.text.innerHTML = renderMarkdown(bubble.raw);
    enhanceJsonBlocks(bubble.text);
      enhanceChartBlocks(bubble.text);
      hydrateMedia(bubble.text);

    var wrap = document.createElement('div');
    wrap.className = 'ws-choices ws-choices--multi';

    var hint = document.createElement('div');
    hint.className = 'ws-choices-hint';
    hint.textContent = 'Можно выбрать несколько';
    wrap.appendChild(hint);

    var list = document.createElement('div');
    list.className = 'ws-choices-list';
    var checked = {};
    options.slice(0, 12).forEach(function (opt, idx) {
      var label = document.createElement('label');
      label.className = 'ws-choice-check';
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.addEventListener('change', function () {
        if (box.checked) { checked[idx] = opt; } else { delete checked[idx]; }
        doneBtn.disabled = !Object.keys(checked).length;
      });
      var span = document.createElement('span');
      span.textContent = opt;
      label.appendChild(box);
      label.appendChild(span);
      list.appendChild(label);
    });
    wrap.appendChild(list);

    var doneBtn = document.createElement('button');
    doneBtn.type = 'button';
    doneBtn.className = 'ws-choice-done';
    doneBtn.textContent = 'Готово';
    doneBtn.disabled = true;
    doneBtn.addEventListener('click', function () {
      Array.prototype.forEach.call(wrap.querySelectorAll('input,button'), function (el) {
        el.disabled = true;
      });
      var picked = Object.keys(checked)
        .sort(function (a, b) { return a - b; })
        .map(function (k) { return checked[k]; });
      send(picked.map(function (o) { return '- ' + o; }).join('\n'));
    });
    wrap.appendChild(doneBtn);

    bubble.root.appendChild(wrap);
    scrollDown();
  }

  /* Карточка проекта сохранена — короткая ссылка на страницу «Проекты» */
  function appendProjectsAction() {
    var row = document.createElement('div');
    row.className = 'ws-projects-action';
    var link = document.createElement('a');
    link.className = 'ws-projects-link';
    link.href = 'projects.html';
    link.textContent = 'Открыть «Проекты» →';
    row.appendChild(link);
    messagesEl.appendChild(row);
    scrollDown();
  }

  /* Онтология опубликована (save_ontology, status=active) — кнопка на 3D */
  function appendOntologyAction(result) {
    var row = document.createElement('div');
    row.className = 'ws-projects-action';
    var link = document.createElement('a');
    link.className = 'ws-projects-link';
    var id = result && result.ontology_id ? result.ontology_id : '';
    link.href = id ? 'ontology.html?id=' + encodeURIComponent(id) : 'ontology.html';
    link.textContent = 'Смотреть онтологию в 3D →';
    row.appendChild(link);
    messagesEl.appendChild(row);
    scrollDown();
  }

  /* ---------- сессия: восстановление и новый диалог ---------- */

  var pendingWatch = null; // {cancelled, timer} — поллинг недописанного в фоне turn'а

  function cancelPendingWatch() {
    if (pendingWatch) {
      pendingWatch.cancelled = true;
      if (pendingWatch.timer) clearTimeout(pendingWatch.timer);
      pendingWatch = null;
    }
  }

  /* Последнее сообщение сессии — от пользователя, ответа ещё нет: turn мог
     уйти в фон при обрыве соединения (см. detach_background_turn на
     бэкенде, до 10 минут). Показываем тот же «печатает» индикатор, что и
     при обычной генерации, и опрашиваем сессию, пока ответ не допишется —
     чтобы не требовать от человека ручного обновления страницы. */
  function watchPendingReply(sessionId, knownCount) {
    cancelPendingWatch();
    var bubble = appendStreamingMessage();
    var state = { cancelled: false, timer: null };
    pendingWatch = state;
    var attempts = 0;
    var MAX_ATTEMPTS = 200; // ~10 минут при интервале 3с — совпадает с
                             // BACKGROUND_TURN_TIMEOUT на бэкенде

    function stop(text) {
      if (state.cancelled) return;
      if (bubble.beginText) bubble.beginText();
      bubble.cursor.remove();
      if (text) bubble.text.textContent = text;
      else bubble.root.remove();
    }

    function poll() {
      if (state.cancelled) return;
      attempts += 1;
      window.AIOS_API.getSession(sessionId).then(function (s) {
        if (state.cancelled) return;
        var messages = s.messages || [];
        if (messages.length > knownCount) {
          stop();
          restoreSession();
          return;
        }
        if (attempts >= MAX_ATTEMPTS) {
          stop('Генерация идёт дольше обычного — обновите страницу чуть позже.');
          return;
        }
        state.timer = setTimeout(poll, 3000);
      }).catch(function () {
        if (state.cancelled || attempts >= MAX_ATTEMPTS) return;
        state.timer = setTimeout(poll, 3000);
      });
    }

    state.timer = setTimeout(poll, 3000);
  }

  function restoreSession() {
    var sessionId = getSessionId();
    if (!sessionId || !window.AIOS_API || !window.AIOS_API.getSession) {
      return Promise.resolve(false);
    }
    return window.AIOS_API.getSession(sessionId)
      .then(function (s) {
        clearTranscript();
        var cards = {};
        (s.messages || []).forEach(function (m) {
          if (m.role === 'user') {
            appendMessage('user', m.content || '', null, m.attachments);
          } else if (m.role === 'assistant') {
            (m.tool_calls || []).forEach(function (tc) {
              var args = {};
              try {
                args = JSON.parse((tc.function && tc.function.arguments) || '{}');
              } catch (e) { /* показываем как есть */ }
              cards[tc.id] = appendToolCard((tc.function && tc.function.name) || 'tool', args);
            });
            if (m.content) {
              var restoredBubble = appendMessage('assistant', m.content);
              renderChoices(restoredBubble);
              renderChoicesMulti(restoredBubble);
            }
          } else if (m.role === 'tool') {
            var card = cards[m.tool_call_id];
            if (card) {
              var pretty = m.content;
              try {
                pretty = JSON.stringify(JSON.parse(m.content), null, 2);
              } catch (e) { /* оставляем как есть */ }
              fillToolCard(card, { ok: true, result: pretty });
            }
            if (m.name === 'chart_generate') {
              try {
                var chartRes = JSON.parse(m.content);
                if (chartRes && chartRes.ok !== false && chartRes.svg) appendChartCard(chartRes.svg);
              } catch (e) { /* результат не JSON — карточки графика не будет */ }
            }
            /* при восстановлении онбординга показываем ссылку один раз */
            if (m.name === 'save_project_card' && !messagesEl.querySelector('.ws-projects-action')) {
              appendProjectsAction();
            }
            /* опубликованная онтология — кнопка на 3D-визуализацию */
            if (m.name === 'save_ontology' && !messagesEl.querySelector('.ws-onto-action')) {
              var ontoResult = null;
              try {
                ontoResult = JSON.parse(m.content);
              } catch (e) { /* показываем как есть */ }
              if (ontoResult && ontoResult.published && ontoResult.ontology_id) {
                var ontoRow = document.createElement('div');
                ontoRow.className = 'ws-projects-action ws-onto-action';
                var ontoLink = document.createElement('a');
                ontoLink.className = 'ws-projects-link';
                ontoLink.href = 'ontology.html?id=' + encodeURIComponent(ontoResult.ontology_id);
                ontoLink.textContent = 'Смотреть онтологию в 3D →';
                ontoRow.appendChild(ontoLink);
                messagesEl.appendChild(ontoRow);
              }
            }
          }
        });
        /* журнал решений сессии: у карточек инструментов, прошедших
           подтверждение человеком, отмечаем решение (аудит при возврате) */
        (s.decision_log || []).forEach(function (e) {
          if (e && e.kind === 'approval_decision' && cards[e.id]) {
            var label = APPROVAL_DECISION_LABELS[e.decision] || e.decision;
            cards[e.id].pre.textContent += '\n[подтверждение: ' + label + ']';
          }
        });
        /* этап конвейера восстановленной сессии навыка — для чипа
           «следующий этап», когда пользователь продолжит диалог */
        sessionStage = s.stage || null;
        scrollDown();
        var lastMsg = (s.messages || [])[(s.messages || []).length - 1];
        if (lastMsg && lastMsg.role === 'user') {
          watchPendingReply(sessionId, (s.messages || []).length);
        }
        return true;
      })
      .catch(function () {
        saveSessionId(null);
        return false;
      });
  }

  function newChat() {
    cancelPendingWatch();
    saveSessionId(null);
    clearSkill();
    sessionStage = null;
    hideSuggestions();
    syncSkillbar();
    if (PAGE_MODE) {
      /* на странице чата новый диалог — снова приветствие по центру */
      enterFresh();
      return;
    }
    clearTranscript();
    if (isFull) {
      try { saveRags([]); } catch (e) { /* ок */ }
      syncSkillbar();
      enterFreshFull();
      return;
    }
    appendMessage('assistant', 'Новый диалог начат.');
    scrollDown();
    input.focus();
  }

  newBtn.addEventListener('click', newChat);

  /* ---------- вложения сообщения (📎 файлы; ссылки — просто текст в сообщении) ---------- */

  var pendingAttachments = []; // [{attachment_id, filename, size}]
  var MAX_ATTACHMENTS = 5;

  function formatSize(bytes) {
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' МБ';
    if (bytes >= 1024) return Math.round(bytes / 1024) + ' КБ';
    return bytes + ' Б';
  }

  function renderAttachStrip() {
    attachStrip.innerHTML = '';
    attachStrip.hidden = pendingAttachments.length === 0;
    pendingAttachments.forEach(function (att, idx) {
      var chip = document.createElement('span');
      chip.className = 'ws-attach-chip';
      var name = document.createElement('span');
      name.className = 'ws-attach-name';
      name.textContent = '📎 ' + att.filename + ' · ' + formatSize(att.size);
      name.title = att.filename;
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'ws-attach-del';
      del.setAttribute('aria-label', 'Убрать файл');
      del.textContent = '✕';
      del.addEventListener('click', function () {
        pendingAttachments.splice(idx, 1);
        renderAttachStrip();
      });
      chip.appendChild(name);
      chip.appendChild(del);
      attachStrip.appendChild(chip);
    });
  }

  function addAttachmentError(message) {
    var note = document.createElement('span');
    note.className = 'ws-attach-chip ws-attach-error';
    note.textContent = message;
    attachStrip.appendChild(note);
    attachStrip.hidden = false;
    setTimeout(renderAttachStrip, 4000);
  }

  attachBtn.addEventListener('click', function () {
    if (!isAuthed()) {
      addAttachmentError('Файлы доступны после входа в AIOS');
      return;
    }
    fileInput.value = '';
    fileInput.click();
  });

  fileInput.addEventListener('change', function () {
    var files = Array.prototype.slice.call(fileInput.files || []);
    files.forEach(function (file) {
      if (pendingAttachments.length >= MAX_ATTACHMENTS) {
        addAttachmentError('Не больше ' + MAX_ATTACHMENTS + ' файлов на сообщение');
        return;
      }
      var chip = document.createElement('span');
      chip.className = 'ws-attach-chip';
      chip.textContent = '⏳ ' + file.name + ' — загрузка…';
      attachStrip.appendChild(chip);
      attachStrip.hidden = false;
      window.AIOS_API.chatUploadAttachment(file)
        .then(function (meta) {
          pendingAttachments.push({ attachment_id: meta.attachment_id, filename: meta.filename, size: meta.size });
          renderAttachStrip();
        })
        .catch(function (err) {
          chip.remove();
          addAttachmentError('Не удалось загрузить «' + file.name + '»' + (err && err.message ? ': ' + err.message : ''));
        });
    });
  });

  /* ---------- composer: отправка через рантайм ---------- */

  var sending = false;

  function autosize() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  }

  function send(forcedText) {
    var forced = typeof forcedText === 'string' && forcedText.trim();
    var text = forced ? forcedText.trim() : input.value.trim();
    if (!text || sending) return;
    /* каталог RAG-баз подтягиваем до отправки: команда «подключи базу …»
       должна подключить базу к этому же сообщению (rag_ids едут с ним на
       сервер). Промис закэширован — после первой загрузки задержки нет */
    ragCatalog().then(function () { sendText(forcedText); });
  }

  function sendText(forcedText) {
    var forced = typeof forcedText === 'string' && forcedText.trim();
    var text = forced ? forcedText.trim() : input.value.trim();
    if (!text || sending) return;
    cancelPendingWatch();
    leaveFresh(); /* первое сообщение: поле ввода возвращается вниз панели */
    /* запрос «запустить навык …» перехватываем: навык стартует новой
       сессией автоматически (см. launchSkill), сообщение не уходит в
       текущий диалог — подтверждения администратора не существует */
    if (SKILL_CATALOG && SKILL_CATALOG.length) {
      var intent = findLaunchIntent(text, SKILL_CATALOG);
      if (intent) {
        if (!forced) { input.value = ''; autosize(); }
        launchSkill(intent, '');
        return;
      }
    } else {
      skillsCatalog(); /* подтянем каталог к следующему сообщению */
    }
    /* «подключи/отключи базу …» — база подключается сама, без нажатий
       в меню «+»: rag_ids уедут с этим же сообщением (applyRagIntent) */
    applyRagIntent(text, RAG_CATALOG);
    sending = true;
    sendBtn.disabled = true;
    setGenerating(true);

    /* запущенный агент (навык) едет на сервер с сообщением (id сессии
       хранит skill_ids сам); чип-метка показывается на первом сообщении
       нового диалога */
    var skill = readSkill();
    var freshChat = !getSessionId();
    var messageAttachments = pendingAttachments.slice();
    appendMessage('user', text, skill && freshChat ? skill : null, messageAttachments);

    if (!forced) {
      input.value = '';
      autosize();
    }
    pendingAttachments = [];
    renderAttachStrip();

    var bubble = appendStreamingMessage(text);
    var cards = {};
    var approvalCards = {};
    var finished = false;
    /* true, пока после обрыва стрима ждём ответ фонового turn'а: finish()
       из .then(chatStream) в этот момент сработать не должен — ход завершит
       опрос транскрипта, когда ответ допишется (или провалится честно) */
    var waitingReply = false;

    /* новый turn закрывает подсказку «следующий этап» прошлого обмена */
    var staleStage = messagesEl.querySelector('.ws-stage-next');
    if (staleStage) staleStage.remove();
    hideSuggestions();

    function finish() {
      if (waitingReply) return;
      if (finished) return;
      finished = true;
      if (bubble.beginText) bubble.beginText();
      if (bubble.mdTimer) {
        clearTimeout(bubble.mdTimer);
        bubble.mdTimer = null;
        bubble.mdPending = false;
      }
      if (!bubble.raw) bubble.raw = '(пустой ответ модели)';
      renderChoices(bubble);
      renderChoicesMulti(bubble);
      bubble.root.classList.add('ws-msg--md');
      bubble.text.innerHTML = renderMarkdown(String(bubble.raw).replace(CHOICES_RE, '').replace(CHOICES_MULTI_RE, ''));
      enhanceJsonBlocks(bubble.text);
      enhanceChartBlocks(bubble.text);
      hydrateMedia(bubble.text);
      bubble.cursor.remove();
      setGenerating(false);
      notifyReady(bubble.interrupted);
      /* счётчик токенов в левом меню обновится свежими цифрами */
      document.dispatchEvent(new CustomEvent('aios-usage-refresh'));
      /* сессия навыка знает этап конвейера — предложим следующий */
      maybeSuggestNextStage();
      sending = false;
      sendBtn.disabled = false;
      if (panelOpen) input.focus();
      scrollDown();
    }

    var payload = {
      message: text,
      session_id: getSessionId()
    };
    if (skill) payload.skill_ids = [skill.skill_id];
    var connectedRags = readRags();
    if (connectedRags.length) payload.rag_ids = connectedRags.map(function (r) { return r.rag_id; });
    if (messageAttachments.length) {
      payload.attachment_ids = messageAttachments.map(function (a) { return a.attachment_id; });
    }

    function runStream() {
      return window.AIOS_API.chatStream(payload, {
        onSession: function (ev) {
          if (ev && ev.id) saveSessionId(ev.id);
          /* этап конвейера навыка (для чипа «следующий этап» по завершении) */
          if (ev && ev.stage) sessionStage = ev.stage;
          else if (ev && ev.stage === null) sessionStage = null;
        },
        onDelta: function (t) {
          appendBubbleText(bubble, t);
        },
        onToolCall: function (ev) {
          cards[ev.id] = appendToolCard(ev.name, ev.arguments);
          /* карточка встаёт выше индикатора «думаю» — он остаётся хвостом */
          messagesEl.appendChild(bubble.root);
          scrollDown();
        },
        onApprovalRequest: function (ev) {
          approvalCards[ev.id] = appendApprovalCard(ev);
          messagesEl.appendChild(bubble.root);
        },
        onApprovalResult: function (ev) {
          settleApprovalCard(approvalCards[ev.id], ev.decision);
        },
        onToolResult: function (ev) {
          var card = cards[ev.id];
          if (card) fillToolCard(card, ev);
          if (ev.name === 'chart_generate' && ev.ok !== false && ev.result && ev.result.svg) {
            appendChartCard(ev.result.svg);
          }
          if (ev.name === 'save_project_card' && ev.ok !== false) {
            appendProjectsAction();
          }
          if (ev.name === 'save_ontology' && ev.ok !== false && ev.result && ev.result.published) {
            appendOntologyAction(ev.result);
          }
          scrollDown();
        },
        onError: function (msg) {
          bubble.root.classList.add('ws-msg--error');
          pushError(bubble, msg);
        },
        /* канал умер до конца генерации (сеть/прокси/простой) — генерация на
           сервере продолжает работать в фоне и допишет ответ в транскрипт
           сессии. Держим пузырь открытым и опрашиваем сессию, пока ответ не
           появится; полный ответ заменяет недопотоканный огрызок */
        onInterrupt: function () {
          var sid = getSessionId();
          if (!sid || !window.AIOS_API.recoverReply) {
            bubble.interrupted = true;
            pushError(bubble, 'Соединение прервано до конца ответа');
            return;
          }
          waitingReply = true;
          pushError(bubble, 'Соединение прервано — агент дорабатывает ответ на сервере, ждём…');
          window.AIOS_API.recoverReply(sid).then(function (res) {
            waitingReply = false;
            if (res && res.status === 'reply') {
              bubble.raw = res.message;
              finish();
              return;
            }
            bubble.interrupted = true;
            pushError(bubble, res && res.message
              ? res.message
              : 'Не дождались фонового ответа — он появится в истории сессии');
            finish();
          });
        },
        onDone: finish
      }).catch(function (err) {
        /* протухший id сессии (например, рантайм перезапущен без сессий):
           сбрасываем и повторяем один раз уже новой сессией */
        if (err && err.status === 404 && payload.session_id) {
          payload.session_id = null;
          saveSessionId(null);
          return runStream();
        }
        throw err;
      });
    }

    runStream()
      .catch(function (err) {
        bubble.root.classList.add('ws-msg--error');
        bubble.interrupted = true;
        var message = err && err.message ? err.message : 'Ошибка соединения с рантаймом';
        pushError(bubble, message);
      })
      .then(finish);
  }

  composer.addEventListener('submit', function (e) {
    e.preventDefault();
    send();
  });

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  input.addEventListener('input', autosize);

  /* Кнопка внутри компонента ответа («Исправить», «Пересчитать с запасом»)
     не знает, где её нажали, и поднимает событие aios:ask с текстом вопроса
     (agent_views.js). В рабочем чате это обычное следующее сообщение —
     продолжение работы одним кликом вместо переписывания вопроса руками. */
  document.addEventListener('aios:ask', function (e) {
    var text = e && e.detail && e.detail.text;
    if (text) send(text);
  });

  syncSkillbar();

  /* «Запустить в рабочем чате» (agents.js ставит флаг с open:true):
     открываем панель сразу, чип навыка подхватится в openPanel.
     Если у навыка задан prompt (автозапуск из онбординга — ставит
     cabinet.js), первое сообщение отправляется сразу после открытия. */
  (function handleSkillLaunch() {
    var skill = readSkill();
    if (skill && skill.open) {
      var autoPrompt = skill.prompt ||
        'Запусти навык «' + (skill.name || skill.skill_id) + '»: работай строго по его алгоритму, начиная с первого шага.';
      skill.open = false;
      skill.prompt = autoPrompt;
      saveSkill(skill, false);
      saveSessionId(null); /* навык всегда начинает новую сессию, даже запущенный со страницы «Агенты» */
      clearTranscript();
      syncSkillbar();
      openPanel(null);
      setTimeout(function autoStart() {
        if (sending) { setTimeout(autoStart, 300); return; }
        send(autoPrompt);
      }, ANIM_MS);
    }
  })();

  /* Внешний запуск навыка с этой же страницы (например, кнопка
     «Запустить онбординг проекта» в cabinet.html): сохраняем навык,
     сбрасываем сессию (навык всегда стартует новой сессией), обновляем
     чип и открываем панель. Если передан prompt — агент сразу начинает
     диалог первым сообщением навыка (автозапуск). */
  document.addEventListener('aios-launch-skill', function (e) {
    var d = (e && e.detail) || {};
    if (!d.skill_id) return;
    var prompt = d.prompt || '';
    if (!prompt) {
      prompt = 'Запусти навык «' + (d.name || d.skill_id) +
        '»: работай строго по его алгоритму, начиная с первого шага.';
    }
    saveSkill({ skill_id: d.skill_id, name: d.name || '', prompt: prompt }, false);
    saveSessionId(null); /* новая сессия на каждый запуск навыка */
    clearTranscript();
    syncSkillbar();
    openPanel(null);
    setTimeout(function autoStart() {
      if (sending) { setTimeout(autoStart, 300); return; }
      send(prompt);
    }, ANIM_MS);
  });
})();
