/* ============================================================
   API-клиент AIOS.
   Единственная точка обращения фронтенда к данным.

   - getModels          — каталог моделей (GET /api/models);
   - chatStream         — потоковый чат с агентным рантаймом
                          (POST /api/chat, SSE поверх fetch);
   - listSessions / getSession / deleteSession — сессии диалогов;
   - register / login / logout / me — серверная авторизация
                          (POST /api/auth/*, Bearer-токен);
   - adminListUsers / adminUpdateUserRole / adminDeleteUser —
                          админ-раздел (GET|PATCH|DELETE /api/admin/*).

   Базовый адрес — локальный FastAPI (/api). Переход на выделенный
   бэкенд достигается изменением BASE_URL — интерфейс не меняется.
   ============================================================ */

(function () {
  var BASE_URL = '/api';

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

  /**
   * Потоковый чат с рантаймом харнесса.
   * @param {Object} payload — { message, session_id?, agent_id?, skill_ids?, model? }
   * @param {Object} handlers — колбэки: onSession, onDelta, onToolCall,
   *                            onToolResult, onDone, onError.
   * @returns {Promise} разрешается после окончания потока.
   */
  function chatStream(payload, handlers) {
    handlers = handlers || {};
    return fetch(BASE_URL + '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body: JSON.stringify(payload || {})
    }).then(function (res) {
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

      function dispatch(ev) {
        if (!ev || !ev.type) return;
        if (ev.type === 'session' && handlers.onSession) handlers.onSession(ev);
        else if (ev.type === 'delta' && handlers.onDelta) handlers.onDelta(ev.text);
        else if (ev.type === 'tool_call' && handlers.onToolCall) handlers.onToolCall(ev);
        else if (ev.type === 'tool_result' && handlers.onToolResult) handlers.onToolResult(ev);
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
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            if (buffer) handleFrame(buffer);
            return;
          }
          buffer += decoder.decode(chunk.value, { stream: true });
          var idx;
          while ((idx = buffer.indexOf('\n\n')) !== -1) {
            handleFrame(buffer.slice(0, idx));
            buffer = buffer.slice(idx + 2);
          }
          return pump();
        });
      }

      return pump();
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

  /** Регистрация: {login, password, name} → {id, login, name, role}. */
  function register(login, password, name) {
    return request('POST', '/auth/register', { login: login, password: password, name: name });
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

  /** [админ] Смена роли: 'Администратор' | 'Пользователь'. */
  function adminUpdateUserRole(token, id, role) {
    return request('PATCH', '/admin/users/' + encodeURIComponent(id), { role: role }, token);
  }

  /** [админ] Удаление пользователя. */
  function adminDeleteUser(token, id) {
    return request('DELETE', '/admin/users/' + encodeURIComponent(id), undefined, token);
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

  window.AIOS_API = {
    getModels: getModels,
    chatStream: chatStream,
    listSessions: listSessions,
    getSession: getSession,
    deleteSession: deleteSession,
    register: register,
    login: login,
    logout: logout,
    me: me,
    changePassword: changePassword,
    adminListUsers: adminListUsers,
    adminUpdateUserRole: adminUpdateUserRole,
    adminDeleteUser: adminDeleteUser,
    adminGetLLM: adminGetLLM,
    adminSetLLM: adminSetLLM,
    adminTestLLM: adminTestLLM,
    adminListLLMModels: adminListLLMModels,
    getRuntime: getRuntime
  };
})();
