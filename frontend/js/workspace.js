/* ============================================================
   Рабочее пространство: клиент AIOS Harness. Панель наползает справа.
   Работает на любой странице сайта: разметку панели скрипт
   инъецирует в DOM, триггеры — плашка на главной (#btnWorkspace)
   и кнопка в верхнем меню (#btnNavWorkspace). Оба видны только
   при авторизации (auth.js диспатчит событие 'aios-auth-change').

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
    '      <button class="ws-btn" id="wsNewBtn" type="button" aria-label="Новый диалог" title="Новый диалог">＋</button>',
    '      <button class="ws-btn" id="wsExpandBtn" type="button" aria-label="Во весь экран">⛶</button>',
    '      <button class="ws-btn" id="wsCloseBtn" type="button" aria-label="Закрыть">✕</button>',
    '    </div>',
    '  </header>',
    '  <div class="ws-messages" id="wsMessages" aria-live="polite"></div>',
    '  <form class="ws-composer" id="wsComposer" onsubmit="return false">',
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
  var newBtn = document.getElementById('wsNewBtn');
  var expandBtn = document.getElementById('wsExpandBtn');
  var closeBtn = document.getElementById('wsCloseBtn');
  var modelLabel = document.getElementById('wsModelLabel');
  var messagesEl = document.getElementById('wsMessages');
  var composer = document.getElementById('wsComposer');
  var sendBtn = composer.querySelector('.ws-send');
  var input = document.getElementById('wsInput');

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

  function welcome() {
    appendMessage('assistant',
      'Добро пожаловать в рабочее пространство. Диалог идёт через агентный рантайм; ' +
      'модель задана администратором. ' +
      'Вызовы инструментов показываются карточками прямо в чате.');
  }

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
    return { root: msg, text: text, cursor: cursor };
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
            appendMessage('user', m.content || '');
          } else if (m.role === 'assistant') {
            (m.tool_calls || []).forEach(function (tc) {
              var args = {};
              try {
                args = JSON.parse((tc.function && tc.function.arguments) || '{}');
              } catch (e) { /* показываем как есть */ }
              cards[tc.id] = appendToolCard((tc.function && tc.function.name) || 'tool', args);
            });
            if (m.content) appendMessage('assistant', m.content);
          } else if (m.role === 'tool') {
            var card = cards[m.tool_call_id];
            if (card) {
              var pretty = m.content;
              try {
                pretty = JSON.stringify(JSON.parse(m.content), null, 2);
              } catch (e) { /* оставляем как есть */ }
              fillToolCard(card, { ok: true, result: pretty });
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
    messagesEl.innerHTML = '';
    appendMessage('assistant', 'Новый диалог начат.');
    scrollDown();
    input.focus();
  }

  newBtn.addEventListener('click', newChat);

  /* ---------- composer: отправка через рантайм ---------- */

  var sending = false;

  function autosize() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  }

  function send() {
    var text = input.value.trim();
    if (!text || sending) return;
    sending = true;
    sendBtn.disabled = true;
    appendMessage('user', text);
    input.value = '';
    autosize();

    var bubble = appendStreamingMessage();
    var cards = {};
    var finished = false;

    function finish() {
      if (finished) return;
      finished = true;
      bubble.cursor.remove();
      if (!bubble.text.textContent) {
        bubble.text.textContent = '(пустой ответ модели)';
      }
      sending = false;
      sendBtn.disabled = false;
      input.focus();
      scrollDown();
    }

    var payload = {
      message: text,
      session_id: getSessionId()
    };

    function runStream() {
      return window.AIOS_API.chatStream(payload, {
        onSession: function (ev) {
          if (ev && ev.id) saveSessionId(ev.id);
        },
        onDelta: function (t) {
          bubble.text.textContent += t;
          scrollDown();
        },
        onToolCall: function (ev) {
          cards[ev.id] = appendToolCard(ev.name, ev.arguments);
          scrollDown();
        },
        onToolResult: function (ev) {
          var card = cards[ev.id];
          if (card) fillToolCard(card, ev);
          scrollDown();
        },
        onError: function (msg) {
          bubble.root.classList.add('ws-msg--error');
          bubble.text.textContent += (bubble.text.textContent ? '\n' : '') + '⚠ ' + msg;
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
        var message = err && err.message ? err.message : 'Ошибка соединения с рантаймом';
        bubble.text.textContent += (bubble.text.textContent ? '\n' : '') + '⚠ ' + message;
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
})();
