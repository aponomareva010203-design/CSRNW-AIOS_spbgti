/* ============================================================
   Рабочее пространство: чат-панель, наползающая справа.
   Работает на любой странице сайта: разметку панели скрипт
   инъецирует в DOM, триггеры — плашка на главной (#btnWorkspace)
   и кнопка в верхнем меню (#btnNavWorkspace). Оба видны только
   при авторизации (auth.js диспатчит событие 'aios-auth-change').
   Панель открывается поверх страницы, растягивается за левую
   кромку до 100vw, содержит контекстные чипы (агенты/навыки из
   JSON, capability-бейджи «Каталог моделей / RAG / Онтологии»)
   и выбор LLM через window.AIOS_API.getModels(). Ответы
   ассистента сейчас — mock-заглушка до подключения бэкенда
   и системных промптов.
   ============================================================ */

(function () {
  var AUTH_KEY = 'aios-auth-v1';
  var LLM_KEY = 'aios-workspace-llm-v1';

  var MIN_WIDTH = 400;
  var DEFAULT_WIDTH = 560;
  var ANIM_MS = 320;

  /* ---------- триггеры ---------- */

  var entryBtn = document.getElementById('btnWorkspace');
  var navBtn = document.getElementById('btnNavWorkspace');
  var triggers = [];
  if (entryBtn) triggers.push(entryBtn);
  if (navBtn) triggers.push(navBtn);
  if (!triggers.length) return;

  /* ---------- разметка панели: инъекция в DOM ---------- */

  var PANEL_HTML = [
    '<div class="ws-overlay" id="wsOverlay" hidden></div>',
    '<div class="ws-panel" id="wsPanel" role="dialog" aria-modal="true" aria-labelledby="wsTitle" hidden>',
    '  <div class="ws-grip" id="wsGrip" title="Потяните, чтобы изменить ширину" aria-hidden="true"></div>',
    '  <header class="ws-head">',
    '    <div class="ws-head-title">',
    '      <h2 id="wsTitle">Рабочее пространство</h2>',
    '      <span class="ws-model-label" id="wsModelLabel">LLM не выбрана</span>',
    '    </div>',
    '    <div class="ws-head-actions">',
    '      <button class="ws-btn" id="wsSettingsBtn" type="button" aria-label="Настройки" aria-haspopup="dialog" aria-controls="wsSettings">⚙</button>',
    '      <button class="ws-btn" id="wsExpandBtn" type="button" aria-label="Во весь экран">⛶</button>',
    '      <button class="ws-btn" id="wsCloseBtn" type="button" aria-label="Закрыть">✕</button>',
    '    </div>',
    '  </header>',
    '  <div class="ws-context">',
    '    <button class="ws-context-toggle" id="wsContextToggle" type="button" aria-expanded="false" aria-controls="wsContextBody">',
    '      <span>Контекст: агенты, навыки и знания</span>',
    '      <span class="ws-context-chev" aria-hidden="true"></span>',
    '    </button>',
    '    <div class="ws-context-body" id="wsContextBody" hidden>',
    '      <div class="ws-context-group">Агенты и навыки</div>',
    '      <div class="ws-chips" id="wsLocalChips"></div>',
    '      <div class="ws-context-group">Всегда доступны</div>',
    '      <div class="ws-chips">',
    '        <span class="ws-chip ws-chip--cap" title="Панель имеет доступ к каталогу моделей">Каталог моделей</span>',
    '        <span class="ws-chip ws-chip--cap" title="Панель имеет доступ к RAG">RAG</span>',
    '        <span class="ws-chip ws-chip--cap" title="Панель имеет доступ к онтологиям">Онтологии</span>',
    '      </div>',
    '    </div>',
    '  </div>',
    '  <div class="ws-messages" id="wsMessages" aria-live="polite"></div>',
    '  <div class="ws-settings" id="wsSettings" role="dialog" aria-label="Настройки рабочего пространства" hidden>',
    '    <div class="ws-settings-title">Модель для диалога</div>',
    '    <div class="ws-settings-list" id="wsSettingsList"></div>',
    '  </div>',
    '  <form class="ws-composer" id="wsComposer">',
    '    <textarea id="wsInput" rows="1" placeholder="Сообщение рабочему пространству…" aria-label="Сообщение"></textarea>',
    '    <button class="ws-send" type="submit" aria-label="Отправить">↑</button>',
    '  </form>',
    '</div>'
  ].join('\n');

  document.body.insertAdjacentHTML('beforeend', PANEL_HTML);

  /* ---------- элементы панели ---------- */

  var overlay = document.getElementById('wsOverlay');
  var panel = document.getElementById('wsPanel');
  var grip = document.getElementById('wsGrip');
  var settingsBtn = document.getElementById('wsSettingsBtn');
  var expandBtn = document.getElementById('wsExpandBtn');
  var closeBtn = document.getElementById('wsCloseBtn');
  var modelLabel = document.getElementById('wsModelLabel');
  var contextToggle = document.getElementById('wsContextToggle');
  var contextBody = document.getElementById('wsContextBody');
  var localChips = document.getElementById('wsLocalChips');
  var messagesEl = document.getElementById('wsMessages');
  var settingsEl = document.getElementById('wsSettings');
  var settingsList = document.getElementById('wsSettingsList');
  var composer = document.getElementById('wsComposer');
  var input = document.getElementById('wsInput');

  /* ---------- утилиты ---------- */

  function fetchJSON(url) {
    return fetch(url, { headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      });
  }

  function isAuthed() {
    try {
      return !!localStorage.getItem(AUTH_KEY);
    } catch (e) {
      return false;
    }
  }

  function getSavedModel() {
    try {
      var raw = localStorage.getItem(LLM_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function saveModel(model) {
    try {
      localStorage.setItem(LLM_KEY, JSON.stringify(model));
    } catch (e) { /* приватный режим */ }
  }

  /* ---------- видимость триггеров ---------- */

  function syncEntry() {
    var show = isAuthed();
    triggers.forEach(function (btn) { btn.hidden = !show; });
  }

  syncEntry();
  window.addEventListener('aios-auth-change', syncEntry);

  /* ---------- открытие / закрытие панели ---------- */

  var panelOpen = false;
  var closeTimer = null;
  var lastTrigger = null;

  function openPanel(via) {
    if (panelOpen) return;
    panelOpen = true;
    lastTrigger = via || null;
    clearTimeout(closeTimer);
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
      appendMessage('assistant',
        'Добро пожаловать в рабочее пространство. Здесь вы общаетесь с LLM, подключая в контекст системы знаний, агентов и навыки.');
      scrollDown();
    }
  }

  function finishClose() {
    panel.hidden = true;
    overlay.hidden = true;
    document.body.classList.remove('modal-open');
    if (lastTrigger && !lastTrigger.hidden) lastTrigger.focus();
  }

  function closePanel() {
    if (!panelOpen) return;
    panelOpen = false;
    closeSettings();
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

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (panel.hidden) return;
    if (!settingsEl.hidden) {
      closeSettings();
    } else {
      closePanel();
    }
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


  /* ---------- контекст: чипы агентов и навыков ---------- */

  var activeChips = {}; /* id -> { label, kind } */
  var contextLoaded = false;

  function addChip(id, label, kind) {
    var chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'ws-chip';
    chip.textContent = label;
    chip.setAttribute('aria-pressed', 'false');
    chip.addEventListener('click', function () {
      var on = chip.classList.toggle('ws-chip--on');
      chip.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (on) {
        activeChips[id] = { label: label, kind: kind };
      } else {
        delete activeChips[id];
      }
    });
    localChips.appendChild(chip);
  }

  function showContextError() {
    localChips.innerHTML = '';
    var text = document.createElement('span');
    text.className = 'ws-chip-load';
    text.textContent = 'Не удалось загрузить агентов и навыки. ';
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn-register models-retry';
    retry.textContent = 'Повторить';
    retry.addEventListener('click', loadContext);
    localChips.appendChild(text);
    localChips.appendChild(retry);
  }

  function loadContext() {
    contextLoaded = true;
    localChips.innerHTML = '';
    var load = document.createElement('span');
    load.className = 'ws-chip-load';
    load.textContent = 'Загрузка…';
    localChips.appendChild(load);

    Promise.all([fetchJSON('/data/agents.json'), fetchJSON('/data/skills.json')])
      .then(function (results) {
        var agents = (results[0] && results[0].items) || [];
        var skills = (results[1] && results[1].items) || [];
        localChips.innerHTML = '';
        agents.forEach(function (agent) {
          addChip('agent:' + agent.id, agent.name, 'агент');
        });
        skills.forEach(function (skill) {
          addChip('skill:' + skill.id, skill.name, 'навык');
        });
        if (!agents.length && !skills.length) {
          var empty = document.createElement('span');
          empty.className = 'ws-chip-load';
          empty.textContent = 'Агенты и навыки пока не добавлены.';
          localChips.appendChild(empty);
        }
      })
      .catch(showContextError);
  }

  contextToggle.addEventListener('click', function () {
    var open = contextBody.hidden;
    contextBody.hidden = !open;
    contextToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && !contextLoaded) loadContext();
  });

  /* ---------- настройки: выбор LLM ---------- */

  var selectedModel = getSavedModel();

  function renderModelLabel() {
    modelLabel.textContent = selectedModel ? ('LLM: ' + selectedModel.name) : 'LLM не выбрана';
  }

  renderModelLabel();

  function showSettingsError() {
    settingsList.innerHTML = '';
    var text = document.createElement('span');
    text.className = 'ws-model-sub';
    text.textContent = 'Не удалось загрузить каталог моделей. ';
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn-register models-retry';
    retry.textContent = 'Повторить';
    retry.addEventListener('click', loadLLMList);
    settingsList.appendChild(text);
    settingsList.appendChild(retry);
  }

  function makeOption(model) {
    var label = document.createElement('label');
    label.className = 'ws-model-option';
    var radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'ws-llm';
    radio.value = model.id;
    radio.checked = !!selectedModel && selectedModel.id === model.id;
    radio.addEventListener('change', function () {
      selectedModel = { id: model.id, name: model.name };
      saveModel(selectedModel);
      renderModelLabel();
    });
    var body = document.createElement('span');
    var name = document.createElement('span');
    name.className = 'ws-model-name';
    name.textContent = model.name;
    var sub = document.createElement('span');
    sub.className = 'ws-model-sub';
    sub.textContent = model.provider || '';
    body.appendChild(name);
    body.appendChild(sub);
    label.appendChild(radio);
    label.appendChild(body);
    return label;
  }

  function loadLLMList() {
    if (!window.AIOS_API || !window.AIOS_API.getModels) return;
    settingsList.innerHTML = '';
    var load = document.createElement('span');
    load.className = 'ws-model-sub';
    load.textContent = 'Загрузка каталога…';
    settingsList.appendChild(load);

    window.AIOS_API.getModels()
      .then(function (data) {
        var items = ((data && data.items) || []).filter(function (m) {
          return (m.tags || []).indexOf('LLM') !== -1;
        });
        settingsList.innerHTML = '';
        if (!items.length) {
          var empty = document.createElement('span');
          empty.className = 'ws-model-sub';
          empty.textContent = 'В каталоге пока нет диалоговых LLM.';
          settingsList.appendChild(empty);
          return;
        }
        items.forEach(function (model) {
          settingsList.appendChild(makeOption(model));
        });
      })
      .catch(showSettingsError);
  }

  function closeSettings() {
    settingsEl.hidden = true;
    settingsBtn.setAttribute('aria-expanded', 'false');
  }

  settingsBtn.addEventListener('click', function () {
    if (settingsEl.hidden) {
      settingsEl.hidden = false;
      settingsBtn.setAttribute('aria-expanded', 'true');
      loadLLMList();
    } else {
      closeSettings();
    }
  });

  document.addEventListener('click', function (e) {
    if (settingsEl.hidden) return;
    if (!settingsEl.contains(e.target) && e.target !== settingsBtn) closeSettings();
  });


  /* ---------- сообщения ---------- */

  function scrollDown() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function appendMessage(role, text) {
    var msg = document.createElement('div');
    msg.className = 'ws-msg ws-msg--' + role;
    msg.textContent = text;
    messagesEl.appendChild(msg);
    scrollDown();
    return msg;
  }

  function showTyping() {
    var msg = document.createElement('div');
    msg.className = 'ws-msg ws-msg--assistant';
    var dots = document.createElement('div');
    dots.className = 'ws-typing';
    for (var i = 0; i < 3; i++) dots.appendChild(document.createElement('span'));
    msg.appendChild(dots);
    messagesEl.appendChild(msg);
    scrollDown();
    return msg;
  }

  /* Mock-ответ до подключения бэкенда и системных промптов */
  function assistantReply() {
    var typing = showTyping();
    setTimeout(function () {
      typing.remove();
      var parts = ['Это заглушка фронтенда: ответ ассистента появится после подключения бэкенда и системных промптов.'];
      parts.push('Модель: ' + (selectedModel ? selectedModel.name : 'не выбрана') + '.');
      var names = Object.keys(activeChips).map(function (k) { return activeChips[k].label; });
      if (names.length) {
        parts.push('В контексте: ' + names.join(', ') + '.');
      }
      appendMessage('assistant', parts.join('\n'));
    }, 800);
  }

  /* ---------- composer ---------- */

  function autosize() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  }

  function send() {
    var text = input.value.trim();
    if (!text) return;
    appendMessage('user', text);
    input.value = '';
    autosize();
    assistantReply();
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
})();

