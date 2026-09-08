/* ============================================================
   Админ-панель: все регистрации AIOS.

   Доступна только роли «Администратор» (сессия в localStorage,
   ключ aios-auth-v1 — тот же, что у auth.js). Действия:
     · смена роли (Администратор ⇄ Пользователь);
     · удаление пользователя (кроме собственной учётки).
   Список приходит с GET /api/admin/users через window.AIOS_API.
   ============================================================ */
(function () {
  var body = document.getElementById('adminBody');
  if (!body) return;

  var wrap = document.getElementById('adminWrap');
  var access = document.getElementById('adminAccess');
  var accessMsg = document.getElementById('adminAccessMsg');
  var countEl = document.getElementById('adminCount');
  var refreshBtn = document.getElementById('btnRefresh');
  var msg = document.getElementById('adminMsg');

  var llmWrap = document.getElementById('llmWrap');
  var llmForm = document.getElementById('llmForm');
  var llmModels = document.getElementById('llmModels');
  var llmMsg = document.getElementById('llmMsg');
  var llmStatus = document.getElementById('llmStatus');
  var llmCurrent = document.getElementById('llmCurrent');
  var llmKeyHint = document.getElementById('llmKeyHint');
  var btnLoadModels = document.getElementById('btnLoadModels');
  var btnTestLLM = document.getElementById('btnTestLLM');

  var AUTH_KEY = 'aios-auth-v1';
  var API = window.AIOS_API;

  function getSession() {
    try {
      var raw = localStorage.getItem(AUTH_KEY);
      var s = raw ? JSON.parse(raw) : null;
      return s && s.token ? s : null;
    } catch (e) {
      return null;
    }
  }

  var session = getSession();

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmtDate(ts) {
    if (!ts) return '—';
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '—';
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear() +
      ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function deny(text) {
    if (wrap) wrap.hidden = true;
    if (access) access.hidden = false;
    if (accessMsg) accessMsg.textContent = text;
  }

  function showMsg(text) {
    if (!msg) return;
    msg.textContent = text;
    msg.hidden = false;
  }

  function hideMsg() {
    if (msg) msg.hidden = true;
  }

  function render(users) {
    hideMsg();
    if (countEl) countEl.textContent = String(users.length);
    var rows = users.map(function (u, i) {
      var self = !!(session && session.login && u.login === session.login);
      var isAdmin = u.role === 'Администратор';
      var rolePill = isAdmin
        ? '<span class="role-pill role-pill--admin">Администратор</span>'
        : '<span class="role-pill role-pill--user">Пользователь</span>';
      var toggleTitle = isAdmin ? 'Сделать пользователем' : 'Сделать админом';
      var nextRole = isAdmin ? 'Пользователь' : 'Администратор';
      var actions = self
        ? '<span class="at-self">это вы</span>'
        : '<button class="adm-btn" type="button" data-act="role" data-id="' + esc(u.id) +
            '" data-role="' + esc(nextRole) + '">' + esc(toggleTitle) + '</button>' +
          '<button class="adm-btn adm-btn--danger" type="button" data-act="del" data-id="' + esc(u.id) +
            '" data-name="' + esc(u.name || u.login) + '">Удалить</button>';
      return '<tr>' +
        '<td class="at-num">' + (i + 1) + '</td>' +
        '<td class="at-name">' + esc(u.name || u.login) + '</td>' +
        '<td class="at-login">' + esc(u.login) + '</td>' +
        '<td>' + rolePill + '</td>' +
        '<td class="at-date">' + fmtDate(u.created_at) + '</td>' +
        '<td class="at-date">' + fmtDate(u.last_login) + '</td>' +
        '<td class="at-actions"><div class="adm-actions">' + actions + '</div></td>' +
        '</tr>';
    });
    body.innerHTML = rows.join('');
  }

  function load() {
    if (!API || !API.adminListUsers) {
      deny('API недоступен: запустите бэкенд (uvicorn api.main:app).');
      return;
    }
    API.adminListUsers(session.token)
      .then(function (res) { render(res.items || []); })
      .catch(function (err) {
        if (err && err.status === 401) {
          try { localStorage.removeItem(AUTH_KEY); } catch (e) {}
          deny('Сессия истекла. Войдите заново под учётной записью администратора на главной странице.');
        } else if (err && err.status === 403) {
          deny('Доступ только для администраторов.');
        } else {
          deny('Не удалось загрузить список регистраций: ' + (err && err.message ? err.message : 'ошибка сети'));
        }
      });
  }

  /* делегирование: кнопки «Сменить роль» / «Удалить» */
  body.addEventListener('click', function (e) {
    var btnEl = e.target.closest ? e.target.closest('button[data-act]') : null;
    if (!btnEl) return;
    var act = btnEl.getAttribute('data-act');
    var id = btnEl.getAttribute('data-id');

    if (act === 'role') {
      var nextRole = btnEl.getAttribute('data-role');
      hideMsg();
      API.adminUpdateUserRole(session.token, id, nextRole)
        .then(load)
        .catch(function (err) { showMsg(err && err.message ? err.message : 'Не удалось сменить роль'); });
    } else if (act === 'del') {
      var name = btnEl.getAttribute('data-name');
      if (!window.confirm('Удалить пользователя «' + name + '»? Действие необратимо.')) return;
      hideMsg();
      API.adminDeleteUser(session.token, id)
        .then(load)
        .catch(function (err) { showMsg(err && err.message ? err.message : 'Не удалось удалить пользователя'); });
    }
  });

  if (refreshBtn) {
    refreshBtn.addEventListener('click', load);
  }

  /* ---------- настройки внешнего LLM-API ---------- */

  function llmShowMsg(text) {
    if (!llmMsg) return;
    llmMsg.textContent = text;
    llmMsg.hidden = false;
  }

  function llmHideMsg() {
    if (llmMsg) llmMsg.hidden = true;
  }

  function llmShowStatus(text) {
    if (!llmStatus) return;
    llmStatus.textContent = text;
    llmStatus.hidden = !text;
  }

  function llmRenderCurrent(cfg) {
    if (!llmCurrent) return;
    if (!cfg || !cfg.configured) {
      llmCurrent.textContent = 'LLM не настроена — рабочий чат недоступен (503)';
      return;
    }
    var source = cfg.source === 'env' ? 'из .env' : 'задано админом';
    var key = cfg.api_key_masked ? (' · ключ ' + cfg.api_key_masked) : '';
    llmCurrent.textContent = 'Сейчас: ' + cfg.model + ' @ ' + cfg.base_url + ' (' + source + ')' + key;
  }

  function llmLoad() {
    if (!API || !API.adminGetLLM) return;
    API.adminGetLLM(session.token)
      .then(function (cfg) {
        llmRenderCurrent(cfg);
        if (llmForm) {
          if (cfg.base_url) llmForm.elements.base_url.value = cfg.base_url;
          if (cfg.model) llmForm.elements.model.value = cfg.model;
        }
        if (llmKeyHint && cfg.api_key_masked) llmKeyHint.textContent = '(сохранён ' + cfg.api_key_masked + ')';
      })
      .catch(function (err) {
        llmRenderCurrent(null);
      });
  }

  if (btnLoadModels) {
    btnLoadModels.addEventListener('click', function () {
      if (!API || !API.adminListLLMModels || !llmForm) return;
      var baseUrl = llmForm.elements.base_url.value.trim();
      var apiKey = llmForm.elements.api_key.value.trim();
      if (!baseUrl) { llmShowMsg('Сначала укажите endpoint (Base URL)'); return; }
      llmHideMsg();
      llmShowStatus('Загрузка списка моделей…');
      btnLoadModels.disabled = true;
      API.adminListLLMModels(session.token, baseUrl, apiKey)
        .then(function (res) {
          var items = (res && res.items) || [];
          if (llmModels) {
            llmModels.innerHTML = items.map(function (m) {
              return '<option value="' + esc(m.id) + '"></option>';
            }).join('');
          }
          llmShowStatus('Найдено моделей: ' + items.length + ' — выберите в списке или введите вручную');
        })
        .catch(function (err) {
          llmShowStatus('');
          llmShowMsg(err && err.message ? err.message : 'Не удалось загрузить модели — введите имя вручную');
        })
        .then(function () { btnLoadModels.disabled = false; });
    });
  }

  if (btnTestLLM) {
    btnTestLLM.addEventListener('click', function () {
      if (!API || !API.adminTestLLM || !llmForm) return;
      llmHideMsg();
      llmShowStatus('Проверка соединения…');
      btnTestLLM.disabled = true;
      var draft = {
        base_url: llmForm.elements.base_url.value.trim(),
        api_key: llmForm.elements.api_key.value.trim(),
        model: llmForm.elements.model.value.trim()
      };
      API.adminTestLLM(session.token, draft)
        .then(function (res) {
          if (res && res.ok) {
            llmShowStatus('✓ Соединение работает (' + res.latency_ms + ' мс' + (res.model ? ', ' + res.model : '') + ')');
          } else {
            llmShowStatus('');
            llmShowMsg((res && res.error) ? res.error : 'Проверка не удалась');
          }
        })
        .catch(function (err) {
          llmShowStatus('');
          llmShowMsg(err && err.message ? err.message : 'Проверка не удалась');
        })
        .then(function () { btnTestLLM.disabled = false; });
    });
  }

  if (llmForm) {
    llmForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (!API || !API.adminSetLLM) return;
      llmHideMsg();
      var saveBtn = llmForm.querySelector('.llm-save');
      if (saveBtn) saveBtn.disabled = true;
      API.adminSetLLM(
        session.token,
        llmForm.elements.base_url.value.trim(),
        llmForm.elements.api_key.value.trim(),
        llmForm.elements.model.value.trim()
      )
        .then(function (cfg) {
          llmRenderCurrent(cfg);
          if (llmKeyHint && cfg.api_key_masked) llmKeyHint.textContent = '(сохранён ' + cfg.api_key_masked + ')';
          llmShowStatus('✓ Настройки сохранены — модель применена ко всем пользователям');
        })
        .catch(function (err) {
          llmShowStatus('');
          llmShowMsg(err && err.message ? err.message : 'Не удалось сохранить настройки');
        })
        .then(function () { if (saveBtn) saveBtn.disabled = false; });
    });
  }

  /* контроль доступа */
  if (!session) {
    deny('Доступ только для администраторов. Войдите на главной странице.');
    return;
  }
  if (session.role !== 'Администратор') {
    deny('Доступ только для администраторов.');
    return;
  }
  if (wrap) wrap.hidden = false;
  if (llmWrap) llmWrap.hidden = false;
  llmLoad();
  load();
})();
