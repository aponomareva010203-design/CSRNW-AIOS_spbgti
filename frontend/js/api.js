/* ============================================================
   API-клиент AIOS.
   Единственная точка обращения фронтенда к данным.

   - getModels          — каталог моделей (GET /api/models);
   - chatStream         — потоковый чат с агентным рантаймом
                          (POST /api/chat, SSE поверх fetch);
   - listSessions / getSession / deleteSession — сессии диалогов;
   - register / resendConfirmation / login / logout / me — серверная авторизация
                          (POST /api/auth/*, Bearer-токен; подтверждение e-mail —
                          GET /api/auth/confirm по ссылке из письма);
   - cabinetGetProfile / cabinetSaveProfile — блок «Данные пользователя»
                          страницы «Кабинет» (GET|PUT /api/cabinet/profile);
                          доступен только собственный блок — по токену;
   - cabinetListSessions / cabinetDeleteSession — история сессий владельца
                         токена (GET|DELETE /api/cabinet/sessions*);
   - cabinetListProjects — проекты для раздела «Результаты работы»
                         (GET /api/cabinet/projects): свои карточки (role:
                         owner) и проекты с участием пользователя (role:
                         participant, поиск по ФИО);
    - projectsList / projectsGet — карточки проектов страницы «Проекты»
                           (GET /api/projects*), только собственные — по токену;
    - projectsUpdate — правка карточки руководителем проекта
                           (PATCH /api/projects/{id}, владелец токена);
    - projectsDelete — удаление карточки (DELETE /api/projects/{id}),
                           только роль «Администратор»;
    - pilotList — строки таблицы «Статус внедрения AIOS»
                           (GET /api/pilot/projects, публичный): активные
                           карточки всех пользователей со статусами артефактов
                           и их гипотезами;
    - pilotSetStatus — статус артефакта проекта
                           (PATCH /api/pilot/projects/{id}), только «Администратор»;

     - ontologiesList / ontologiesGet / ontologiesUpdate / ontologiesPublish —
                            онтологии навыка «Конструктор онтологий»
                            (GET|PATCH|POST /api/ontologies*), правка и публикация
                            в каталог «Данные и знания» (владелец токена);
     - ontologiesDelete — удаление онтологии (DELETE /api/ontologies/{id}),
                            только роль «Администратор»;

   - adminListUsers / adminUpdateUserRole / adminDeleteUser —
                          админ-раздел (GET|PATCH|DELETE /api/admin/*);
   - automationsList / automationsCreate / automationsUpdate / automationsDelete /
     automationsPublish / automationsRun / automationsExecutions /
     automationsRetryExecution — раздел «Автоматизации» (GET|POST|PUT|DELETE
     /api/automations*): блочный конструктор поверх воркфлоу n8n; запуск
     кнопкой и история выполнений; админу доступен ?all=1;
   - adminGetN8n / adminSaveN8n / adminTestN8n — подключение к n8n
                          (GET|PUT|POST /api/admin/n8n*), только роль
                          «Администратор»;
   - adminGetOrganization / adminSaveOrgProfile / adminSaveOrgMetrics /
      adminUploadOrgDocument / adminUpdateOrgDocument / adminDeleteOrgDocument —
                           раздел «Об организации» (GET|PUT|POST|DELETE /api/admin/organization*).

   Базовый адрес — локальный FastAPI (/api). Переход на выделенный
   бэкенд достигается изменением BASE_URL — интерфейс не меняется.
   ============================================================ */

(function () {
  var BASE_URL = '/api';

  /* Фича-флаги разделов, скрытых с фронта до готовности.
     «Автоматизации»: движок и интеграция с n8n работают, но конструктор
                     решено переделать со стека блоков на визуальную схему
                     потока (см. BACKLOG.md) — до того раздел скрыт. */
  window.AIOS_FEATURES = { automations: false };

  /** Bearer-токен вошедшего пользователя (localStorage aios-auth-v1), если есть. */
  function authToken() {
    try {
      var raw = localStorage.getItem('aios-auth-v1');
      var s = raw ? JSON.parse(raw) : null;
      return s && s.token ? s.token : null;
    } catch (e) {
      return null;
    }
  }

  function getJSON(path) {
    return fetch(BASE_URL + path, { headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      });
  }

  /**
   * Каталог моделей.
   * @param {string} [source] — 'spbti' | 'huggingface'; без параметра — весь каталог.
   * @returns {Promise<{items: Array}>}
   */
  function getModels(source) {
    var query = source ? '?source=' + encodeURIComponent(source) : '';
    return getJSON('/models' + query);
  }

  /* Тишина в стриме дольше этого времени считается обрывом соединения.
     Сервер шлёт SSE-комментарий keep-alive каждые 15 с (chat.py,
     HEARTBEAT_INTERVAL), пока модель думает; 60 с — четыре пропущенных
     сердцебиения — значит канал умер, хотя fetch этого ещё не заметил
     (NAT-таймаут, «зависший» прокси). Тогда рвём fetch сами. */
  var STREAM_IDLE_TIMEOUT = 60000;

  /**
   * Потоковый чат с рантаймом харнесса.
   * @param {Object} payload — { message, session_id?, agent_id?, skill_ids?, model? }
   * @param {Object} handlers — колбэки: onSession, onDelta, onToolCall,
   *                            onToolResult, onDone, onError, onInterrupt.
   * @returns {Promise} разрешается после окончания потока.
   *
   * Гарантия терминального события: сколько бы ни прожил стрим, вызов ровно
   * один из — onDone (сервер дописал ответ), onError (сервер сообщил об
   * ошибке) или onInterrupt (соединение умерло до конца генерации: обрыв
   * сети/прокси, watchdog по простою). Без onInterrupt обрыв транслируется
   * в onError. Раньше тихая смерть канала не вызывала ничего: промис
   * разрешался, страница агента оставалась с мигающим курсором и заблоки-
   * рованной кнопкой, хотя генерация на сервере продолжала работать в фоне.
   */
  function chatStream(payload, handlers) {
    handlers = handlers || {};
    /* если пользователь вошёл — прикладываем токен: сессия диалога привяжется
       к учётной записи (нужно для «Кабинета»: анализ своих сессий ассистентом);
       анонимный чат продолжает работать без заголовка */
    var headers = { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' };
    var token = authToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var fetchOptions = {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(payload || {})
    };
    if (controller) fetchOptions.signal = controller.signal;
    return fetch(BASE_URL + '/chat', fetchOptions).then(function (res) {
      if (!res.ok || !res.body) {
        return res.text().then(function (text) {
          /* вытаскиваем detail из JSON-ошибки API (например, 503 «LLM не настроена») */
          var detail = '';
          try {
            var data = JSON.parse(text);
            if (data && typeof data.detail === 'string') detail = ': ' + data.detail;
          } catch (e) {
            if (text) detail = ': ' + text.slice(0, 200);
          }
          var err = new Error('HTTP ' + res.status + detail);
          err.status = res.status; /* как в fail(): клиент может различать коды */
          throw err;
        });
      }
      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      var ended = false; /* терминальное событие (done/error) уже пришло */
      var watchdog = null;

      function stopWatchdog() {
        if (watchdog) {
          clearTimeout(watchdog);
          watchdog = null;
        }
      }

      function armWatchdog() {
        if (!controller) return;
        stopWatchdog();
        watchdog = setTimeout(function () {
          controller.abort(); /* read() ниже вернёт ошибку — пойдём по ветке обрыва */
        }, STREAM_IDLE_TIMEOUT);
      }

      function interrupted(err) {
        if (handlers.onInterrupt) handlers.onInterrupt(err || new Error('Соединение прервано'));
        else if (handlers.onError) {
          handlers.onError('Соединение прервано до конца ответа — генерация могла продолжаться на сервере');
        }
      }

      function dispatch(ev) {
        if (!ev || !ev.type) return;
        if (ev.type === 'done' || ev.type === 'error') ended = true;
        if (ev.type === 'session' && handlers.onSession) handlers.onSession(ev);
        else if (ev.type === 'delta' && handlers.onDelta) handlers.onDelta(ev.text);
        else if (ev.type === 'tool_call' && handlers.onToolCall) handlers.onToolCall(ev);
        else if (ev.type === 'tool_result' && handlers.onToolResult) handlers.onToolResult(ev);
        else if (ev.type === 'approval_request' && handlers.onApprovalRequest) handlers.onApprovalRequest(ev);
        else if (ev.type === 'approval_result' && handlers.onApprovalResult) handlers.onApprovalResult(ev);
        else if (ev.type === 'error' && handlers.onError) handlers.onError(ev.message);
        else if (ev.type === 'done' && handlers.onDone) handlers.onDone();
      }

      function handleFrame(frame) {
        frame.split('\n').forEach(function (line) {
          if (line.lastIndexOf('data:', 0) !== 0) return;
          var data = line.slice(5).trim();
          if (!data || data === '[DONE]') return;
          try {
            dispatch(JSON.parse(data));
          } catch (e) { /* некорректный кадр пропускаем */ }
        });
      }

      function pump() {
        armWatchdog();
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            if (buffer) handleFrame(buffer);
            stopWatchdog();
            if (!ended) interrupted(); /* канал закрылся без done — обрыв */
            return;
          }
          buffer += decoder.decode(chunk.value, { stream: true });
          var idx;
          while ((idx = buffer.indexOf('\n\n')) !== -1) {
            handleFrame(buffer.slice(0, idx));
            buffer = buffer.slice(idx + 2);
          }
          return pump();
        }, function (err) {
          /* сюда попадаем по watchdog-abort'у и при сетевой ошибке чтения */
          stopWatchdog();
          if (!ended) interrupted(err);
        });
      }

      return pump();
    });
  }

  /* ---------- восстановление ответа после обрыва стрима ---------- */

  function replyMessages(session) {
    return ((session && session.messages) || []).filter(function (m) {
      return m.role === 'assistant' && m.content;
    });
  }

  /**
   * Ждёт, пока фоновый turn допишет ответ в транскрипт сессии.
   *
   * После обрыва стрима сервер дорабатывает генерацию в фоне и дописывает
   * финальный ответ ассистента в сессию; состояние turn'а видно в самой
   * сессии (session.turn: running | done | failed — см. api/routers/chat.py).
   *
   * @param {string} sessionId
   * @param {number} knownAssistantCount — сколько ответов ассистента было
   *        в транскрипте на момент обрыва (снапшот делает recoverReply)
   * @param {Object} [opts] — { interval=4000, timeout=660000 }
   * @returns {Promise<{status: 'reply'|'failed'|'timeout', message: string}>}
   */
  function waitForReply(sessionId, knownAssistantCount, opts) {
    opts = opts || {};
    var interval = opts.interval || 4000;
    /* серверный watchdog фонового turn'а — 600 с; запас на опрос */
    var timeout = opts.timeout || 660000;
    var started = Date.now();
    return new Promise(function (resolve) {
      function done(result) { resolve(result); }
      function tick() {
        getSession(sessionId).then(function (s) {
          if (!s) return done({ status: 'timeout', message: 'Сессия не найдена' });
          var replies = replyMessages(s);
          if (replies.length > knownAssistantCount) {
            return done({ status: 'reply', message: replies[replies.length - 1].content });
          }
          var turn = s.turn;
          if (turn && turn.state === 'failed') {
            return done({ status: 'failed', message: turn.error || 'Фоновая генерация завершилась ошибкой' });
          }
          if (turn && turn.state === 'done') {
            /* turn завершён, но нового ответа нет — пустой ответ агента */
            return done({ status: 'failed', message: 'Агент завершился без ответа' });
          }
          if (Date.now() - started >= timeout) {
            return done({ status: 'timeout', message: 'Не дождались ответа' });
          }
          setTimeout(tick, interval);
        }).catch(function () {
          if (Date.now() - started >= timeout) {
            return done({ status: 'timeout', message: 'Не дождались ответа' });
          }
          setTimeout(tick, interval);
        });
      }
      setTimeout(tick, interval);
    });
  }

  /**
   * Восстановление ответа после обрыва стрима: снапшотит транскрипт и ждёт
   * фонового ответа (или честной ошибки/таймаута). Единая точка для рабочего
   * чата (workspace.js) и страницы агента (agent_page.js).
   * @returns {Promise<{status: 'reply'|'failed'|'timeout', message: string}>}
   */
  function recoverReply(sessionId) {
    return getSession(sessionId).then(function (s) {
      if (!s) return { status: 'timeout', message: 'Сессия не найдена' };
      var turn = s.turn;
      /* turn успел закончиться до первого опроса (обрыв в самом конце
         генерации) — берём готовый ответ прямо из снапшота */
      if (turn && turn.state !== 'running') {
        if (turn.state === 'failed') {
          return { status: 'failed', message: turn.error || 'Фоновая генерация завершилась ошибкой' };
        }
        var replies = replyMessages(s);
        var last = replies[replies.length - 1];
        return last
          ? { status: 'reply', message: last.content }
          : { status: 'failed', message: 'Агент завершился без ответа' };
      }
      return waitForReply(sessionId, replyMessages(s).length);
    }).catch(function () {
      return { status: 'timeout', message: 'Сессия недоступна' };
    });
  }

  /** Список сессий: {items: [{id, title, message_count, ...}]} */
  function listSessions() {
    return getJSON('/sessions');
  }

  /** Статус рантайма: настройка LLM (mode/default_model) и инструменты. */
  function getRuntime() {
    return getJSON('/runtime');
  }

  /** Подсказки навыков по тексту сообщения ({items: [{id, name, description_ru}]}).
    * Лёгкий вызов для чипов над полем ввода рабочего чата; токен передаём,
    * чтобы admin-only агенты подсказывались администратору. */
  function skillsSuggest(q, excludeIds) {
    var qs = '?q=' + encodeURIComponent(q || '');
    if (excludeIds && excludeIds.length) qs += '&exclude=' + encodeURIComponent(excludeIds.join(','));
    return request('GET', '/chat/skills-suggest' + qs, undefined, authToken());
  }

  /** Карта этапов конвейера ({stages: [{key, name_ru, skills, next}]}). */
  function getStageMap() {
    return getJSON('/stage-map');
  }

  /** Решение по карточке подтверждения действия агента (approve | decline).
    * Идентификаторы приходят из SSE-события approval_request; токен
    * прикладываем, как в chatStream: у сессии с владельцем решение
    * принимает только он. */
  function resolveApproval(sessionId, approvalId, decision) {
    var headers = { 'Content-Type': 'application/json' };
    var token = authToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    return fetch(BASE_URL + '/chat/approvals', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify({
        session_id: sessionId,
        approval_id: approvalId,
        decision: decision
      })
    }).then(function (res) {
      if (!res.ok) {
        /* 409 — запрос уже решён (например, истёк таймаут): не ошибка для UI */
        if (res.status === 409) return { ok: false, conflict: true };
        return res.json().catch(function () { return {}; }).then(function (data) {
          var detail = data && typeof data.detail === 'string' ? ': ' + data.detail : '';
          throw new Error('HTTP ' + res.status + detail);
        });
      }
      return res.json();
    });
  }

  /** Полная сессия с транскриптом. */
  function getSession(id) {
    return getJSON('/sessions/' + encodeURIComponent(id));
  }

  function deleteSession(id) {
    return fetch(BASE_URL + '/sessions/' + encodeURIComponent(id), { method: 'DELETE' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      });
  }

  /* ---------- Авторизация и админ-раздел (server-side) ---------- */

  /** Ошибка с detail от API; в err.status сохраняется HTTP-код. */
  function fail(res) {
    return res.json().catch(function () { return {}; }).then(function (data) {
      var detail = data && data.detail;
      var err = new Error(typeof detail === 'string' ? detail : 'HTTP ' + res.status);
      err.status = res.status;
      throw err;
    });
  }

  function request(method, path, body, token) {
    var headers = { 'Accept': 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (token) headers['Authorization'] = 'Bearer ' + token;
    return fetch(BASE_URL + path, {
      method: method,
      headers: headers,
      body: body !== undefined ? JSON.stringify(body) : undefined
    }).then(function (res) {
      if (res.ok) return res.json();
      return fail(res);
    });
  }

  /** Регистрация: {login, password, website?honeypot, consent_pdn, consent_policy} → {ok, email}. Письмо — через SMTP. */
  function register(login, password, website, consentPdn, consentPolicy) {
    return request('POST', '/auth/register', {
      login: login,
      password: password,
      website: website || '',
      consent_pdn: !!consentPdn,
      consent_policy: !!consentPolicy
    });
  }

  /** Повторная отправка письма подтверждения: {login, password} → {ok}. */
  function resendConfirmation(login, password) {
    return request('POST', '/auth/resend', { login: login, password: password });
  }

  /** Вход: {login, password} → {token, user}. */
  function login(login, password) {
    return request('POST', '/auth/login', { login: login, password: password });
  }

  /** Выход — инвалидация токена на сервере. */
  function logout(token) {
    return request('POST', '/auth/logout', undefined, token);
  }

  /** Профиль текущего пользователя по токену. */
  function me(token) {
    return request('GET', '/auth/me', undefined, token);
  }

  /** Расход токенов за текущие сутки и свой лимит: {used_today, limit, is_override}. */
  function usageMe(token) {
    return request('GET', '/usage/me', undefined, token);
  }

  /** Смена собственного пароля: {old, new} → {ok}. Чужие сессии отзываются. */
  function changePassword(token, oldPassword, newPassword) {
    return request('PATCH', '/auth/password', {
      old_password: oldPassword,
      new_password: newPassword
    }, token);
  }

  /** [админ] Все регистрации. */
  function adminListUsers(token) {
    return request('GET', '/admin/users', undefined, token);
  }

  /** Предложение по улучшению платформы: {message, login?} → {ok}. */
  /** Отправить предложение ({message, login?}) — {ok: true}. */
  function sendSuggestion(payload, token) {
    return request('POST', '/feedback/suggestions', payload || {}, token);
  }

  /** Предложения текущего аккаунта со статусами (GET /feedback/suggestions/mine):
      {items: [{id, message, created_at, done, done_at}]}, свежие сверху.
      done=false — «в работе», done=true — администратор отметил «выполнено». */
  function suggestMine(token) {
    return request('GET', '/feedback/suggestions/mine', undefined, token);
  }

  /** [админ] Все предложения из «Предложить изменения» (вкл. выполненные). */
  function adminListSuggestions(token) {
    return request('GET', '/feedback/suggestions', undefined, token);
  }

  /** [админ] Отметка «выполнено»/«вернуть в работу»; предложение не удаляется. */
  function adminSetSuggestionDone(token, id, done) {
    return request('PATCH', '/feedback/suggestions/' + encodeURIComponent(id), { done: !!done }, token);
  }

  /** [админ] Смена роли: 'Администратор' | 'Пользователь'. */
  function adminUpdateUserRole(token, id, role) {
    return request('PATCH', '/admin/users/' + encodeURIComponent(id), { role: role }, token);
  }

  /** [админ] Удаление пользователя. */
  function adminDeleteUser(token, id) {
    return request('DELETE', '/admin/users/' + encodeURIComponent(id), undefined, token);
  }

  /** [админ] Текущие суточные лимиты токенов: {daily_limit, default_limit, overrides_count}. */
  function adminGetLimits(token) {
    return request('GET', '/admin/limits', undefined, token);
  }

  /** [админ] Общий суточный лимит токенов на пользователя. */
  function adminSetLimits(token, dailyLimit) {
    return request('PUT', '/admin/limits', { daily_limit: dailyLimit }, token);
  }

  /** [админ] Индивидуальный суточный лимит пользователя; null — вернуть общий. */
  function adminSetUserTokenLimit(token, id, dailyTokenLimit) {
    return request('PUT', '/admin/users/' + encodeURIComponent(id) + '/token-limit',
      { daily_token_limit: dailyTokenLimit }, token);
  }

  /** [админ] Текущие настройки LLM (ключ замаскирован). */
  function adminGetLLM(token) {
    return request('GET', '/admin/llm', undefined, token);
  }

  /** [админ] Сохранить настройки LLM: {base_url, api_key, model}; пустой ключ = прежний. */
  function adminSetLLM(token, baseUrl, apiKey, model) {
    return request('PUT', '/admin/llm', { base_url: baseUrl, api_key: apiKey, model: model }, token);
  }

  /** [админ] Проверка соединения с LLM (можно с черновиком настроек). */
  function adminTestLLM(token, cfg) {
    return request('POST', '/admin/llm/test', cfg || {}, token);
  }

  /** [админ] Модели, доступные ключу на эндпоинте. */
  function adminListLLMModels(token, baseUrl, apiKey) {
    return request('POST', '/admin/llm/models', { base_url: baseUrl, api_key: apiKey }, token);
  }

  /* ---------- Рассылка писем (админ-панель) ---------- */

  /** [админ] Текущие настройки SMTP рассылки (пароль — маской). */
  function adminGetMailSettings(token) {
    return request('GET', '/admin/mail/settings', undefined, token);
  }

  /** [админ] Сохранить настройки SMTP рассылки; пустой password — прежний. */
  function adminSetMailSettings(token, cfg) {
    return request('PUT', '/admin/mail/settings', cfg || {}, token);
  }

  /** [админ] Email + ФИО всех пользователей — для чекбоксов формы рассылки. */
  function adminMailRecipients(token) {
    return request('GET', '/admin/mail/recipients', undefined, token);
  }

  /** [админ] HTML предпросмотра письма — без отправки. */
  function adminMailPreview(token, bodyText, buttonLabel, buttonUrl) {
    return request('POST', '/admin/mail/preview', {
      body_text: bodyText, button_label: buttonLabel || '', button_url: buttonUrl || ''
    }, token);
  }

  /** [админ] Отправить тестовое письмо на произвольный адрес. */
  function adminMailTest(token, to, subject, bodyText, buttonLabel, buttonUrl) {
    return request('POST', '/admin/mail/test', {
      to: to, subject: subject, body_text: bodyText,
      button_label: buttonLabel || '', button_url: buttonUrl || ''
    }, token);
  }

  /** [админ] Отправить письмо отмеченным пользователям: userIds — их id. */
  function adminMailSend(token, subject, bodyText, buttonLabel, buttonUrl, userIds) {
    return request('POST', '/admin/mail/send', {
      subject: subject, body_text: bodyText,
      button_label: buttonLabel || '', button_url: buttonUrl || '', user_ids: userIds
    }, token);
  }

  /* ---------- Напоминания по шагам пайплайна (админ-панель) ---------- */

  /** [админ] Текущая конфигурация напоминаний (глобальные тумблеры + режим). */
  function adminGetReminders(token) {
    return request('GET', '/admin/reminders', undefined, token);
  }

  /** [админ] Обновить тумблеры шагов и/или режим показа: {display_mode?, steps?}. */
  function adminSetReminders(token, patch) {
    return request('PUT', '/admin/reminders', patch || {}, token);
  }

  /** [админ] Личные оверрайды напоминаний вызывающего администратора. */
  function adminGetOwnReminderOverrides(token) {
    return request('GET', '/admin/reminders/self', undefined, token);
  }

  /** [админ] Личный оверрайд напоминаний для тестирования: {step_id: 'on'|'off'|null}. */
  function adminSetOwnReminderOverrides(token, overrides) {
    return request('PATCH', '/admin/reminders/self', { overrides: overrides || {} }, token);
  }

  /* ---------- Раздел «Об организации» (админ-панель) ---------- */

  /** [админ] Данные организации: {profile, metrics, documents, categories}. */
  function adminGetOrganization(token) {
    return request('GET', '/admin/organization', undefined, token);
  }

  /** [админ] Сохранить «Основную информацию» в исходный profile.json. */
  function adminSaveOrgProfile(token, values, source) {
    return request('PUT', '/admin/organization/profile', { values: values, source: source }, token);
  }

  /** [админ] Сохранить показатели деятельности в исходный metrics.json. */
  function adminSaveOrgMetrics(token, values, source) {
    return request('PUT', '/admin/organization/metrics', { values: values, source: source }, token);
  }

  /** [админ] Загрузить документ (multipart): файл попадает в organization/objects. */
  function adminUploadOrgDocument(token, category, title, summary, file) {
    var form = new FormData();
    form.append('category', category);
    form.append('title', title || '');
    form.append('summary', summary || '');
    form.append('file', file, file.name);
    return fetch(BASE_URL + '/admin/organization/documents', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token },
      body: form
    }).then(function (res) {
      if (res.ok) return res.json();
      return fail(res);
    });
  }

  /** [админ] Правка названия/категории/.md-описания документа. */
  function adminUpdateOrgDocument(token, id, patch) {
    return request('PUT', '/admin/organization/documents/' + encodeURIComponent(id), patch || {}, token);
  }

  /** [админ] Удалить документ (запись реестра + файл). */
  function adminDeleteOrgDocument(token, id) {
    return request('DELETE', '/admin/organization/documents/' + encodeURIComponent(id), undefined, token);
  }

  /* ---------- Кабинет (собственные данные пользователя) ---------- */

  /** Блок «Данные пользователя»: {last_name, first_name, patronymic, positions, degree, interests, updated_at}. */
  function cabinetGetProfile(token) {
    return request('GET', '/cabinet/profile', undefined, token);
  }

  /** Сохранить блок «Данные пользователя» (user/{id}-profile.json на сервере). */
  function cabinetSaveProfile(token, profile) {
    return request('PUT', '/cabinet/profile', profile || {}, token);
  }

  /** История сессий владельца токена: {items: [{id, title, created_at, updated_at, model, message_count}]}. */
  function cabinetListSessions(token) {
    return request('GET', '/cabinet/sessions', undefined, token);
  }

  /** Поиск по своей истории чатов: {items: [{id, title, snippet, updated_at}]}. */
  function cabinetSearchSessions(token, query) {
    return request('GET', '/cabinet/sessions/search?q=' + encodeURIComponent(query), undefined, token);
  }

  /** Удалить свою сессию из истории ({ok, deleted}); чужая — 404. */
  function cabinetDeleteSession(token, sessionId) {
    return request('DELETE', '/cabinet/sessions/' + encodeURIComponent(sessionId), undefined, token);
  }

  /** Проекты «Результатов работы»: {items: [...]} с role: owner|participant. */
  function cabinetListProjects(token) {
    return request('GET', '/cabinet/projects', undefined, token);
  }

  /** Таблица «Моя команда»: {items: [{user_id, full_name, position, statuses, permissions}]}. */
  function cabinetListTeam(token) {
    return request('GET', '/cabinet/team', undefined, token);
  }

  /** Выдать/снять права редактирования участнику команды ({ok, permissions}). */
  function cabinetSetTeamPermissions(token, userId, permissions) {
    return request(
      'PUT',
      '/cabinet/team/' + encodeURIComponent(userId) + '/permissions',
      { permissions: permissions || {} },
      token
    );
  }

  /* ---------- Настройки (личные данные, внешний API) ---------- */

  /** Личные настройки внешнего API (LLM): {provider, base_url,
      api_key_masked, active, llm_mode} — ключ только маской. */
  function settingsGetLLM(token) {
    return request('GET', '/settings/llm', undefined, token);
  }

  /** Сохранить личные настройки внешнего API (LLM). fields = {provider,
      base_url, api_key?, api_key2?}: пустой base_url — возврат к общим
      настройкам, пустой api_key — прежний ключ сохраняется. */
  function settingsSaveLLM(token, fields) {
    return request('PUT', '/settings/llm', fields || {}, token);
  }

  /** Удалить личные настройки внешнего API — снова общие ({ok, …}). */
  function settingsClearLLM(token) {
    return request('DELETE', '/settings/llm', undefined, token);
  }

  /** Проверить соединение с личным endpoint внешнего API
      (POST /api/settings/llm/test): draft = {base_url?, api_key?} —
      черновик из формы; пустые поля берутся из сохранённого.
      Возвращает {ok, latency_ms?, model?, error?}. */
  function settingsTestLLM(token, draft) {
    return request('POST', '/settings/llm/test', draft || {}, token);
  }

  /* ---------- Напоминания по шагам пайплайна ---------- */

  /** Активные напоминания текущего пользователя: {items: [{id, label}]}. */
  function remindersList(token) {
    return request('GET', '/reminders', undefined, token);
  }

  /* ---------- Проекты (карточки проектов пользователя) ---------- */

  /** Карточки проектов владельца токена: {items: [...]} (GET /api/projects). */
  function projectsList(token) {
    return request('GET', '/projects', undefined, token);
  }

  /** Пустая карточка-черновик владельца токена (POST /api/projects, fields — те же поля, что у PATCH). */
  function projectsCreate(token, fields) {
    return request('POST', '/projects', fields || {}, token);
  }

  /** Одна карточка проекта по id (GET /api/projects/{id}). */
  function projectsGet(token, projectId) {
    return request('GET', '/projects/' + encodeURIComponent(projectId), undefined, token);
  }

  /** Правка карточки руководителем проекта: patch — частичный словарь полей. */
  function projectsUpdate(token, projectId, patch) {
    return request('PATCH', '/projects/' + encodeURIComponent(projectId), patch || {}, token);
  }

  /** Удалить карточку — только роль «Администратор» ({ok, deleted}). */
  function projectsDelete(token, projectId) {
    return request('DELETE', '/projects/' + encodeURIComponent(projectId), undefined, token);
  }

  /** Прикрепить файл к своей карточке (multipart): kind — groundwork_files|presentation_files. */
  function projectsUploadFile(token, projectId, kind, file) {
    var form = new FormData();
    form.append('kind', kind);
    form.append('file', file, file.name);
    return fetch(BASE_URL + '/projects/' + encodeURIComponent(projectId) + '/files', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token },
      body: form
    }).then(function (res) {
      if (res.ok) return res.json();
      return fail(res);
    });
  }

  /** JSON-карточка гипотез проекта (GET /api/projects/{id}/hypotheses). */
  function projectsHypotheses(token, projectId) {
    return request('GET', '/projects/' + encodeURIComponent(projectId) + '/hypotheses', undefined, token);
  }

  /** Правка текста одной гипотезы (PATCH /api/projects/{id}/hypotheses):
      patch = {section, index, title?, statement?}; возвращает всю карточку. */
  function projectsHypothesisUpdate(token, projectId, patch) {
    return request('PATCH', '/projects/' + encodeURIComponent(projectId) + '/hypotheses', patch || {}, token);
  }

  /** Индивидуальная SVG-иконка карточки (POST /api/projects/{id}/icon).
      Идемпотентно: если иконка уже сохранена — сервер вернёт её без
      повторной генерации. Возвращает {icon_svg: str|null}. */
  function projectsGenerateIcon(token, projectId) {
    return request('POST', '/projects/' + encodeURIComponent(projectId) + '/icon', undefined, token);
  }

  /** Участники проекта и их уровни доступа (GET /api/projects/{id}/members):
      {items: [{user_id, full_name, position, level, source}], unregistered: [...]}.
      level — owner|read|write|admin; source — owner|acl|matched. */
  function projectsMembers(token, projectId) {
    return request('GET', '/projects/' + encodeURIComponent(projectId) + '/members', undefined, token);
  }

  /** Выдать/сменить уровень доступа участнику (PUT .../members/{user_id}):
      level — read|write|admin; доступно руководителю и участникам с
      «Полным доступом». Возвращает {ok, user_id, level}. */
  function projectsSetMember(token, projectId, userId, level) {
    return request(
      'PUT',
      '/projects/' + encodeURIComponent(projectId) + '/members/' + encodeURIComponent(userId),
      { level: level || 'read' },
      token
    );
  }

  /** Снять явный уровень доступа участнику (DELETE .../members/{user_id}):
      автопросмотр по ФИО остаётся, пока человек в составе команды.
      Возвращает {ok, user_id, level|null}. */
  function projectsRemoveMember(token, projectId, userId) {
    return request(
      'DELETE',
      '/projects/' + encodeURIComponent(projectId) + '/members/' + encodeURIComponent(userId),
      undefined,
      token
    );
  }

  /* ---------- Статус внедрения AIOS (таблица из карточек) ---------- */

  /**
   * Строки таблицы пилота: {items: [{id, title, lead, artifacts: {rag:
   * {status, hypothesis}, ...}}]} — публичный эндпоинт, токен не нужен.
   */
  function pilotList() {
    return getJSON('/pilot/projects');
  }

  /**
   * Статус артефакта проекта — только роль «Администратор».
   * @param {string} token — Bearer-токен администратора.
   * @param {string} projectId — id карточки (prj-…).
   * @param {string} artifact — ключ артефакта: rag|models|agents|polygon|social.
   * @param {number} status — 0..3.
   */
  function pilotSetStatus(token, projectId, artifact, status) {
    return request(
      'PATCH',
      '/pilot/projects/' + encodeURIComponent(projectId),
      { artifact: artifact, status: status },
      token
    );
  }

  /* ---------- Онтологии (навык «Конструктор онтологий») ---------- */

  /** Онтологии владельца токена: {items: [...]} — полные графы. */
  function ontologiesList(token) {
    return request('GET', '/ontologies', undefined, token);
  }

  /** Одна онтология по id (GET /api/ontologies/{id}). */
  function ontologiesGet(token, ontologyId) {
    return request('GET', '/ontologies/' + encodeURIComponent(ontologyId), undefined, token);
  }

  /** Правка онтологии владельцем (PATCH /api/ontologies/{id}, частичный словарь). */
  function ontologiesUpdate(token, ontologyId, patch) {
    return request('PATCH', '/ontologies/' + encodeURIComponent(ontologyId), patch || {}, token);
  }

  /** Опубликовать онтологию в каталог «Данные и знания» (владелец). */
  function ontologiesPublish(token, ontologyId) {
    return request('POST', '/ontologies/' + encodeURIComponent(ontologyId) + '/publish', undefined, token);
  }

  /** Удалить онтологию — только роль «Администратор» ({ok, deleted}). */
  function ontologiesDelete(token, ontologyId) {
    return request('DELETE', '/ontologies/' + encodeURIComponent(ontologyId), undefined, token);
  }

  /** RAG-базы владельца токена, свежие сверху (GET /api/rag). */
  function ragList(token) {
    return request('GET', '/rag', undefined, token);
  }

  /** Одна RAG-база владельца по id (GET /api/rag/{id}). */
  function ragGet(token, ragId) {
    return request('GET', '/rag/' + encodeURIComponent(ragId), undefined, token);
  }

  /** Правка карточки RAG-базы (частичный словарь полей). */
  function ragUpdate(token, ragId, patch) {
    return request('PATCH', '/rag/' + encodeURIComponent(ragId), patch || {}, token);
  }

  /** Опубликовать RAG-базу в каталог «Данные и знания» (владелец). */
  function ragPublish(token, ragId) {
    return request('POST', '/rag/' + encodeURIComponent(ragId) + '/publish', undefined, token);
  }

  /** Статусы документов базы — для опроса страницей загрузки. */
  function ragListDocuments(token, ragId) {
    return request('GET', '/rag/' + encodeURIComponent(ragId) + '/documents', undefined, token);
  }

  /** Сверка с текущей опубликованной версией привязанной онтологии: что
      изменилось в составе классов и сколько чанков затронет актуализация. */
  function ragOntologyCheck(token, ragId) {
    return request('GET', '/rag/' + encodeURIComponent(ragId) + '/ontology-check', undefined, token);
  }

  /** Актуализировать class_ids чанков RAG-базы под текущую онтологию
      (без пере-эмбеддинга; бэкап старых значений — на сервере). */
  function ragReclassify(token, ragId) {
    return request('POST', '/rag/' + encodeURIComponent(ragId) + '/reclassify', undefined, token);
  }

  /** Перенести документ в папку (только метка для просмотра). */
  function ragMoveDocument(token, ragId, docId, folder) {
    return request('PATCH', '/rag/' + encodeURIComponent(ragId) + '/documents/' + encodeURIComponent(docId), { folder: folder }, token);
  }

  var RAG_DOC_LABEL = { queued: 'в очереди', processing: 'обрабатывается', ready: 'готов', error: 'ошибка' };
  var RAG_FOLDER_ICO = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7.5A2.5 2.5 0 0 1 6.5 5h3l2 2.5h6A2.5 2.5 0 0 1 20 10v7a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 17z"/></svg>';
  function ragEsc(v) {
    return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /** Список документов RAG с папками, перемещением и удалением.
    * ctx: {token, ragId, refresh(), onError(msg)} */
  function ragRenderDocs(container, items, ctx) {
    if (container._busy && Date.now() - (container._busyAt || 0) < 8000) return; /* идёт перетаскивание или выбор папки — не перерисовываем, но не дольше 8 с */
    if (!items.length) {
      container.innerHTML = '<p class="mm-source">Документов пока нет — загрузите первый файл.</p>';
      return;
    }
    var collapsed = container._collapsed = container._collapsed || {};
    var sel = container._sel = container._sel || {};
    var present = {};
    items.forEach(function (d) { if (d.doc_id) present[d.doc_id] = 1; });
    Object.keys(sel).forEach(function (k) { if (!present[k]) delete sel[k]; });
    var groups = {}, order = [];
    items.forEach(function (d) {
      var f = d.folder || '';
      if (!groups[f]) { groups[f] = []; order.push(f); }
      groups[f].push(d);
    });
    order.sort(function (a, b) {
      if (a === b) return 0;
      if (!a) return -1;
      if (!b) return 1;
      return a.localeCompare(b, 'ru');
    });
    var folders = order.filter(Boolean);

    function normFolder(v) {
      return String(v || '').replace(/\\/g, '/').split('/').map(function (x) { return x.trim(); })
        .filter(function (x) { return x && x !== '.' && x !== '..'; }).join('/').slice(0, 150);
    }
    function rowHtml(d) {
      var status = d.status || 'queued';
      var act = d.doc_id && status !== 'queued' && status !== 'processing';
      var ready = act && status === 'ready';
      return '<div class="rag-doc-row' + (status === 'error' ? ' rag-doc-row--error' : '') + (ready && sel[d.doc_id] ? ' rag-doc-row--sel' : '') + '"' +
        (ready ? ' draggable="true" data-doc="' + ragEsc(d.doc_id) + '"' : '') + '>' +
        (ready ? '<input type="checkbox" class="rag-doc-chk" aria-label="Выбрать документ"' + (sel[d.doc_id] ? ' checked' : '') + '>' : '') +
        '<span class="rag-doc-name">' + ragEsc(d.filename || d.doc_id) + '</span>' +
        '<span class="rag-doc-status rag-doc-status--' + ragEsc(status) + '">' + ragEsc(RAG_DOC_LABEL[status] || status) + '</span>' +
        (ready ? '<button type="button" class="rag-doc-mv" data-doc="' + ragEsc(d.doc_id) + '" data-folder="' + ragEsc(d.folder || '') + '" title="Переместить в папку" aria-label="Переместить в папку">' + RAG_FOLDER_ICO + '</button>' : '') +
        (act ? '<button type="button" class="rag-doc-del" data-doc="' + ragEsc(d.doc_id) + '" title="Удалить документ" aria-label="Удалить документ">✕</button>' : '') +
        (d.error ? '<span class="mm-source rag-doc-err">' + ragEsc(d.error) + '</span>' : '') +
        '</div>';
    }
    var barOpts = '<option value="">Без папки</option>' + folders.map(function (f) {
      return '<option value="' + ragEsc(f) + '">' + ragEsc(f) + '</option>';
    }).join('') + '<option value="__new">＋ Новая папка…</option>';

    container.innerHTML =
      '<div class="rag-sel-bar" hidden><span class="rag-sel-count"></span>' +
      '<select class="rag-sel-target" aria-label="Папка назначения">' + barOpts + '</select>' +
      '<button type="button" class="rag-sel-go">Переместить</button>' +
      '<button type="button" class="rag-sel-clear">Снять выделение</button></div>' +
      '<div class="rag-drop-new" hidden>＋ Перетащите сюда, чтобы создать новую папку</div>' +
      order.map(function (f) {
        var rows = groups[f].map(rowHtml).join('');
        if (!f && !folders.length) return rows;
        var shut = !!collapsed[f];
        return '<div class="rag-folder-head" data-folder="' + ragEsc(f) + '">' +
          '<span class="rag-folder-caret">' + (shut ? '▸' : '▾') + '</span>' +
          '<span class="rag-folder-name">' + (f ? RAG_FOLDER_ICO + ' ' + ragEsc(f) : 'Без папки') + '</span>' +
          '<span class="rag-folder-count">' + groups[f].length + '</span></div>' +
          '<div class="rag-folder-body"' + (shut ? ' hidden' : '') + '>' + rows + '</div>';
      }).join('');

    var bar = container.querySelector('.rag-sel-bar');
    var dropNew = container.querySelector('.rag-drop-new');
    function updateBar() {
      var n = Object.keys(sel).length;
      bar.hidden = !n;
      bar.querySelector('.rag-sel-count').textContent = 'Выбрано: ' + n;
    }
    updateBar();

    function moveMany(ids, folder) {
      container._busy = true;
      container._busyAt = Date.now();
      container._moving = true;
      var chain = Promise.resolve();
      ids.forEach(function (id) {
        chain = chain.then(function () { return ragMoveDocument(ctx.token, ctx.ragId, id, folder); });
      });
      chain.then(function () {
        ids.forEach(function (id) { delete sel[id]; });
      }).catch(function (e) {
        ctx.onError('Не удалось переместить: ' + (e && e.message ? e.message : 'ошибка'));
      }).then(function () {
        container._moving = false;
        container._busy = false;
        ctx.refresh();
      });
    }
    function askNew() {
      var name = window.prompt('Название новой папки (вложенность через /):', '');
      return name === null ? '' : normFolder(name);
    }

    /* сворачивание папок */
    Array.prototype.forEach.call(container.querySelectorAll('.rag-folder-head'), function (h) {
      h.addEventListener('click', function () {
        var f = h.getAttribute('data-folder');
        collapsed[f] = !collapsed[f];
        var body = h.nextElementSibling;
        if (body) body.hidden = !!collapsed[f];
        h.querySelector('.rag-folder-caret').textContent = collapsed[f] ? '▸' : '▾';
      });
    });

    /* выделение: чекбокс и Ctrl/Cmd+клик по строке */
    function setSel(row, on) {
      var id = row.getAttribute('data-doc');
      if (on) sel[id] = 1; else delete sel[id];
      row.classList.toggle('rag-doc-row--sel', !!on);
      var chk = row.querySelector('.rag-doc-chk');
      if (chk) chk.checked = !!on;
      updateBar();
    }
    Array.prototype.forEach.call(container.querySelectorAll('.rag-doc-row[data-doc]'), function (row) {
      var chk = row.querySelector('.rag-doc-chk');
      if (chk) chk.addEventListener('change', function () { setSel(row, chk.checked); });
      row.addEventListener('click', function (e) {
        if (!(e.ctrlKey || e.metaKey)) return;
        if (e.target.closest('button, select, input')) return;
        setSel(row, !sel[row.getAttribute('data-doc')]);
      });
      row.addEventListener('dragstart', function (e) {
        var id = row.getAttribute('data-doc');
        var ids = sel[id] ? Object.keys(sel) : [id];
        container._drag = ids;
        container._busy = true;
        container._busyAt = Date.now();
        setTimeout(function () {
          if (!container._drag) return;
          container.classList.add('rag-dragging');
          dropNew.hidden = false;
        }, 0);
        e.dataTransfer.effectAllowed = 'move';
        try { e.dataTransfer.setData('text/plain', ids.join(',')); } catch (er) { /* ок */ }
      });
      row.addEventListener('dragend', function () {
        container._drag = null;
        container._busy = !!container._moving;
        container.classList.remove('rag-dragging');
        dropNew.hidden = true;
        Array.prototype.forEach.call(container.querySelectorAll('.rag-drop-over'), function (x) { x.classList.remove('rag-drop-over'); });
      });
    });

    /* цели перетаскивания: заголовок и содержимое папки, полоса «новая папка» */
    Array.prototype.forEach.call(container.querySelectorAll('.rag-folder-head'), function (h) {
      var f = h.getAttribute('data-folder');
      [h, h.nextElementSibling].forEach(function (el) {
        if (!el) return;
        el.addEventListener('dragover', function (e) {
          if (!container._drag) return;
          e.preventDefault();
          h.classList.add('rag-drop-over');
        });
        el.addEventListener('dragleave', function (e) {
          if (e.relatedTarget && el.contains(e.relatedTarget)) return;
          h.classList.remove('rag-drop-over');
        });
        el.addEventListener('drop', function (e) {
          if (!container._drag) return;
          e.preventDefault();
          h.classList.remove('rag-drop-over');
          moveMany(container._drag, f);
        });
      });
    });
    dropNew.addEventListener('dragover', function (e) {
      if (!container._drag) return;
      e.preventDefault();
      dropNew.classList.add('rag-drop-over');
    });
    dropNew.addEventListener('dragleave', function () { dropNew.classList.remove('rag-drop-over'); });
    dropNew.addEventListener('drop', function (e) {
      if (!container._drag) return;
      e.preventDefault();
      dropNew.classList.remove('rag-drop-over');
      var ids = container._drag;
      var name = askNew();
      if (name) moveMany(ids, name);
    });

    /* панель выделения */
    bar.querySelector('.rag-sel-clear').addEventListener('click', function () {
      Object.keys(sel).forEach(function (k) { delete sel[k]; });
      Array.prototype.forEach.call(container.querySelectorAll('.rag-doc-row[data-doc]'), function (row) { setSel(row, false); });
    });
    bar.querySelector('.rag-sel-go').addEventListener('click', function () {
      var ids = Object.keys(sel);
      if (!ids.length) return;
      var val = bar.querySelector('.rag-sel-target').value;
      if (val === '__new') { val = askNew(); if (!val) return; }
      moveMany(ids, val);
    });

    /* удаление */
    Array.prototype.forEach.call(container.querySelectorAll('.rag-doc-del'), function (b) {
      b.addEventListener('click', function () {
        var nm = b.parentNode.querySelector('.rag-doc-name').textContent;
        if (!window.confirm('Удалить документ «' + nm + '» из базы? Его фрагменты исчезнут из поиска.')) return;
        b.disabled = true;
        ragDeleteDocument(ctx.token, ctx.ragId, b.getAttribute('data-doc')).then(ctx.refresh).catch(function (e) {
          ctx.onError('Не удалось удалить: ' + (e && e.message ? e.message : 'ошибка'));
          b.disabled = false;
        });
      });
    });

    /* перемещение одного документа через выпадающий список */
    Array.prototype.forEach.call(container.querySelectorAll('.rag-doc-mv'), function (b) {
      b.addEventListener('click', function () {
        var cur = b.getAttribute('data-folder') || '';
        var one = document.createElement('select');
        one.className = 'rag-doc-mvsel';
        var opts = [['', 'Без папки']].concat(folders.map(function (f) { return [f, f]; }), [['__new', '＋ Новая папка…']]);
        opts.forEach(function (o) {
          var op = document.createElement('option');
          op.value = o[0]; op.textContent = o[1];
          if (o[0] === cur) op.selected = true;
          one.appendChild(op);
        });
        container._busy = true;
        function done() { container._busy = false; ctx.refresh(); }
        one.addEventListener('change', function () {
          var val = one.value;
          if (val === '__new') { val = askNew(); if (!val) { done(); return; } }
          container._busy = false;
          moveMany([b.getAttribute('data-doc')], val);
        });
        one.addEventListener('blur', function () { if (container._busy && !container._moving) setTimeout(function () { if (container._busy && !container._moving) done(); }, 150); });
        b.parentNode.replaceChild(one, b);
        one.focus();
      });
    });
  }

  /** Удалить документ из RAG-базы (чанки и запись); саму базу не трогает. */
  function ragDeleteDocument(token, ragId, docId) {
    return request('DELETE', '/rag/' + encodeURIComponent(ragId) + '/documents/' + encodeURIComponent(docId), undefined, token);
  }

  /** Загрузить документ в RAG-базу (multipart); индексация идёт фоном на сервере. */
  function ragUploadDocument(token, ragId, file) {
    var form = new FormData();
    form.append('file', file, file.name);
    return fetch(BASE_URL + '/rag/' + encodeURIComponent(ragId) + '/documents', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token },
      body: form
    }).then(function (res) {
      if (res.ok) return res.json();
      return fail(res);
    });
  }

  /** Загрузить вложение к сообщению рабочего чата (multipart, по Bearer).
    * Возвращает дескриптор {attachment_id, filename, size}: его id уходит
    * в chatStream (payload.attachment_ids). Без входа — 401 от сервера. */
  function chatUploadAttachment(file) {
    var form = new FormData();
    form.append('file', file, file.name);
    var headers = {};
    var token = authToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    return fetch(BASE_URL + '/chat/attachments', {
      method: 'POST',
      headers: headers,
      body: form
    }).then(function (res) {
      if (res.ok) return res.json();
      return fail(res);
    });
  }

  /** Удалить RAG-базу — только роль «Администратор» ({ok, deleted}). */
  function ragDelete(token, ragId) {
    return request('DELETE', '/rag/' + encodeURIComponent(ragId), undefined, token);
  }

  /** Пользовательские агенты: свои + расшаренные по проектам ({items}).
    * Доступ к агентам — из кабинета (раздел «Агенты»); карточка ведёт
    * на страницу агента /agent.html?id=… */
  function agentsList(token) {
    return request('GET', '/agents', undefined, token);
  }

  /* Обратная связь каталога агентов (страница «Агенты»): звёзды,
     публичные комментарии и автоматическая статистика применения
     в проектах (GET|POST|DELETE /api/catalog/agents*). */
  function catalogAgentsOverview() {
    return request('GET', '/catalog/agents/overview');
  }
  function catalogAgentFeedback(agentId, token) {
    return request('GET', '/catalog/agents/' + encodeURIComponent(agentId) + '/feedback', undefined, token);
  }
  function catalogAgentStar(agentId, token) {
    return request('POST', '/catalog/agents/' + encodeURIComponent(agentId) + '/star', {}, token);
  }
  function catalogCommentAdd(agentId, text, token) {
    return request('POST', '/catalog/agents/' + encodeURIComponent(agentId) + '/comments', { text: text }, token);
  }
  function catalogCommentDelete(agentId, commentId, token) {
    return request('DELETE', '/catalog/agents/' + encodeURIComponent(agentId) + '/comments/' + encodeURIComponent(commentId), undefined, token);
  }

  /* Автоматизации (исполняет self-hosted n8n, отдельного входа нет):
     блочный конструктор, публикация, запуск кнопкой, история выполнений */
  function automationsList(token, all) {
    return request('GET', '/automations' + (all ? '?all=1' : ''), undefined, token);
  }
  function automationsCreate(token, fields) {
    return request('POST', '/automations', fields, token);
  }
  function automationsUpdate(token, id, fields) {
    return request('PUT', '/automations/' + encodeURIComponent(id), fields, token);
  }
  function automationsDelete(token, id) {
    return request('DELETE', '/automations/' + encodeURIComponent(id), undefined, token);
  }
  function automationsPublish(token, id, on) {
    var suffix = on ? '/publish' : '/unpublish';
    return request('POST', '/automations/' + encodeURIComponent(id) + suffix, undefined, token);
  }
  function automationsRun(token, id) {
    return request('POST', '/automations/' + encodeURIComponent(id) + '/run', {}, token);
  }
  function automationsExecutions(token, id) {
    return request('GET', '/automations/' + encodeURIComponent(id) + '/executions?limit=20', undefined, token);
  }
  function automationsRetryExecution(token, id, execId) {
    return request('POST', '/automations/' + encodeURIComponent(id) + '/executions/' + encodeURIComponent(execId) + '/retry', {}, token);
  }
  function adminGetN8n(token) {
    return request('GET', '/admin/n8n', undefined, token);
  }
  function adminSaveN8n(token, fields) {
    return request('PUT', '/admin/n8n', fields, token);
  }
  function adminTestN8n(token, draft) {
    return request('POST', '/admin/n8n/test', draft || {}, token);
  }

  /* Модели проектов (GET|POST|PATCH|DELETE /api/models/registry*):
     каталог, паспорт версии, прогноз, график, подбор режима, данные,
     переобучение, версии, публикация, подтверждение, «прогноз → факт».
     Токен берётся из сохранённого входа; витрина открытых моделей —
     getModels (GET /api/models) — не меняется. */
  function mdlPath(id, tail) {
    return '/models/registry/' + encodeURIComponent(id) + (tail || '');
  }
  function mdlForm(path, fields, files) {
    var form = new FormData();
    Object.keys(fields || {}).forEach(function (k) {
      if (fields[k] !== undefined && fields[k] !== null) form.append(k, fields[k]);
    });
    Object.keys(files || {}).forEach(function (k) {
      if (files[k]) form.append(k, files[k], files[k].name);
    });
    var headers = {};
    var token = authToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    return fetch(BASE_URL + path, { method: 'POST', headers: headers, body: form })
      .then(function (res) {
        if (res.ok) return res.json();
        return fail(res);
      });
  }
  function teamModelsList() { return request('GET', '/models/registry', undefined, authToken()); }
  function teamModelGet(id, version) {
    return request('GET', mdlPath(id, version ? '?version=' + encodeURIComponent(version) : ''), undefined, authToken());
  }
  function teamModelPredict(id, body) { return request('POST', mdlPath(id, '/predict'), body, authToken()); }
  function teamModelProfile(id, body) { return request('POST', mdlPath(id, '/profile'), body, authToken()); }
  function teamModelOptimize(id, body) { return request('POST', mdlPath(id, '/optimize'), body, authToken()); }
  function teamModelData(id, version) {
    return request('GET', mdlPath(id, '/data' + (version ? '?version=' + encodeURIComponent(version) : '')), undefined, authToken());
  }
  function teamModelRetrain(id, fields, file) { return mdlForm(mdlPath(id, '/retrain'), fields, { file: file }); }
  function teamModelSetCurrent(id, version) { return request('POST', mdlPath(id, '/current'), { version: version }, authToken()); }
  function teamModelStatus(id, action, body) { return request('POST', mdlPath(id, '/' + action), body || {}, authToken()); }
  function teamModelFacts(id) { return request('GET', mdlPath(id, '/facts'), undefined, authToken()); }
  function teamModelAddFact(id, body) { return request('POST', mdlPath(id, '/facts'), body, authToken()); }
  function teamModelFork(id) { return request('POST', mdlPath(id, '/fork'), {}, authToken()); }
  function teamModelUpdate(id, patch) { return request('PATCH', mdlPath(id), patch, authToken()); }
  function teamModelCreate(fields, file) { return mdlForm('/models/registry/create', fields, { file: file }); }
  function teamModelImport(fields, onnxFile, holdoutFile) {
    return mdlForm('/models/registry/import', fields, { onnx: onnxFile, holdout: holdoutFile });
  }

  /** Telegram: статус привязки — {linked, username?, linked_at?, bot_username}. */
  function telegramLinkStatus(token) { return request('GET', '/auth/telegram/link', undefined, token); }
  /** Привязка по коду из /start бота: {code} → {ok, username, linked_at}. */
  function telegramLink(code, token) { return request('POST', '/auth/telegram/link', { code: code }, token); }
  /** Отвязка Telegram от учётной записи. */
  function telegramUnlink(token) { return request('DELETE', '/auth/telegram/link', undefined, token); }

  window.AIOS_API = {
    request: request,
    getModels: getModels,
    teamModelsList: teamModelsList,
    teamModelGet: teamModelGet,
    teamModelPredict: teamModelPredict,
    teamModelProfile: teamModelProfile,
    teamModelOptimize: teamModelOptimize,
    teamModelData: teamModelData,
    teamModelRetrain: teamModelRetrain,
    teamModelSetCurrent: teamModelSetCurrent,
    teamModelStatus: teamModelStatus,
    teamModelFacts: teamModelFacts,
    teamModelAddFact: teamModelAddFact,
    teamModelFork: teamModelFork,
    teamModelUpdate: teamModelUpdate,
    teamModelCreate: teamModelCreate,
    teamModelImport: teamModelImport,
    telegramLinkStatus: telegramLinkStatus,
    telegramLink: telegramLink,
    telegramUnlink: telegramUnlink,
    chatStream: chatStream,
    waitForReply: waitForReply,
    recoverReply: recoverReply,
    chatUploadAttachment: chatUploadAttachment,
    listSessions: listSessions,
    getSession: getSession,
    deleteSession: deleteSession,
    register: register,
    resendConfirmation: resendConfirmation,
    login: login,
    logout: logout,
    me: me,
    usageMe: usageMe,
    changePassword: changePassword,
    cabinetGetProfile: cabinetGetProfile,
    cabinetSaveProfile: cabinetSaveProfile,
    cabinetListSessions: cabinetListSessions,
    cabinetSearchSessions: cabinetSearchSessions,
    cabinetDeleteSession: cabinetDeleteSession,
    cabinetListProjects: cabinetListProjects,
    cabinetListTeam: cabinetListTeam,
    cabinetSetTeamPermissions: cabinetSetTeamPermissions,
    settingsGetLLM: settingsGetLLM,
    settingsSaveLLM: settingsSaveLLM,
    settingsClearLLM: settingsClearLLM,
    settingsTestLLM: settingsTestLLM,
    remindersList: remindersList,
    projectsList: projectsList,
    projectsCreate: projectsCreate,
    projectsGet: projectsGet,
    projectsUpdate: projectsUpdate,
    projectsDelete: projectsDelete,
    projectsUploadFile: projectsUploadFile,
    projectsHypotheses: projectsHypotheses,
    projectsHypothesisUpdate: projectsHypothesisUpdate,
    projectsGenerateIcon: projectsGenerateIcon,
    projectsMembers: projectsMembers,
    projectsSetMember: projectsSetMember,
    projectsRemoveMember: projectsRemoveMember,
    pilotList: pilotList,
    pilotSetStatus: pilotSetStatus,
    ontologiesList: ontologiesList,
    ontologiesGet: ontologiesGet,
    ontologiesUpdate: ontologiesUpdate,
    ontologiesPublish: ontologiesPublish,
    ontologiesDelete: ontologiesDelete,
    ragList: ragList,
    ragGet: ragGet,
    ragUpdate: ragUpdate,
    ragPublish: ragPublish,
    ragListDocuments: ragListDocuments,
    ragOntologyCheck: ragOntologyCheck,
    ragReclassify: ragReclassify,
    ragUploadDocument: ragUploadDocument,
    ragDeleteDocument: ragDeleteDocument,
    ragMoveDocument: ragMoveDocument,
    ragRenderDocs: ragRenderDocs,
    ragDelete: ragDelete,
    agentsList: agentsList,
    catalogAgentsOverview: catalogAgentsOverview,
    catalogAgentFeedback: catalogAgentFeedback,
    catalogAgentStar: catalogAgentStar,
    catalogCommentAdd: catalogCommentAdd,
    catalogCommentDelete: catalogCommentDelete,
    automationsList: automationsList,
    automationsCreate: automationsCreate,
    automationsUpdate: automationsUpdate,
    automationsDelete: automationsDelete,
    automationsPublish: automationsPublish,
    automationsRun: automationsRun,
    automationsExecutions: automationsExecutions,
    automationsRetryExecution: automationsRetryExecution,
    adminGetN8n: adminGetN8n,
    adminSaveN8n: adminSaveN8n,
    adminTestN8n: adminTestN8n,
    adminListUsers: adminListUsers,
    sendSuggestion: sendSuggestion,
    suggestMine: suggestMine,
    adminListSuggestions: adminListSuggestions,
    adminSetSuggestionDone: adminSetSuggestionDone,
    adminUpdateUserRole: adminUpdateUserRole,
    adminDeleteUser: adminDeleteUser,
    adminGetLimits: adminGetLimits,
    adminSetLimits: adminSetLimits,
    adminSetUserTokenLimit: adminSetUserTokenLimit,
    adminGetLLM: adminGetLLM,
    adminSetLLM: adminSetLLM,
    adminTestLLM: adminTestLLM,
    adminListLLMModels: adminListLLMModels,
    adminGetMailSettings: adminGetMailSettings,
    adminSetMailSettings: adminSetMailSettings,
    adminMailRecipients: adminMailRecipients,
    adminMailPreview: adminMailPreview,
    adminMailTest: adminMailTest,
    adminMailSend: adminMailSend,
    adminGetReminders: adminGetReminders,
    adminSetReminders: adminSetReminders,
    adminGetOwnReminderOverrides: adminGetOwnReminderOverrides,
    adminSetOwnReminderOverrides: adminSetOwnReminderOverrides,
    adminGetOrganization: adminGetOrganization,
    adminSaveOrgProfile: adminSaveOrgProfile,
    adminSaveOrgMetrics: adminSaveOrgMetrics,
    adminUploadOrgDocument: adminUploadOrgDocument,
    adminUpdateOrgDocument: adminUpdateOrgDocument,
    adminDeleteOrgDocument: adminDeleteOrgDocument,
    getRuntime: getRuntime,
    skillsSuggest: skillsSuggest,
    getStageMap: getStageMap,
    resolveApproval: resolveApproval
  };
})();

/* ============================================================
   Русские названия инструментов для человека.

   Технические имена (get_recipe, run_calc, …) остаются английскими:
   они уходят в OpenAI-совместимый tool-calling API, который требует
   имена вида ^[a-zA-Z0-9_-]+$ и отклоняет кириллицу; кроме того, имена
   зашиты в промптах навыков и сохранённых сессиях. Человек же должен
   видеть в интерфейсе понятное название действия агента — этот словарь
   единый источник отображаемых названий для workspace.js (карточки
   вызовов и подтверждений в чате) и agent_page.js (лента работы).
   Порядок — как в реестре: tools.py, engineering_tools.py,
   chemistry_tools.py. Для неизвестного имени показываем его как есть.
   ============================================================ */

window.AIOS_TOOL_TITLES = {
  /* встроенные (api/runtime/tools.py) */
  search_knowledge: 'Подбор баз знаний',
  get_catalog: 'Каталог AIOS',
  read_file: 'Чтение файла',
  list_files: 'Список файлов',
  write_file: 'Запись файла',
  get_organization: 'Данные об организации',
  read_document: 'Чтение документа организации',
  get_projects: 'Карточки проектов',
  save_project_card: 'Сохранение карточки проекта',
  attach_project_file: 'Прикрепление файла к проекту',
  get_ontologies: 'Онтологии',
  save_ontology: 'Сохранение онтологии',
  get_rag: 'RAG-базы',
  save_rag: 'Сохранение RAG-базы',
  get_agents: 'Список агентов',
  save_agent: 'Сохранение агента',
  search_corpus: 'Поиск по базе знаний',
  call_agent: 'Вызов другого агента',
  get_records: 'Чтение накопленных записей',
  save_record: 'Сохранение записи',
  format_table: 'Оформление таблицы',
  chart_generate: 'Построение графика',
  run_polygon: 'Прогон полигона',
  /* инженерные (api/runtime/engineering_tools.py) */
  read_data_file: 'Разбор таблицы данных',
  literature_search: 'Поиск в мировой литературе',
  compound_lookup: 'Свойства вещества',
  make_document: 'Сборка документа',
  get_recipe: 'Методика расчёта',
  notify: 'Уведомление владельцу',
  run_calc: 'Инженерный расчёт',
  /* химические (api/runtime/chemistry_tools.py) */
  chem_mol_convert: 'Представления молекулы',
  chem_descriptors: 'Дескрипторы молекулы',
  chem_functional_groups: 'Функциональные группы молекулы',
  chem_similarity: 'Сходство молекул',
  chem_substructure: 'Поиск подструктуры',
  chem_reaction_check: 'Проверка реакции',
  chem_depict: 'Структура молекулы (2D)',
  chem_control_chem_screen: 'Скрининг вещества по открытым источникам',
  chem_safety_lookup: 'Классификация опасности вещества (GHS)',
  rxn_retrosynthesis: 'План синтеза (ретросинтез)',
  rxn_forward: 'Предсказание продукта реакции',
  /* модели проектов (api/runtime/model_tools.py) */
  get_models: 'Модели проектов',
  run_model: 'Расчёт по модели',
  optimize_model: 'Подбор режима по модели',
  train_model: 'Обучение модели',
  save_model_fact: 'Замер к прогнозу модели'
};
