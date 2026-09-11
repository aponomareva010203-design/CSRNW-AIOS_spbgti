/* ============================================================
   Рабочий чат: клиент AIOS Harness. Панель наползает справа.
   Работает на любой странице сайта: разметку панели скрипт
   инъецирует в DOM, триггеры — золотая плашка на главной
   (#btnWorkspace) и пункт «Рабочий чат» в левом меню
   (#btnNavWorkspace). Раздел «Рабочее пространство» меню (чат,
   «Кабинет», «Проекты») виден только при авторизации
   (auth.js диспатчит событие 'aios-auth-change').

   Диалог идёт через агентный рантайм (POST /api/chat, SSE):
   текст ассистента печатается потоково, вызовы инструментов
   показываются карточками, транскрипт восстанавливается из
   сессии при повторном открытии панели. Модель задаёт
   администратор централизованно (админ-панель →
   «Настройки внешних API»); текущий статус — GET /api/runtime.
   Подбор навыков и систем знаний появится позже — рекомендациями
   в самом чате (раздел «Контекст» панели убран, ничего не
   подгружается в пространство автоматически).
   ============================================================ */

(function () {
  var AUTH_KEY = 'aios-auth-v1';
  var SESSION_KEY = 'aios-workspace-session-v1';
  var SKILL_KEY = 'aios-workspace-skill-v1'; /* навык, запущенный в чате (ставит agents.js) */
  var RAG_KEY = 'aios-workspace-rag-v1'; /* RAG-база, подключённая кнопкой 📚 в чате */

  var MIN_WIDTH = 400;
  var DEFAULT_WIDTH = 560;
  var ANIM_MS = 320;

  /* ---------- триггеры ---------- */

  var entryBtn = document.getElementById('btnWorkspace');
  var navBtn = document.getElementById('btnNavWorkspace');
  var wsGroup = document.getElementById('wsGroup');
  var menuCabinet = document.getElementById('menuCabinet');
  var menuProjects = document.getElementById('menuProjects');
  var triggers = [];
  if (entryBtn) triggers.push(entryBtn);
  if (navBtn) triggers.push(navBtn);
  if (!triggers.length) return;

  /* ---------- разметка панели: инъекция в DOM ---------- */

  var PANEL_HTML = [
    '<div class="ws-overlay" id="wsOverlay" hidden></div>',
    '<button class="ws-edge-tab" id="wsEdgeTab" type="button" aria-label="Открыть рабочий чат" hidden><span>💬</span></button>',
    '<button class="ws-ready-badge" id="wsReadyBadge" type="button" hidden aria-live="polite">✦ Ответ готов</button>',
    '<div class="ws-panel" id="wsPanel" role="dialog" aria-modal="true" aria-labelledby="wsTitle" hidden>',
    '  <div class="ws-grip" id="wsGrip" title="Потяните, чтобы изменить ширину" aria-hidden="true"></div>',
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
    '    <div class="ws-skillpick" id="wsSkillPick">',
    '      <button class="ws-skillpick-btn" id="wsSkillPickBtn" type="button" aria-haspopup="listbox" aria-expanded="false" aria-label="Выбрать навык" data-tooltip="Выбрать навык"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg></button>',
    '      <div class="ws-skillpick-menu" id="wsSkillPickMenu" role="listbox" aria-label="Навыки" hidden></div>',
    '    </div>',
    '    <div class="ws-composer-row">',
    '    <div class="ws-skillpick" id="wsRagPick">',
    '      <button class="ws-skillpick-btn" id="wsRagPickBtn" type="button" aria-haspopup="listbox" aria-expanded="false" aria-label="Подключить RAG-базу" data-tooltip="Подключить RAG-базу"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"></path><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"></path></svg></button>',
    '      <div class="ws-skillpick-menu" id="wsRagPickMenu" role="listbox" aria-label="RAG-базы" hidden></div>',
    '    </div>',
    '    <button class="ws-skillpick-btn" id="wsAttachBtn" type="button" aria-label="Прикрепить файл" data-tooltip="Прикрепить файл (.pdf, .txt, .md, .docx)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg></button>',
    '    <input type="file" id="wsFileInput" multiple accept=".pdf,.txt,.md,.docx" hidden>',
    '    <textarea id="wsInput" rows="1" placeholder="Сообщение рабочему чату…" aria-label="Сообщение"></textarea>',
    '    <button class="ws-send" type="submit" aria-label="Отправить">↑</button>',
    '    </div>',
    '    <div class="ws-attach-strip" id="wsAttachStrip" hidden></div>',
    '  </form>',
    '</div>'
  ].join('\n');

  document.body.insertAdjacentHTML('beforeend', PANEL_HTML);

  /* «язычок» у правого края экрана — всегда виден (пока доступен рабочий
     чат), тянет открыть панель, даже когда её ещё не открывали ни разу.
     Видимость по авторизации — вместе с остальными triggers (syncEntry). */
  var edgeTab = document.getElementById('wsEdgeTab');
  if (edgeTab) triggers.push(edgeTab);

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
  var skillPick = document.getElementById('wsSkillPick');
  var skillPickBtn = document.getElementById('wsSkillPickBtn');
  var skillPickMenu = document.getElementById('wsSkillPickMenu');
  var ragPick = document.getElementById('wsRagPick');
  var ragPickBtn = document.getElementById('wsRagPickBtn');
  var ragPickMenu = document.getElementById('wsRagPickMenu');
  var input = document.getElementById('wsInput');
  var attachBtn = document.getElementById('wsAttachBtn');
  var fileInput = document.getElementById('wsFileInput');
  var attachStrip = document.getElementById('wsAttachStrip');
  var liveDot = document.getElementById('wsLiveDot');
  var readyBadge = document.getElementById('wsReadyBadge');

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

  /* ---------- подключённая RAG-база (кнопка 📚 «Подключить RAG-базу») ---------- */

  var RAG_LIMIT = 3; /* синхронизировано с ChatRequest.rag_ids max_length на сервере */

  function readRags() {
    try {
      var raw = localStorage.getItem(RAG_KEY);
      var list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list.filter(function (r) { return r && r.rag_id; }) : [];
    } catch (e) {
      return [];
    }
  }

  function saveRags(list) {
    try { localStorage.setItem(RAG_KEY, JSON.stringify(list)); } catch (e) { /* приватный режим */ }
  }

  function toggleRag(rag) {
    var list = readRags();
    var idx = -1;
    for (var i = 0; i < list.length; i++) { if (list[i].rag_id === rag.id) { idx = i; break; } }
    if (idx !== -1) {
      list.splice(idx, 1);
    } else if (list.length < RAG_LIMIT) {
      list.push({ rag_id: rag.id, name: rag.name || rag.id });
    }
    saveRags(list);
  }

  /* Активный навык и подключённая RAG-база видны в заголовке панели:
     «Рабочий чат: <навык> · база: <имя>». Оба уезжают на сервер с каждым
     сообщением (skill_ids и rag_ids), поэтому переживают новый диалог. */
  function syncSkillbar() {
    var title = document.getElementById('wsTitle');
    var skill = readSkill();
    var rags = readRags();
    var parts = [];
    if (skill) parts.push(skill.name || skill.skill_id);
    if (rags.length) parts.push('базы: ' + rags.map(function (r) { return r.name || r.rag_id; }).join(', '));
    if (title) title.textContent = parts.length ? 'Рабочий чат: ' + parts.join(' · ') : 'Рабочий чат';
    /* кнопка выбора навыка остаётся видимой всегда; пока в чате уже
       запущен навык — клик заблокирован, подсказка объясняет почему
       (чтобы сменить навык, нужен новый диалог, а не скрытая кнопка). */
    if (skillPickBtn) {
      skillPickBtn.classList.toggle('ws-skillpick-btn--locked', !!skill);
      skillPickBtn.setAttribute(
        'data-tooltip',
        skill ? 'Чтобы перейти к новому навыку, откройте новый чат' : 'Выбрать навык'
      );
    }
    if (skill) closeSkillPickMenu();
    if (ragPickBtn) ragPickBtn.classList.toggle('ws-skillpick-btn--active', rags.length > 0);
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
    saveSkill({ skill_id: skill.id, name: skill.name || '', prompt: autoPrompt }, false);
    saveSessionId(null); /* навык всегда начинает новую сессию */
    messagesEl.innerHTML = '';
    syncSkillbar();
    openPanel(null);
    /* ждём окончания текущей отправки, если такая идёт (retry);
       стартуем после анимации открытия панели */
    setTimeout(function autoStart() {
      if (sending) { setTimeout(autoStart, 300); return; }
      send(autoPrompt);
    }, ANIM_MS);
  }

  /* ---------- выпадающий список навыков у поля ввода ---------- */

  /* Кнопка «⚡» слева от поля ввода видна, только когда в чате не запущен
     ни один навык; клик открывает список названий из каталога навыков,
     выбор запускает навык (новая сессия, автостарт пайплайна). */
  function closeSkillPickMenu() {
    if (!skillPickMenu) return;
    skillPickMenu.hidden = true;
    skillPickBtn.setAttribute('aria-expanded', 'false');
  }

  function renderSkillPickMenu(items) {
    skillPickMenu.innerHTML = '';
    if (!items.length) {
      var empty = document.createElement('div');
      empty.className = 'ws-skillpick-empty';
      empty.textContent = 'Навыков пока нет';
      skillPickMenu.appendChild(empty);
      return;
    }
    items.forEach(function (skill) {
      var opt = document.createElement('button');
      opt.type = 'button';
      opt.className = 'ws-skillpick-item';
      opt.setAttribute('role', 'option');
      opt.textContent = skill.name || skill.id;
      opt.title = skill.description || '';
      opt.addEventListener('click', function () {
        closeSkillPickMenu();
        launchSkill(skill, '');
      });
      skillPickMenu.appendChild(opt);
    });
  }

  if (skillPickBtn && skillPickMenu) {
    skillPickBtn.addEventListener('click', function () {
      if (readSkill()) return; /* навык уже активен — открыть новый чат, чтобы сменить */
      if (!skillPickMenu.hidden) { closeSkillPickMenu(); return; }
      skillPickMenu.hidden = false;
      skillPickBtn.setAttribute('aria-expanded', 'true');
      skillPickMenu.innerHTML = '';
      var loading = document.createElement('div');
      loading.className = 'ws-skillpick-empty';
      loading.textContent = 'Загрузка…';
      skillPickMenu.appendChild(loading);
      skillsCatalog().then(renderSkillPickMenu);
    });

    /* закрытие: клик мимо, Escape в поле ввода, сворачивание панели */
    document.addEventListener('click', function (e) {
      if (skillPickMenu.hidden) return;
      if (skillPick.contains(e.target)) return;
      closeSkillPickMenu();
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !skillPickMenu.hidden) {
        e.stopPropagation();
        closeSkillPickMenu();
      }
    });
  }

  /* ---------- каталог RAG-баз и пикер подключения (кнопка 📚) ---------- */

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

  function closeRagPickMenu() {
    if (!ragPickMenu) return;
    ragPickMenu.hidden = true;
    ragPickBtn.setAttribute('aria-expanded', 'false');
  }

  function renderRagPickMenu(items) {
    ragPickMenu.innerHTML = '';
    var connected = readRags();
    var connectedIds = connected.map(function (r) { return r.rag_id; });
    if (!items.length) {
      var empty = document.createElement('div');
      empty.className = 'ws-skillpick-empty';
      empty.textContent = 'Опубликованных RAG-баз пока нет';
      ragPickMenu.appendChild(empty);
      return;
    }
    var hint = document.createElement('div');
    hint.className = 'ws-skillpick-empty';
    hint.textContent = 'Можно подключить до ' + RAG_LIMIT + ' баз одновременно';
    ragPickMenu.appendChild(hint);
    items.forEach(function (rag) {
      var isOn = connectedIds.indexOf(rag.id) !== -1;
      var opt = document.createElement('button');
      opt.type = 'button';
      opt.className = 'ws-skillpick-item' + (isOn ? ' ws-skillpick-item--on' : '');
      opt.setAttribute('role', 'option');
      opt.setAttribute('aria-selected', isOn ? 'true' : 'false');
      opt.textContent = (isOn ? '✓ ' : '') + (rag.name || rag.id);
      opt.title = rag.description_ru || '';
      opt.addEventListener('click', function () {
        toggleRag(rag);
        syncSkillbar();
        renderRagPickMenu(items);
      });
      ragPickMenu.appendChild(opt);
    });
  }

  if (ragPickBtn && ragPickMenu) {
    ragPickBtn.addEventListener('click', function () {
      if (!ragPickMenu.hidden) { closeRagPickMenu(); return; }
      ragPickMenu.hidden = false;
      ragPickBtn.setAttribute('aria-expanded', 'true');
      ragPickMenu.innerHTML = '';
      var loading = document.createElement('div');
      loading.className = 'ws-skillpick-empty';
      loading.textContent = 'Загрузка…';
      ragPickMenu.appendChild(loading);
      ragCatalog().then(renderRagPickMenu);
    });

    /* закрытие: клик мимо, Escape в поле ввода */
    document.addEventListener('click', function (e) {
      if (ragPickMenu.hidden) return;
      if (ragPick.contains(e.target)) return;
      closeRagPickMenu();
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !ragPickMenu.hidden) {
        e.stopPropagation();
        closeRagPickMenu();
      }
    });
  }

  /* ---------- видимость триггеров ---------- */

  function syncEntry() {
    var show = isAuthed();
    triggers.forEach(function (btn) { btn.hidden = !show; });
    /* раздел «Рабочее пространство» меню (чат, «Кабинет»,
       «Проекты») — целиком только вошедшим */
    if (wsGroup) wsGroup.hidden = !show;
    if (menuCabinet) menuCabinet.hidden = !show;
    if (menuProjects) menuProjects.hidden = !show;
  }

  syncEntry();
  window.addEventListener('aios-auth-change', syncEntry);

  /* ---------- открытие / закрытие панели ---------- */

  var panelOpen = false;
  var closeTimer = null;
  var lastTrigger = null;

  function welcome() {
    appendMessage('assistant',
      'Добро пожаловать в рабочий чат. Диалог идёт через агентный рантайм; ' +
      'модель задана администратором. ' +
      'Вызовы инструментов показываются карточками прямо в чате.');
  }

  function openPanel(via) {
    if (panelOpen) return;
    panelOpen = true;
    lastTrigger = via || null;
    clearTimeout(closeTimer);
    if (readyBadge) readyBadge.hidden = true;
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
    if (!panelOpen) return;
    panelOpen = false;
    overlay.classList.remove('ws-overlay--show');
    panel.classList.remove('ws-open');
    clearTimeout(closeTimer);
    closeTimer = setTimeout(finishClose, ANIM_MS);
  }

  triggers.forEach(function (btn) {
    btn.addEventListener('click', function () { openPanel(btn); });
  });
  closeBtn.addEventListener('click', closePanel);
  overlay.addEventListener('click', closePanel);

  /* Открыть конкретную сессию из чужого интерфейса («Кабинет» → история
     сессий): фиксируем id, очищаем транскрипт и открываем панель —
     восстановление сессии выполняет restoreSession(). */
  document.addEventListener('aios-open-session', function (e) {
    var id = e && e.detail && e.detail.session_id;
    if (!id) return;
    saveSessionId(id);
    messagesEl.innerHTML = '';
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
    var width = clampWidth(drag.startW + (drag.startX - e.clientX));
    panel.style.width = width + 'px';
    /* ручное растягивание выходит из режима «во весь экран» */
    isFull = false;
    expandBtn.setAttribute('aria-label', 'Во весь экран');
  });

  ['pointerup', 'pointercancel'].forEach(function (ev) {
    grip.addEventListener(ev, function () {
      drag = null;
      document.body.classList.remove('ws-dragging');
    });
  });

  function toggleFull() {
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
  }

  expandBtn.addEventListener('click', toggleFull);
  grip.addEventListener('dblclick', toggleFull);

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
        if (fence[1] !== 'aios-choices') {
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
    msg.appendChild(textEl);
    messagesEl.appendChild(msg);
    scrollDown();
    return bubble;
  }

  function appendStreamingMessage() {
    var msg = document.createElement('div');
    msg.className = 'ws-msg ws-msg--assistant';
    var text = document.createElement('span');
    text.className = 'ws-msg-text';
    var cursor = document.createElement('span');
    cursor.className = 'ws-cursor';
    msg.appendChild(text);
    msg.appendChild(cursor);
    messagesEl.appendChild(msg);
    scrollDown();
    return { root: msg, text: text, cursor: cursor, raw: '', mdTimer: null, mdPending: false, mdShown: false };
  }

  /* Потоковый показ ответа: сырое значение копим в bubble.raw, на экран
     выносим безопасный markdown не чаще раза в 120 мс (самая первая
     дельта — сразу). Пользовательские сообщения остаются простым текстом. */
  var MD_RENDER_MS = 120;

  function flushMd(bubble) {
    bubble.mdShown = true;
    bubble.root.classList.add('ws-msg--md');
    bubble.text.innerHTML = renderMarkdown(String(bubble.raw || '').replace(CHOICES_RE, ''));
    enhanceJsonBlocks(bubble.text);
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
    bubble.raw += t;
    scheduleMdRender(bubble);
  }

  function pushError(bubble, msg) {
    bubble.raw = (bubble.raw ? bubble.raw + '\n' : '') + '⚠ ' + msg;
    if (bubble.mdTimer) {
      clearTimeout(bubble.mdTimer);
      bubble.mdTimer = null;
      bubble.mdPending = false;
    }
    flushMd(bubble);
  }

  /* Карточка вызова инструмента */
  function appendToolCard(name, args) {
    var card = document.createElement('div');
    card.className = 'ws-tool';

    var head = document.createElement('button');
    head.type = 'button';
    head.className = 'ws-tool-head';
    head.setAttribute('aria-expanded', 'false');
    var title = document.createElement('span');
    title.className = 'ws-tool-name';
    title.textContent = '⚙ ' + name;
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
    messagesEl.appendChild(card);
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

  function fillToolCard(card, ev) {
    card.root.classList.add(ev.ok === false ? 'ws-tool--err' : 'ws-tool--ok');
    var text = toolResultText(ev.result);
    if (text.length > 4000) text = text.slice(0, 4000) + '\n…';
    card.pre.textContent = text;
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

  function restoreSession() {
    var sessionId = getSessionId();
    if (!sessionId || !window.AIOS_API || !window.AIOS_API.getSession) {
      return Promise.resolve(false);
    }
    return window.AIOS_API.getSession(sessionId)
      .then(function (s) {
        messagesEl.innerHTML = '';
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
            if (m.content) renderChoices(appendMessage('assistant', m.content));
          } else if (m.role === 'tool') {
            var card = cards[m.tool_call_id];
            if (card) {
              var pretty = m.content;
              try {
                pretty = JSON.stringify(JSON.parse(m.content), null, 2);
              } catch (e) { /* оставляем как есть */ }
              fillToolCard(card, { ok: true, result: pretty });
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
        scrollDown();
        return true;
      })
      .catch(function () {
        saveSessionId(null);
        return false;
      });
  }

  function newChat() {
    saveSessionId(null);
    clearSkill();
    syncSkillbar();
    messagesEl.innerHTML = '';
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
    sending = true;
    sendBtn.disabled = true;
    setGenerating(true);

    /* активный навык едет на сервер с сообщением (id сессии хранит skill_ids
       сам); чип-метка показывается на первом сообщении нового диалога */
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

    var bubble = appendStreamingMessage();
    var cards = {};
    var finished = false;

    function finish() {
      if (finished) return;
      finished = true;
      if (bubble.mdTimer) {
        clearTimeout(bubble.mdTimer);
        bubble.mdTimer = null;
        bubble.mdPending = false;
      }
      if (!bubble.raw) bubble.raw = '(пустой ответ модели)';
      renderChoices(bubble);
      bubble.root.classList.add('ws-msg--md');
      bubble.text.innerHTML = renderMarkdown(String(bubble.raw).replace(CHOICES_RE, ''));
      enhanceJsonBlocks(bubble.text);
      bubble.cursor.remove();
      setGenerating(false);
      notifyReady(bubble.interrupted);
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
        },
        onDelta: function (t) {
          appendBubbleText(bubble, t);
        },
        onToolCall: function (ev) {
          cards[ev.id] = appendToolCard(ev.name, ev.arguments);
          scrollDown();
        },
        onToolResult: function (ev) {
          var card = cards[ev.id];
          if (card) fillToolCard(card, ev);
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
      saveSessionId(null); /* навык всегда начинает новую сессию, даже запущенный со страницы «Агенты и навыки» */
      messagesEl.innerHTML = '';
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
    messagesEl.innerHTML = '';
    syncSkillbar();
    openPanel(null);
    setTimeout(function autoStart() {
      if (sending) { setTimeout(autoStart, 300); return; }
      send(prompt);
    }, ANIM_MS);
  });
})();
