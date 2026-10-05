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
  var limWrap = document.getElementById('limWrap');
  var limForm = document.getElementById('limForm');
  var limStatus = document.getElementById('limStatus');
  var limMsg = document.getElementById('limMsg');

  var mailWrap = document.getElementById('mailWrap');
  var mailSettingsForm = document.getElementById('mailSettingsForm');
  var mailSettingsStatus = document.getElementById('mailSettingsStatus');
  var mailSettingsMsg = document.getElementById('mailSettingsMsg');
  var mailPassHint = document.getElementById('mailPassHint');
  var mailComposeForm = document.getElementById('mailComposeForm');
  var mailBodyText = document.getElementById('mailBodyText');
  var mailButtonLabel = document.getElementById('mailButtonLabel');
  var mailButtonUrl = document.getElementById('mailButtonUrl');
  var mailPreviewFrame = document.getElementById('mailPreviewFrame');
  var btnMailPreview = document.getElementById('btnMailPreview');
  var mailRecipients = document.getElementById('mailRecipients');
  var mailRecipientsCount = document.getElementById('mailRecipientsCount');
  var btnMailSelectAll = document.getElementById('btnMailSelectAll');
  var btnMailSelectNone = document.getElementById('btnMailSelectNone');
  var mailTestTo = document.getElementById('mailTestTo');
  var btnMailTest = document.getElementById('btnMailTest');
  var btnMailSend = document.getElementById('btnMailSend');
  var mailSendStatus = document.getElementById('mailSendStatus');
  var mailSendMsg = document.getElementById('mailSendMsg');

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

  function fmtLimitShort(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (Math.round(n / 1e5) / 10).toFixed(1).replace('.', ',') + ' млн';
    if (n >= 1e4) return Math.round(n / 1e3) + ' тыс.';
    return fmtNum(n);
  }

  /* суточные лимиты: общее значение подтягивается adminGetLimits (limLoad) */
  var limitsCfg = null;
  var lastUsers = null; /* кэш последнего рендера — откат inline-правки лимита */
  var lastTotals = null;
  var tokenSort = null; /* null | 'desc' | 'asc' — сортировка «Токены» (рейтинг токенмаксинга) */
  var thTokens = document.getElementById('thTokens');
  var tokensSortArrow = document.getElementById('tokensSortArrow');

  function totalTokens(u) {
    return (Number(u.tokens_prompt) || 0) + (Number(u.tokens_completion) || 0);
  }

  function rankCell(i) {
    var place = i + 1;
    var medal = place === 1 ? '\ud83e\udd47' : place === 2 ? '\ud83e\udd48' : place === 3 ? '\ud83e\udd49' : '';
    return medal
      ? '<span class="at-rank-medal" title="Место ' + place + ' в рейтинге токенмаксинга">' + medal + '</span>'
      : String(place);
  }

  function updateSortHeader() {
    if (!tokensSortArrow) return;
    tokensSortArrow.textContent = tokenSort === 'desc' ? '\u25bc' : tokenSort === 'asc' ? '\u25b2' : '';
    if (thTokens) thTokens.classList.toggle('at-sort-active', !!tokenSort);
  }

  function fmtTokens(u) {
    var total = (Number(u.tokens_prompt) || 0) + (Number(u.tokens_completion) || 0);
    var today = Number(u.tokens_today) || 0;
    var todayHtml = '<div class="at-tokens-today">за сутки: ' + fmtNum(today) + '</div>';
    if (!total) return '<span class="at-tokens-zero">0</span>' + todayHtml;
    return '<span title="Вход: ' + fmtNum(u.tokens_prompt) +
      ' · Выход: ' + fmtNum(u.tokens_completion) +
      ' · Запросов: ' + fmtNum(u.tokens_requests || 0) + '">' +
      fmtNum(total) + '</span>' + todayHtml;
  }

  /* ячейка «Внешний API»: общие настройки платформы либо личные настройки
     пользователя (Настройки → Внешний API — свой endpoint и ключ) */
  function llmCell(u) {
    if (u.personal_llm) {
      return '<span class="at-llm-value at-llm-value--personal" title="Личные настройки внешнего API: пользователь работает через свой endpoint и ключ">личные</span>';
    }
    return '<span class="at-llm-value" title="Общие настройки платформы (админ-панель → Настройки внешних API)">общие</span>';
  }

  /* ячейка «Суточный лимит»: индивидуальный оверрайд либо общий лимит платформы.
     Для пользователя с личным внешним API лимит не действует (свой ключ) —
     так и подписываем; оверрайд всё равно можно задать на будущее. */
  function limitCell(u) {
    if (u.personal_llm) {
      return '<span class="at-limit-value" title="Суточный лимит не применяется: пользователь работает по личному ключу внешнего API">без лимита</span> ' +
        '<span class="at-limit-kind">личный API</span>' +
        '<button class="adm-btn adm-btn--limit" type="button" data-act="limit" data-id="' + esc(u.id) +
        '">Изменить</button>';
    }
    var isOverride = u.daily_token_limit != null;
    var value = isOverride ? u.daily_token_limit : (limitsCfg ? limitsCfg.daily_limit : null);
    var text = value == null ? '—' : fmtLimitShort(value);
    var title = value == null ? '' : (isOverride
      ? 'Индивидуальный лимит: ' + fmtNum(value) + ' токенов в сутки'
      : 'Общий лимит платформы: ' + fmtNum(value) + ' токенов в сутки');
    return '<span class="at-limit-value"' + (title ? ' title="' + esc(title) + '"' : '') + '>' +
      esc(text) + '</span> <span class="at-limit-kind">' + (isOverride ? 'индивид.' : 'общий') + '</span>' +
      '<button class="adm-btn adm-btn--limit" type="button" data-act="limit" data-id="' + esc(u.id) +
      '">Изменить</button>';
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
    lastUsers = users;
    lastTotals = totals;
    hideMsg();
    renderTotals(totals);
    if (countEl) countEl.textContent = String(users.length);
    renderTable();
  }

  function renderTable() {
    updateSortHeader();
    var ranked = !!tokenSort;
    var users = (lastUsers || []).slice();
    if (ranked) {
      users.sort(function (a, b) {
        var diff = totalTokens(b) - totalTokens(a);
        return tokenSort === 'asc' ? -diff : diff;
      });
    }
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
      var prev = users[i - 1];
      var groupStart = !ranked && i > 0 && (prev ? (prev.project_id || null) : null) !== (u.project_id || null);
      var rowCls = groupStart ? ' class="adm-group-start"' : '';
      var positions = (u.positions || []).join('; ');
      var projectCell = u.project_id
        ? esc(u.project_title) + (u.project_role === 'lead'
            ? ' <span class="at-project-role at-project-role--lead">рук.</span>'
            : ' <span class="at-project-role">уч.</span>')
        : '<span class="at-project-empty">—</span>';
      return '<tr' + rowCls + '>' +
        '<td class="at-num">' + (ranked ? rankCell(i) : (i + 1)) + '</td>' +
        '<td class="at-name">' + esc(u.name || u.login) +
          (ranked ? ' <span class="at-name-tokens">— ' + fmtNum(totalTokens(u)) + '</span>' : '') +
        '</td>' +
        '<td class="at-fio">' + (esc(u.fio) || '<span class="at-project-empty">—</span>') + '</td>' +
        '<td class="at-position">' + (esc(positions) || '<span class="at-project-empty">—</span>') + '</td>' +
        '<td class="at-login">' + esc(u.login) + '</td>' +
        '<td class="at-project">' + projectCell + '</td>' +
        '<td class="at-stage">' + esc(u.stage_label || '—') + '</td>' +
        '<td>' + rolePill + '</td>' +
        '<td class="at-date">' + fmtDate(u.created_at) + '</td>' +
        '<td class="at-date">' + fmtDate(u.last_login) + '</td>' +
        '<td class="at-tokens">' + fmtTokens(u) + '</td>' +
        '<td class="at-llm">' + llmCell(u) + '</td>' +
        '<td class="at-limit">' + limitCell(u) + '</td>' +
        '<td class="at-ctx">' + ctxCell(u) + '</td>' +
        '<td class="at-actions"><div class="adm-actions">' + actions + '</div></td>' +
        '</tr>' +
        (u.hidden_context
          ? '<tr class="ctx-row" data-ctx-for="' + esc(u.id) + '" hidden><td colspan="15">' +
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

    if (act === 'limit') {
      /* inline-правка суточного лимита прямо в ячейке: пустое значение
         означает «вернуть общий лимит платформы» */
      var cell = btnEl.closest('td');
      if (!cell) return;
      var target = (lastUsers || []).filter(function (x) { return x.id === id; })[0];
      var current = target && target.daily_token_limit != null ? String(target.daily_token_limit) : '';
      cell.innerHTML =
        '<input type="number" class="lim-input" min="1" step="1000" value="' + esc(current) + '" placeholder="общий">' +
        '<button class="adm-btn" type="button" data-act="limit-save" data-id="' + esc(id) + '">✓</button>' +
        '<button class="adm-btn" type="button" data-act="limit-cancel">✕</button>' +
        '<div class="at-limit-hint">пусто — общий</div>';
      var limInput = cell.querySelector('input');
      if (limInput) { limInput.focus(); limInput.select(); }
      return;
    }

    if (act === 'limit-cancel') {
      render(lastUsers || [], lastTotals);
      return;
    }

    if (act === 'limit-save') {
      var saveCell = btnEl.closest('td');
      var saveInput = saveCell ? saveCell.querySelector('input') : null;
      var raw = saveInput ? saveInput.value.trim() : '';
      var limitValue = raw === '' ? null : parseInt(raw, 10);
      if (limitValue !== null && (!isFinite(limitValue) || limitValue < 1)) {
        showMsg('Лимит должен быть положительным числом токенов (пусто — общий лимит)');
        return;
      }
      hideMsg();
      btnEl.disabled = true;
      API.adminSetUserTokenLimit(session.token, id, limitValue)
        .then(load)
        .catch(function (err) {
          showMsg(err && err.message ? err.message : 'Не удалось сохранить лимит');
          btnEl.disabled = false;
        });
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

  if (thTokens) {
    thTokens.addEventListener('click', function () {
      tokenSort = tokenSort === 'desc' ? 'asc' : tokenSort === 'asc' ? null : 'desc';
      renderTable();
    });
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

  /* ---------- n8n: движок автоматизаций ---------- */

  var n8nWrap = document.getElementById('n8nWrap');
  var n8nForm = document.getElementById('n8nForm');
  var n8nMsg = document.getElementById('n8nMsg');
  var n8nStatus = document.getElementById('n8nStatus');
  var n8nCurrent = document.getElementById('n8nCurrent');
  var n8nKeyHint = document.getElementById('n8nKeyHint');
  var btnTestN8n = document.getElementById('btnTestN8n');

  function n8nShowMsg(text) {
    if (!n8nMsg) return;
    n8nMsg.textContent = text;
    n8nMsg.hidden = false;
  }

  function n8nHideMsg() {
    if (n8nMsg) n8nMsg.hidden = true;
  }

  function n8nShowStatus(text) {
    if (!n8nStatus) return;
    n8nStatus.textContent = text;
    n8nStatus.hidden = !text;
  }

  function n8nRenderCurrent(cfg) {
    if (!n8nCurrent) return;
    if (!cfg || !cfg.configured) {
      n8nCurrent.textContent = 'n8n не подключён — раздел «Автоматизации» недоступен (503)';
      return;
    }
    var source = cfg.source === 'env' ? 'из .env' : 'задано админом';
    var key = cfg.api_key_masked ? (' · ключ ' + cfg.api_key_masked) : '';
    n8nCurrent.textContent = 'Сейчас: ' + cfg.base_url + ' (' + source + ')' + key;
  }

  function n8nLoad() {
    if (!API || !API.adminGetN8n) return;
    API.adminGetN8n(session.token)
      .then(function (cfg) {
        n8nRenderCurrent(cfg);
        if (n8nForm && cfg.base_url) n8nForm.elements.base_url.value = cfg.base_url;
        if (n8nKeyHint && cfg.api_key_masked) n8nKeyHint.textContent = '(сохранён ' + cfg.api_key_masked + ')';
      })
      .catch(function () {
        n8nRenderCurrent(null);
      });
  }

  if (btnTestN8n) {
    btnTestN8n.addEventListener('click', function () {
      if (!API || !API.adminTestN8n || !n8nForm) return;
      n8nHideMsg();
      n8nShowStatus('Проверка связи с n8n…');
      btnTestN8n.disabled = true;
      API.adminTestN8n(session.token, {
        base_url: n8nForm.elements.base_url.value.trim(),
        api_key: n8nForm.elements.api_key.value.trim()
      })
        .then(function (res) {
          if (res && res.ok) {
            n8nShowStatus('✓ Связь работает (' + res.latency_ms + ' мс)');
          } else {
            n8nShowStatus('');
            n8nShowMsg((res && res.error) ? res.error : 'Проверка не удалась');
          }
        })
        .catch(function (err) {
          n8nShowStatus('');
          n8nShowMsg(err && err.message ? err.message : 'Проверка не удалась');
        })
        .then(function () { btnTestN8n.disabled = false; });
    });
  }

  if (n8nForm) {
    n8nForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (!API || !API.adminSaveN8n) return;
      n8nHideMsg();
      var saveBtn = n8nForm.querySelector('.llm-save');
      if (saveBtn) saveBtn.disabled = true;
      API.adminSaveN8n(session.token, {
        base_url: n8nForm.elements.base_url.value.trim(),
        api_key: n8nForm.elements.api_key.value.trim()
      })
        .then(function (cfg) {
          n8nRenderCurrent(cfg);
          if (n8nKeyHint && cfg.api_key_masked) n8nKeyHint.textContent = '(сохранён ' + cfg.api_key_masked + ')';
          n8nShowStatus('✓ Подключение сохранено — автоматизации работают');
        })
        .catch(function (err) {
          n8nShowStatus('');
          n8nShowMsg(err && err.message ? err.message : 'Не удалось сохранить настройки');
        })
        .then(function () { if (saveBtn) saveBtn.disabled = false; });
    });
  }

  /* ---------- напоминания по шагам пайплайна ---------- */

  var REM_STEP_ORDER = ['onboarding', 'hypotheses', 'ontology', 'rag', 'agents', 'models', 'polygon', 'social'];

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

  /* ---------- суточные лимиты токенов ---------- */

  function limShowStatus(text) {
    if (!limStatus) return;
    limStatus.textContent = text;
    limStatus.hidden = !text;
  }

  function limShowMsg(text) {
    if (!limMsg) return;
    limMsg.textContent = text;
    limMsg.hidden = false;
  }

  function limLoad() {
    if (!API || !API.adminGetLimits || !limForm) return;
    API.adminGetLimits(session.token)
      .then(function (cfg) {
        limitsCfg = cfg;
        if (limForm.elements.daily_limit) {
          limForm.elements.daily_limit.value = cfg && cfg.daily_limit ? cfg.daily_limit : '';
        }
        /* в таблице колонка «Суточный лимит» показывает общий лимит — перерисовать */
        if (lastUsers) render(lastUsers, lastTotals);
      })
      .catch(function () { limitsCfg = null; });
  }

  if (limForm) {
    limForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (!API || !API.adminSetLimits) return;
      limStatus.hidden = true;
      limMsg.hidden = true;
      var value = parseInt(limForm.elements.daily_limit.value, 10);
      if (!isFinite(value) || value < 1) {
        limShowMsg('Лимит должен быть положительным числом токенов');
        return;
      }
      var saveBtn = limForm.querySelector('[type="submit"]');
      if (saveBtn) saveBtn.disabled = true;
      API.adminSetLimits(session.token, value)
        .then(function () {
          limShowStatus('✓ Лимит сохранён — применяется ко всем пользователям сразу');
          limLoad();
        })
        .catch(function (err) {
          limShowMsg(err && err.message ? err.message : 'Не удалось сохранить лимит');
        })
        .then(function () { if (saveBtn) saveBtn.disabled = false; });
    });
  }

  /* ---------- рассылка писем ---------- */

  function mailSettingsShowMsg(text) {
    if (!mailSettingsMsg) return;
    mailSettingsMsg.textContent = text;
    mailSettingsMsg.hidden = false;
  }
  function mailSettingsHideMsg() {
    if (mailSettingsMsg) mailSettingsMsg.hidden = true;
  }
  function mailSettingsShowStatus(text) {
    if (!mailSettingsStatus) return;
    mailSettingsStatus.textContent = text;
    mailSettingsStatus.hidden = !text;
  }

  function mailSettingsLoad() {
    if (!API || !API.adminGetMailSettings || !mailSettingsForm) return;
    API.adminGetMailSettings(session.token)
      .then(function (cfg) {
        if (cfg.host) mailSettingsForm.elements.host.value = cfg.host;
        if (cfg.port) mailSettingsForm.elements.port.value = cfg.port;
        if (cfg.tls) mailSettingsForm.elements.tls.value = cfg.tls;
        if (cfg.user) mailSettingsForm.elements.user.value = cfg.user;
        if (cfg.from_email) mailSettingsForm.elements.from_email.value = cfg.from_email;
        if (cfg.from_name) mailSettingsForm.elements.from_name.value = cfg.from_name;
        if (mailPassHint) mailPassHint.textContent = cfg.password_masked ? '(сохранён ' + cfg.password_masked + ')' : '';
      })
      .catch(function () {});
  }

  if (mailSettingsForm) {
    mailSettingsForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (!API || !API.adminSetMailSettings) return;
      mailSettingsHideMsg();
      var saveBtn = mailSettingsForm.querySelector('[type="submit"]');
      if (saveBtn) saveBtn.disabled = true;
      var f = mailSettingsForm.elements;
      API.adminSetMailSettings(session.token, {
        host: f.host.value.trim(),
        port: parseInt(f.port.value, 10) || 465,
        tls: f.tls.value,
        user: f.user.value.trim(),
        password: f.password.value,
        from_email: f.from_email.value.trim(),
        from_name: f.from_name.value.trim()
      })
        .then(function (cfg) {
          if (mailPassHint) mailPassHint.textContent = cfg.password_masked ? '(сохранён ' + cfg.password_masked + ')' : '';
          f.password.value = '';
          mailSettingsShowStatus('\u2713 Настройки SMTP сохранены');
        })
        .catch(function (err) {
          mailSettingsShowStatus('');
          mailSettingsShowMsg(err && err.message ? err.message : 'Не удалось сохранить настройки SMTP');
        })
        .then(function () { if (saveBtn) saveBtn.disabled = false; });
    });
  }

  function mailUpdatePreview() {
    if (!mailPreviewFrame || !mailBodyText || !API || !API.adminMailPreview) return;
    var values = mailComposeValues();
    if (!values.body_text) {
      mailPreviewFrame.srcdoc = '<p style="font-family:sans-serif;color:#888;">Пусто</p>';
      return;
    }
    API.adminMailPreview(session.token, values.body_text, values.button_label, values.button_url)
      .then(function (res) { mailPreviewFrame.srcdoc = res.html || ''; })
      .catch(function () {});
  }

  if (btnMailPreview) btnMailPreview.addEventListener('click', mailUpdatePreview);
  [mailBodyText, mailButtonLabel, mailButtonUrl].forEach(function (el) {
    if (!el) return;
    var mailPreviewTimer = null;
    el.addEventListener('input', function () {
      if (mailPreviewTimer) clearTimeout(mailPreviewTimer);
      mailPreviewTimer = setTimeout(mailUpdatePreview, 500);
    });
  });

  var mailRecipientsCache = [];

  function mailRenderRecipients() {
    if (!mailRecipients) return;
    mailRecipients.innerHTML = mailRecipientsCache.map(function (r) {
      return '<label class="mail-recipient-row">' +
        '<input type="checkbox" class="mail-recipient-cb" value="' + esc(r.id) + '" checked>' +
        '<span class="mail-recipient-fio">' + (esc(r.fio) || '<span class="at-project-empty">без ФИО</span>') + '</span>' +
        '<span class="mail-recipient-email">' + esc(r.email) + '</span>' +
        '</label>';
    }).join('');
    mailUpdateRecipientsCount();
  }

  function mailUpdateRecipientsCount() {
    if (!mailRecipientsCount || !mailRecipients) return;
    var total = mailRecipientsCache.length;
    var checked = mailRecipients.querySelectorAll('.mail-recipient-cb:checked').length;
    mailRecipientsCount.textContent = 'Отмечено ' + checked + ' из ' + total;
  }

  function mailLoadRecipients() {
    if (!API || !API.adminMailRecipients) return;
    API.adminMailRecipients(session.token)
      .then(function (res) {
        mailRecipientsCache = res.items || [];
        mailRenderRecipients();
      })
      .catch(function () {
        if (mailRecipients) mailRecipients.innerHTML = '<p class="login-msg login-msg--error">Не удалось загрузить список получателей</p>';
      });
  }

  if (mailRecipients) {
    mailRecipients.addEventListener('change', function (e) {
      if (e.target && e.target.classList.contains('mail-recipient-cb')) mailUpdateRecipientsCount();
    });
  }
  if (btnMailSelectAll) {
    btnMailSelectAll.addEventListener('click', function () {
      if (!mailRecipients) return;
      mailRecipients.querySelectorAll('.mail-recipient-cb').forEach(function (cb) { cb.checked = true; });
      mailUpdateRecipientsCount();
    });
  }
  if (btnMailSelectNone) {
    btnMailSelectNone.addEventListener('click', function () {
      if (!mailRecipients) return;
      mailRecipients.querySelectorAll('.mail-recipient-cb').forEach(function (cb) { cb.checked = false; });
      mailUpdateRecipientsCount();
    });
  }

  function mailSendShowMsg(text) {
    if (!mailSendMsg) return;
    mailSendMsg.textContent = text;
    mailSendMsg.hidden = false;
  }
  function mailSendHideMsg() {
    if (mailSendMsg) mailSendMsg.hidden = true;
  }
  function mailSendShowStatus(text) {
    if (!mailSendStatus) return;
    mailSendStatus.textContent = text;
    mailSendStatus.hidden = !text;
  }

  function mailComposeValues() {
    var f = mailComposeForm ? mailComposeForm.elements : null;
    return {
      subject: f ? f.subject.value.trim() : '',
      body_text: mailBodyText ? mailBodyText.value : '',
      button_label: mailButtonLabel ? mailButtonLabel.value.trim() : '',
      button_url: mailButtonUrl ? mailButtonUrl.value.trim() : ''
    };
  }

  if (btnMailTest) {
    btnMailTest.addEventListener('click', function () {
      if (!API || !API.adminMailTest) return;
      mailSendHideMsg();
      var values = mailComposeValues();
      var to = mailTestTo ? mailTestTo.value.trim() : '';
      if (!to) { mailSendShowMsg('Укажите тестовый адрес'); return; }
      if (!values.subject || !values.body_text) { mailSendShowMsg('Заполните тему и текст письма'); return; }
      mailSendShowStatus('Отправка тестового письма\u2026');
      btnMailTest.disabled = true;
      API.adminMailTest(session.token, to, values.subject, values.body_text, values.button_label, values.button_url)
        .then(function () { mailSendShowStatus('\u2713 Тестовое письмо отправлено на ' + to); })
        .catch(function (err) {
          mailSendShowStatus('');
          mailSendShowMsg(err && err.message ? err.message : 'Не удалось отправить тестовое письмо');
        })
        .then(function () { btnMailTest.disabled = false; });
    });
  }

  if (btnMailSend) {
    btnMailSend.addEventListener('click', function () {
      if (!API || !API.adminMailSend || !mailRecipients) return;
      mailSendHideMsg();
      var values = mailComposeValues();
      if (!values.subject || !values.body_text) { mailSendShowMsg('Заполните тему и текст письма'); return; }
      var ids = Array.prototype.slice.call(mailRecipients.querySelectorAll('.mail-recipient-cb:checked'))
        .map(function (cb) { return cb.value; });
      if (!ids.length) { mailSendShowMsg('Отметьте хотя бы одного получателя'); return; }
      if (!window.confirm('Отправить письмо ' + ids.length + ' получателям? Действие необратимо.')) return;
      mailSendShowStatus('Отправка письма ' + ids.length + ' получателям\u2026');
      btnMailSend.disabled = true;
      API.adminMailSend(session.token, values.subject, values.body_text, values.button_label, values.button_url, ids)
        .then(function (res) {
          var failed = (res && res.failed) || [];
          if (failed.length) {
            mailSendShowStatus('');
            mailSendShowMsg('Отправлено: ' + res.sent + '. Не удалось доставить: ' +
              failed.map(function (f) { return f.email; }).join(', '));
          } else {
            mailSendShowStatus('\u2713 Письмо отправлено всем отмеченным получателям (' + res.sent + ')');
          }
        })
        .catch(function (err) {
          mailSendShowStatus('');
          mailSendShowMsg(err && err.message ? err.message : 'Не удалось отправить рассылку');
        })
        .then(function () { btnMailSend.disabled = false; });
    });
  }

  /* ---------- раздел «Предложения пользователей» ----------
     Все записи из окна «Предложить изменения» (feedback/proposals.json,
     GET /api/feedback/suggestions — только администратор). Выполненные
     предложения не удаляются: сортируются вниз списка и становятся
     бледными; кнопка «Вернуть» возвращает в работу. */
  var sugWrap = document.getElementById('sugWrap');
  var sugList = document.getElementById('sugList');
  var sugCount = document.getElementById('sugCount');
  var sugMsg = document.getElementById('sugMsg');
  var btnSugRefresh = document.getElementById('btnSugRefresh');
  var sugData = null; /* кэш последнего ответа API */

  function sugShowMsg(text) {
    if (!sugMsg) return;
    sugMsg.textContent = text;
    sugMsg.hidden = false;
  }

  function sugFmtDate(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    return fmtDate(d.getTime() / 1000);
  }

  function sugRender() {
    if (!sugList) return;
    var items = (sugData || []).slice();
    /* в работе — сверху (свежие раньше), выполненные — внизу (по done_at) */
    items.sort(function (a, b) {
      var ad = !!a.done, bd = !!b.done;
      if (ad !== bd) return ad ? 1 : -1;
      var ak = ad ? (a.done_at || a.created_at || '') : (a.created_at || '');
      var bk = bd ? (b.done_at || b.created_at || '') : (b.created_at || '');
      return ak < bk ? 1 : (ak > bk ? -1 : 0);
    });
    var openCount = items.filter(function (s) { return !s.done; }).length;
    if (sugCount) {
      sugCount.textContent = 'Всего предложений: ' + items.length +
        (items.length ? (openCount ? ' · в работе: ' + openCount : ' · все выполнены') : '');
    }
    if (!items.length) {
      sugList.innerHTML = '<p class="admin-note">Пока предложений нет — они появятся после отправки через окно «Предложить изменения».</p>';
      return;
    }
    sugList.innerHTML = items.map(function (s) {
      var done = !!s.done;
      return '<div class="sug-item' + (done ? ' sug-item--done' : '') + '">' +
        '<div class="sug-head">' +
          '<span class="sug-date">' + esc(sugFmtDate(s.created_at)) + '</span>' +
          '<span class="sug-login">' + esc(s.login || 'гость (без входа)') + '</span>' +
          (done ? '<span class="sug-badge">Выполнено</span>' : '') +
          '<button class="adm-btn sug-btn" type="button" data-id="' + esc(s.id) + '">' +
            (done ? 'Вернуть' : 'Выполнено') + '</button>' +
        '</div>' +
        '<div class="sug-msg">' + esc(s.message) + '</div>' +
      '</div>';
    }).join('');
  }

  function sugLoad() {
    if (!sugList || !API || !API.adminListSuggestions) return;
    if (sugMsg) sugMsg.hidden = true;
    API.adminListSuggestions(session.token)
      .then(function (res) {
        sugData = (res && res.suggestions) || [];
        sugRender();
      })
      .catch(function (err) {
        sugShowMsg(err && err.message ? err.message : 'Не удалось загрузить предложения');
      });
  }

  if (btnSugRefresh) btnSugRefresh.addEventListener('click', sugLoad);

  if (sugList) {
    sugList.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.sug-btn') : null;
      if (!btn || !API || !API.adminSetSuggestionDone) return;
      var id = btn.getAttribute('data-id');
      var rec = null;
      (sugData || []).forEach(function (s) { if (s.id === id) rec = s; });
      if (!rec) return;
      var makeDone = !rec.done;
      btn.disabled = true;
      API.adminSetSuggestionDone(session.token, id, makeDone)
        .then(function (updated) {
          sugData = sugData.map(function (s) {
            return s.id === id ? (updated && updated.id ? updated : Object.assign({}, s, { done: makeDone })) : s;
          });
          sugRender();
        })
        .catch(function (err) {
          sugShowMsg(err && err.message ? err.message : 'Не удалось обновить предложение');
          btn.disabled = false;
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
  /* раздел «Автоматизации» скрыт с фронта до редизайна конструктора
     (js/api.js → AIOS_FEATURES.automations, см. BACKLOG.md) */
  if (n8nWrap) n8nWrap.hidden = !(window.AIOS_FEATURES && window.AIOS_FEATURES.automations);
  if (remWrap) remWrap.hidden = false;
  if (limWrap) limWrap.hidden = false;
  if (mailWrap) mailWrap.hidden = false;
  if (sugWrap) sugWrap.hidden = false;
  llmLoad();
  if (window.AIOS_FEATURES && window.AIOS_FEATURES.automations) n8nLoad();
  remLoad();
  limLoad();
  mailSettingsLoad();
  mailLoadRecipients();
  sugLoad();
  load();

  /* сворачиваемые разделы: кнопка-заголовок прячет/показывает тело
     («Настройки внешних API», «Напоминания», «Лимиты токенов»,
     «Об организации», «Основные документы», «Предложения пользователей») */
  [['llmToggle', 'llmBody'], ['n8nToggle', 'n8nBody'], ['remToggle', 'remBody'], ['limToggle', 'limBody'], ['mailToggle', 'mailBody'], ['orgToggle', 'orgBody'], ['orgDocsToggle', 'orgDocsGroups'], ['sugToggle', 'sugBody']]
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
