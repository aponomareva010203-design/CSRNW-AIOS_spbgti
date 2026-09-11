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
    - pilotList — строки таблицы «Статус пилотного проекта»
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
   - adminGetOrganization / adminSaveOrgProfile / adminSaveOrgMetrics /
      adminUploadOrgDocument / adminUpdateOrgDocument / adminDeleteOrgDocument —
                           раздел «Об организации» (GET|PUT|POST|DELETE /api/admin/organization*).

   Базовый адрес — локальный FastAPI (/api). Переход на выделенный
   бэкенд достигается изменением BASE_URL — интерфейс не меняется.
   ============================================================ */

(function () {
  var BASE_URL = '/api';

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

  /**
   * Потоковый чат с рантаймом харнесса.
   * @param {Object} payload — { message, session_id?, agent_id?, skill_ids?, model? }
   * @param {Object} handlers — колбэки: onSession, onDelta, onToolCall,
   *                            onToolResult, onDone, onError.
   * @returns {Promise} разрешается после окончания потока.
   */
  function chatStream(payload, handlers) {
    handlers = handlers || {};
    /* если пользователь вошёл — прикладываем токен: сессия диалога привяжется
       к учётной записи (нужно для «Кабинета»: анализ своих сессий ассистентом);
       анонимный чат продолжает работать без заголовка */
    var headers = { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' };
    var token = authToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    return fetch(BASE_URL + '/chat', {
      method: 'POST',
      headers: headers,
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

  /** Регистрация: {login, password, name?} → {ok, email}. Письмо — через SMTP. */
  function register(login, password) {
    return request('POST', '/auth/register', { login: login, password: password });
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
  function sendSuggestion(payload, token) {
    return request('POST', '/feedback/suggestions', payload, token);
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

  /* ---------- Статус пилотного проекта (таблица из карточек) ---------- */

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

  window.AIOS_API = {
    getModels: getModels,
    chatStream: chatStream,
    chatUploadAttachment: chatUploadAttachment,
    listSessions: listSessions,
    getSession: getSession,
    deleteSession: deleteSession,
    register: register,
    resendConfirmation: resendConfirmation,
    login: login,
    logout: logout,
    me: me,
    changePassword: changePassword,
    cabinetGetProfile: cabinetGetProfile,
    cabinetSaveProfile: cabinetSaveProfile,
    cabinetListSessions: cabinetListSessions,
    cabinetDeleteSession: cabinetDeleteSession,
    cabinetListProjects: cabinetListProjects,
    cabinetListTeam: cabinetListTeam,
    cabinetSetTeamPermissions: cabinetSetTeamPermissions,
    projectsList: projectsList,
    projectsCreate: projectsCreate,
    projectsGet: projectsGet,
    projectsUpdate: projectsUpdate,
    projectsDelete: projectsDelete,
    projectsUploadFile: projectsUploadFile,
    projectsHypotheses: projectsHypotheses,
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
    ragUploadDocument: ragUploadDocument,
    ragDelete: ragDelete,
    adminListUsers: adminListUsers,
    sendSuggestion: sendSuggestion,
    adminUpdateUserRole: adminUpdateUserRole,
    adminDeleteUser: adminDeleteUser,
    adminGetLLM: adminGetLLM,
    adminSetLLM: adminSetLLM,
    adminTestLLM: adminTestLLM,
    adminListLLMModels: adminListLLMModels,
    adminGetOrganization: adminGetOrganization,
    adminSaveOrgProfile: adminSaveOrgProfile,
    adminSaveOrgMetrics: adminSaveOrgMetrics,
    adminUploadOrgDocument: adminUploadOrgDocument,
    adminUpdateOrgDocument: adminUpdateOrgDocument,
    adminDeleteOrgDocument: adminDeleteOrgDocument,
    getRuntime: getRuntime
  };
})();
