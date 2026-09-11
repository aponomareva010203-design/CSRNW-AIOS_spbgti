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
  var spendEl = document.getElementById('adminSpend');
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
  var remWrap = document.getElementById('remWrap');
  var remRows = document.getElementById('remRows');
  var remStatus = document.getElementById('remStatus');
  var remMsg = document.getElementById('remMsg');
  var btnRemSave = document.getElementById('btnRemSave');

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

  function fmtNum(n) {
    var v = Math.round(Number(n) || 0);
    return String(v).replace(/\B(?=(\d{3})+(?!\d))/g, '\u2009');
  }

  function fmtTokens(u) {
    var total = (Number(u.tokens_prompt) || 0) + (Number(u.tokens_completion) || 0);
    if (!total) return '<span class="at-tokens-zero">0</span>';
    return '<span title="Вход: ' + fmtNum(u.tokens_prompt) +
      ' · Выход: ' + fmtNum(u.tokens_completion) +
      ' · Запросов: ' + fmtNum(u.tokens_requests || 0) + '">' +
      fmtNum(total) + '</span>';
  }

  function ctxCell(u) {
    if (!u.hidden_context) return '<span class="at-ctx-empty">—</span>';
    return '<button class="adm-btn adm-btn--ctx" type="button" data-act="ctx" data-id="' + esc(u.id) +
      '">Показать</button>';
  }

  function renderTotals(t) {
    if (!spendEl) return;
    if (!t) { spendEl.hidden = true; return; }
    var usersTotal = (Number(t.prompt) || 0) + (Number(t.completion) || 0);
    var bg = t.background || {};
    var bgTotal = (Number(bg.prompt) || 0) + (Number(bg.completion) || 0);
    spendEl.textContent = 'Расход токенов платформой: ' + fmtNum(usersTotal + bgTotal) +
      ' — пользователи: ' + fmtNum(usersTotal) +
      ' · фоновые агенты: ' + fmtNum(bgTotal) +
      ' (запросов к LLM: ' + fmtNum((Number(t.requests) || 0) + (Number(bg.requests) || 0)) + ')';
    spendEl.hidden = false;
  }

  function render(users, totals) {
    hideMsg();
    renderTotals(totals);
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
        '<td class="at-tokens">' + fmtTokens(u) + '</td>' +
        '<td class="at-ctx">' + ctxCell(u) + '</td>' +
        '<td class="at-actions"><div class="adm-actions">' + actions + '</div></td>' +
        '</tr>' +
        (u.hidden_context
          ? '<tr class="ctx-row" data-ctx-for="' + esc(u.id) + '" hidden><td colspan="9">' +
            '<div class="ctx-row-head">Скрытый контекст о пользователе (собран платформой автоматически' +
            (u.context_generated_at ? ', обновлён ' + fmtDate(u.context_generated_at) : '') +
            '):</div>' +
            '<div class="ctx-row-text">' + esc(u.hidden_context) + '</div>' +
            '</td></tr>'
          : '');
    });
    body.innerHTML = rows.join('');
  }

  function load() {
    if (!API || !API.adminListUsers) {
      deny('API недоступен: запустите бэкенд (uvicorn api.main:app).');
      return;
    }
    API.adminListUsers(session.token)
      .then(function (res) { render(res.items || [], res.platform_totals || null); })
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

    if (act === 'ctx') {
      var row = body.querySelector('tr[data-ctx-for="' + id + '"]');
      if (row) {
        row.hidden = !row.hidden;
        btnEl.textContent = row.hidden ? 'Показать' : 'Скрыть';
      }
      return;
    }

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

  /* ---------- напоминания по шагам пайплайна ---------- */

  var REM_STEP_ORDER = ['onboarding', 'hypotheses', 'ontology', 'rag', 'models', 'agents', 'polygon', 'social'];

  function remShowMsg(text) {
    if (!remMsg) return;
    remMsg.textContent = text;
    remMsg.hidden = false;
  }

  function remHideMsg() {
    if (remMsg) remMsg.hidden = true;
  }

  function remShowStatus(text) {
    if (!remStatus) return;
    remStatus.textContent = text;
    remStatus.hidden = !text;
  }

  function remRenderRows(cfg, overrides) {
    if (!remRows) return;
    var steps = (cfg && cfg.steps) || {};
    overrides = overrides || {};
    remRows.innerHTML = REM_STEP_ORDER.map(function (stepId) {
      var step = steps[stepId] || {};
      var checked = step.enabled_global ? ' checked' : '';
      var ov = overrides[stepId] || '';
      var optOn = ov === 'on' ? ' selected' : '';
      var optOff = ov === 'off' ? ' selected' : '';
      var optDefault = ov ? '' : ' selected';
      return '<tr data-step="' + stepId + '">' +
        '<td>' + esc(step.label || stepId) + '</td>' +
        '<td><input type="checkbox" class="rem-global"' + checked + '></td>' +
        '<td><select class="rem-self">' +
          '<option value=""' + optDefault + '>обычно</option>' +
          '<option value="on"' + optOn + '>вкл (тест)</option>' +
          '<option value="off"' + optOff + '>выкл (тест)</option>' +
        '</select></td>' +
      '</tr>';
    }).join('');
  }

  function remLoad() {
    if (!API || !API.adminGetReminders) return;
    var overridesCall = API.adminGetOwnReminderOverrides
      ? API.adminGetOwnReminderOverrides(session.token)
      : Promise.resolve({ reminder_overrides: {} });
    Promise.all([API.adminGetReminders(session.token), overridesCall])
      .then(function (results) {
        var cfg = results[0];
        var overrides = (results[1] && results[1].reminder_overrides) || {};
        remRenderRows(cfg, overrides);
      })
      .catch(function (err) {
        remShowMsg(err && err.message ? err.message : 'Не удалось загрузить напоминания');
      });
  }

  if (btnRemSave) {
    btnRemSave.addEventListener('click', function () {
      if (!API || !API.adminSetReminders || !remRows) return;
      remHideMsg();
      var steps = {};
      remRows.querySelectorAll('tr[data-step]').forEach(function (row) {
        var stepId = row.getAttribute('data-step');
        var checkbox = row.querySelector('.rem-global');
        steps[stepId] = { enabled_global: !!(checkbox && checkbox.checked) };
      });
      btnRemSave.disabled = true;
      API.adminSetReminders(session.token, { steps: steps })
        .then(function () {
          remShowStatus('✓ Настройки сохранены — применяются ко всем пользователям');
          remLoad();
        })
        .catch(function (err) {
          remShowStatus('');
          remShowMsg(err && err.message ? err.message : 'Не удалось сохранить настройки');
        })
        .then(function () { btnRemSave.disabled = false; });
    });
  }

  if (remRows) {
    remRows.addEventListener('change', function (e) {
      var select = e.target && e.target.closest ? e.target.closest('.rem-self') : null;
      if (!select) return;
      var row = select.closest('tr[data-step]');
      var stepId = row && row.getAttribute('data-step');
      if (!stepId || !API || !API.adminSetOwnReminderOverrides) return;
      var patch = {};
      patch[stepId] = select.value || null;
      API.adminSetOwnReminderOverrides(session.token, patch)
        .catch(function (err) {
          remShowMsg(err && err.message ? err.message : 'Не удалось сохранить личную настройку');
        });
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
  if (remWrap) remWrap.hidden = false;
  llmLoad();
  remLoad();
  load();

  /* сворачиваемые разделы: кнопка-заголовок прячет/показывает тело
     («Настройки внешних API», «Об организации», «Основные документы») */
  [['llmToggle', 'llmBody'], ['remToggle', 'remBody'], ['orgToggle', 'orgBody'], ['orgDocsToggle', 'orgDocsGroups']]
    .forEach(function (pair) {
      var toggle = document.getElementById(pair[0]);
      var target = document.getElementById(pair[1]);
      if (!toggle || !target) return;
      toggle.addEventListener('click', function () {
        var open = toggle.getAttribute('aria-expanded') !== 'false';
        toggle.setAttribute('aria-expanded', open ? 'false' : 'true');
        target.hidden = open;
      });
    });
})();
