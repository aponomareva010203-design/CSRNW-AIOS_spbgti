/* ============================================================
   Настройки: данные пользователя, личный внешний API (LLM),
   Telegram-бот, смена пароля.

   «Данные пользователя» (user/{id}-profile.json) переехали сюда
   из «Кабинета»: ФИО, должности (динамические строки, до 10),
   учёная степень, профессиональные интересы. Идентификатор — только из
   Bearer-токена (GET|PUT /api/cabinet/profile).

   «Внешний API (LLM)» (user/{id}-llm.json, GET|PUT|DELETE
   /api/settings/llm): если задан Endpoint, платформа ходит к модели
   по личным endpoint/ключу пользователя (чат, иконки, контекст);
   пусто — общие настройки платформы. Ключ с сервера не возвращается
   (только маска), пустые поля ключа при сохранении означают «не
   менять». При личных настройках суточный лимит токенов не действует —
   после сохранения шлём 'aios-usage-refresh', чтобы счётчик в меню
   переключился на «потрачено за сутки».

   «Telegram-бот» — привязка учётной записи (GET|POST|DELETE
   /api/auth/telegram/link; переехало из «Кабинета»). Код выдаёт
   /start в боте; deep-link settings.html#tg-link=КОД из кнопки бота
   подставляет код сам. Пароль боту не вводится.

   «Сменить пароль» — модалка passModal из auth.js (data-open-pass).

   Онбординг: после регистрации профиль не заполнен — auth.js ведёт
   сюда с ?onboarding=1; после сохранения открывается рабочий чат с
   навыком «Онбординг проекта» (флаг в localStorage, как раньше).
   ============================================================ */
(function () {
  var API = window.AIOS_API;
  var AUTH_KEY = 'aios-auth-v1';
  var ONBOARDING_KEY = 'aios-onboarding-v1';
  var SKILL_KEY = 'aios-workspace-skill-v1';
  var MAX_POSITIONS = 10;

  /* Режим онбординга: см. комментарий в шапке. */
  var onboarding = false;
  (function detectOnboarding() {
    try {
      if (/[?&]onboarding=1\b/.test(location.search)) {
        sessionStorage.setItem(ONBOARDING_KEY, '1');
      }
      onboarding = sessionStorage.getItem(ONBOARDING_KEY) === '1';
      if (/[?&]onboarding=/.test(location.search)) {
        history.replaceState(null, '', location.pathname);
      }
    } catch (e) { onboarding = false; }
  })();

  var guest = document.getElementById('setGuest');
  var guestMsg = document.getElementById('setGuestMsg');
  var loginBtn = document.getElementById('setLoginBtn');
  var profileCard = document.getElementById('setProfileCard');
  var llmCard = document.getElementById('setLLMCard');
  var tgCard = document.getElementById('setTgCard');
  var securityCard = document.getElementById('setSecurityCard');

  var form = document.getElementById('setProfileForm');
  var posWrap = document.getElementById('setPositions');
  var posAdd = document.getElementById('setPosAdd');
  var msg = document.getElementById('setProfileMsg');
  var status = document.getElementById('setProfileStatus');
  var updated = document.getElementById('setUpdated');
  var saveBtn = document.getElementById('setProfileSave');
  var consent = document.getElementById('setConsent');

  var llmForm = document.getElementById('setLLMForm');
  var llmMsg = document.getElementById('setLLMMsg');
  var llmSaveBtn = document.getElementById('setLLMSave');
  var llmClearBtn = document.getElementById('setLLMClear');
  var llmTestBtn = document.getElementById('setLLMTest');
  var llmModeEl = document.getElementById('setLLMMode');
  var llmKeyHint = document.getElementById('setLLMKeyHint');

  if (!guest || !form || !llmForm) return;

  function getSession() {
    try {
      var raw = localStorage.getItem(AUTH_KEY);
      var s = raw ? JSON.parse(raw) : null;
      return s && s.token ? s : null;
    } catch (e) {
      return null;
    }
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmtDate(ts) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '';
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear() +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function showMsg(text, kind) {
    if (!msg) return;
    msg.textContent = text || '';
    msg.className = 'login-msg login-msg--' + (kind || 'error');
    msg.hidden = !text;
  }

  function showStatus(text) {
    if (!status) return;
    status.textContent = text || '';
  }

  function showLLMMsg(text, kind) {
    if (!llmMsg) return;
    llmMsg.textContent = text || '';
    llmMsg.className = 'login-msg login-msg--' + (kind || 'error');
    llmMsg.hidden = !text;
  }

  function onSessionLost(text) {
    try { localStorage.removeItem(AUTH_KEY); } catch (e) { /* приватный режим */ }
    renderState(false, text);
  }

  /* ---------- должности: динамические строки ---------- */

  function posRow(value) {
    return '<div class="org-alias-row">' +
      '<input type="text" maxlength="120" value="' + esc(value) + '" placeholder="должность — например, доцент кафедры искусственного интеллекта">' +
      '<button class="org-alias-del" type="button" title="Убрать должность">×</button>' +
      '</div>';
  }

  function renderPositions(list) {
    if (!posWrap) return;
    var items = (list || []).filter(function (p) { return String(p || '').trim(); });
    posWrap.innerHTML = (items.length ? items : ['']).map(posRow).join('');
    syncPosAdd();
  }

  function syncPosAdd() {
    if (!posAdd || !posWrap) return;
    posAdd.hidden = posWrap.querySelectorAll('.org-alias-row').length >= MAX_POSITIONS;
  }

  if (posWrap) {
    posWrap.addEventListener('click', function (e) {
      var del = e.target && e.target.closest ? e.target.closest('.org-alias-del') : null;
      if (!del) return;
      var row = del.closest('.org-alias-row');
      if (posWrap.querySelectorAll('.org-alias-row').length > 1) {
        row.remove();
      } else {
        var input = row.querySelector('input');
        if (input) input.value = '';
      }
      syncPosAdd();
    });
  }

  if (posAdd) {
    posAdd.addEventListener('click', function () {
      if (!posWrap) return;
      if (posWrap.querySelectorAll('.org-alias-row').length >= MAX_POSITIONS) return;
      posWrap.insertAdjacentHTML('beforeend', posRow(''));
      var inputs = posWrap.querySelectorAll('input');
      if (inputs.length) inputs[inputs.length - 1].focus();
      syncPosAdd();
    });
  }

  /* ---------- данные пользователя: загрузка и сохранение ---------- */

  function fillProfile(p) {
    p = p || {};
    form.elements.last_name.value = p.last_name || '';
    form.elements.first_name.value = p.first_name || '';
    form.elements.patronymic.value = p.patronymic || '';
    form.elements.degree.value = p.degree || '';
    form.elements.interests.value = p.interests || '';
    renderPositions(p.positions);
    if (updated) updated.textContent = p.updated_at ? 'обновлено: ' + fmtDate(p.updated_at) : '';
  }

  function renderState(authed, guestText) {
    guest.hidden = !!authed;
    if (profileCard) profileCard.hidden = !authed;
    if (llmCard) llmCard.hidden = !authed;
    if (tgCard) tgCard.hidden = !authed;
    if (securityCard) securityCard.hidden = !authed;
    if (guestMsg) {
      guestMsg.textContent = guestText || 'Настройки доступны только после входа в AIOS.';
    }
  }

  function loadProfile() {
    var session = getSession();
    if (!session || !API || !API.cabinetGetProfile) return;
    API.cabinetGetProfile(session.token)
      .then(function (profile) {
        fillProfile(profile);
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          onSessionLost('Сессия истекла — войдите заново.');
        } else {
          showMsg('Не удалось загрузить данные: ' + (err && err.message ? err.message : 'ошибка сети'));
        }
      });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var session = getSession();
    if (!session) {
      renderState(false);
      return;
    }
    var positions = [];
    if (posWrap) {
      Array.prototype.forEach.call(posWrap.querySelectorAll('input'), function (input) {
        var value = String(input.value || '').trim();
        if (value) positions.push(value);
      });
    }
    if (positions.length > MAX_POSITIONS) {
      showMsg('Должностей не может быть больше ' + MAX_POSITIONS);
      return;
    }
    if (consent && !consent.checked) {
      showMsg('Подтвердите согласие на обработку персональных данных');
      return;
    }
    if (saveBtn) saveBtn.disabled = true;
    showStatus('Сохранение…');
    showMsg('');
    API.cabinetSaveProfile(session.token, {
      last_name: String(form.elements.last_name.value || '').trim(),
      first_name: String(form.elements.first_name.value || '').trim(),
      patronymic: String(form.elements.patronymic.value || '').trim(),
      positions: positions,
      degree: String(form.elements.degree.value || '').trim(),
      interests: String(form.elements.interests.value || '')
    })
      .then(function (data) {
        showStatus('');
        if (updated && data && data.updated_at) {
          updated.textContent = 'обновлено: ' + fmtDate(data.updated_at);
        }
        /* Онбординг завершён: помечаем профиль заполненным в локальной
           сессии, запускаем навык «Онбординг проекта» в рабочем чате
           и возвращаемся на главную (workspace.js подхватит флаг). */
        if (onboarding) {
          try {
            sessionStorage.removeItem(ONBOARDING_KEY);
            var s = getSession();
            if (s) {
              s.profile_filled = true;
              localStorage.setItem(AUTH_KEY, JSON.stringify(s));
            }
            localStorage.setItem(SKILL_KEY, JSON.stringify({
              skill_id: 'project-onboarding',
              name: 'Онбординг проекта',
              prompt: 'Начните онбординг: познакомьтесь со мной и помогите ' +
                'сформулировать тему и карточку моего первого проекта.',
              open: true
            }));
          } catch (e) { /* приватный режим — просто остаёмся в настройках */ }
          location.href = 'index.html';
          return;
        }
        showMsg('Данные сохранены', 'note');
      })
      .catch(function (err) {
        showStatus('');
        if (err && err.status === 401) {
          onSessionLost('Сессия истекла — войдите заново.');
        } else {
          showMsg(err && err.message ? err.message : 'Не удалось сохранить данные');
        }
      })
      .then(function () {
        if (saveBtn) saveBtn.disabled = false;
      });
  });

  /* ---------- внешний API (LLM): загрузка и сохранение ---------- */

  function renderLLM(data) {
    data = data || {};
    llmForm.elements.provider.value = data.provider || '';
    llmForm.elements.base_url.value = data.base_url || '';
    llmForm.elements.model.value = data.model || '';
    llmForm.elements.api_key.value = '';
    llmForm.elements.api_key2.value = '';
    var masked = data.api_key_masked || '';
    llmForm.elements.api_key.placeholder = masked
      ? 'сохранён ключ: ' + masked
      : 'введите ключ (или пусто — endpoint без авторизации)';
    llmForm.elements.api_key2.placeholder = 'повторите ключ';
    if (llmModeEl) {
      llmModeEl.textContent = data.active
        ? 'личные настройки'
        : 'общие настройки';
    }
    if (llmKeyHint) {
      llmKeyHint.textContent = masked
        ? 'Сохранён ключ ' + masked + ' — оставьте поля ключа пустыми, чтобы не менять его.'
        : 'Ключ ещё не сохранён; для endpoint без авторизации оставьте поля ключа пустыми.';
    }
  }

  function loadLLM() {
    var session = getSession();
    if (!session || !API || !API.settingsGetLLM) return;
    API.settingsGetLLM(session.token)
      .then(renderLLM)
      .catch(function (err) {
        if (err && err.status === 401) {
          onSessionLost('Сессия истекла — войдите заново.');
        }
        /* без настроек форма просто остаётся пустой */
      });
  }

  llmForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var session = getSession();
    if (!session || !API.settingsSaveLLM) return;
    var baseUrl = String(llmForm.elements.base_url.value || '').trim();
    var apiKey = String(llmForm.elements.api_key.value || '');
    var apiKey2 = String(llmForm.elements.api_key2.value || '');
    if (!baseUrl) {
      showLLMMsg('Укажите Endpoint (Base URL) или нажмите «Использовать общие»');
      return;
    }
    if ((apiKey || apiKey2) && apiKey !== apiKey2) {
      showLLMMsg('Ключи в полях не совпадают');
      return;
    }
    if (llmSaveBtn) llmSaveBtn.disabled = true;
    showLLMMsg('');
    API.settingsSaveLLM(session.token, {
      provider: String(llmForm.elements.provider.value || '').trim(),
      base_url: baseUrl,
      api_key: apiKey,
      api_key2: apiKey2,
      model: String(llmForm.elements.model.value || '').trim()
    })
      .then(function (data) {
        renderLLM(data);
        /* счётчик в меню: лимит пропал/появился — перечитать usage */
        try { document.dispatchEvent(new CustomEvent('aios-usage-refresh')); } catch (e) {}
        showLLMMsg(data.active
          ? 'Сохранено — платформа работает через ваши endpoint и ключ.'
          : 'Сохранено: личные настройки убраны, работают общие.', 'note');
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          onSessionLost('Сессия истекла — войдите заново.');
          return;
        }
        showLLMMsg(err && err.message ? err.message : 'Не удалось сохранить настройки');
      })
      .then(function () {
        if (llmSaveBtn) llmSaveBtn.disabled = false;
      });
  });

  if (llmClearBtn) {
    llmClearBtn.addEventListener('click', function () {
      var session = getSession();
      if (!session || !API.settingsClearLLM) return;
      llmClearBtn.disabled = true;
      API.settingsClearLLM(session.token)
        .then(function (data) {
          renderLLM(data);
          try { document.dispatchEvent(new CustomEvent('aios-usage-refresh')); } catch (e) {}
          showLLMMsg('Личные настройки убраны — платформа работает по общим.', 'note');
        })
        .catch(function (err) {
          if (err && err.status === 401) {
            onSessionLost('Сессия истекла — войдите заново.');
            return;
          }
          showLLMMsg(err && err.message ? err.message : 'Не удалось очистить настройки');
        })
        .then(function () { llmClearBtn.disabled = false; });
    });
  }

  /* «Проверить соединение»: минимальный запрос к личному endpoint с общей
     моделью платформы (как в админке). Черновик берётся из формы — можно
     проверить до сохранения; пустые поля — сохранённая конфигурация. */
  if (llmTestBtn) {
    llmTestBtn.addEventListener('click', function () {
      var session = getSession();
      if (!session || !API.settingsTestLLM) return;
      llmTestBtn.disabled = true;
      showLLMMsg('Проверка соединения…');
      API.settingsTestLLM(session.token, {
        base_url: String(llmForm.elements.base_url.value || '').trim(),
        api_key: String(llmForm.elements.api_key.value || '')
      })
        .then(function (res) {
          if (res && res.ok) {
            showLLMMsg(
              'API работает (' + (res.latency_ms || 0) + ' мс' +
              (res.model ? ', модель «' + res.model + '»' : '') + ') — ' +
              'вы используете личные настройки внешнего API к LLM. ' +
              'Чтобы вернуть общие, нажмите «Использовать общие».',
              'note'
            );
          } else {
            showLLMMsg(
              'Ошибка: ' + ((res && res.error) || 'эндпоинт недоступен') + '. ' +
              'Проверьте корректность заполнения настроек — сейчас платформа ' +
              'отвечает через общие настройки.'
            );
          }
        })
        .catch(function (err) {
          if (err && err.status === 401) {
            onSessionLost('Сессия истекла — войдите заново.');
            return;
          }
          showLLMMsg(
            'Ошибка: ' + (err && err.message ? err.message : 'не удалось выполнить проверку') + '. ' +
            'Проверьте корректность заполнения настроек — сейчас платформа отвечает через общие настройки.'
          );
        })
        .then(function () { llmTestBtn.disabled = false; });
    });
  }

  /* ---------- Telegram-бот: привязка учётной записи ----------

     Код выдаёт /start в боте (api/routers/telegram.py); привязка —
     POST /api/auth/telegram/link. Deep-link settings.html#tg-link=КОД
     из кнопки бота подставляет код сам. Переехало из «Кабинета». */

  var tgLinkForm = document.getElementById('setTgLinkForm');
  var tgCodeInput = document.getElementById('setTgCode');
  var tgLinkBtn = document.getElementById('setTgLinkBtn');
  var tgUnlinkBtn = document.getElementById('setTgUnlinkBtn');
  var tgLinkedInfo = document.getElementById('setTgLinkedInfo');
  var tgMsg = document.getElementById('setTgMsg');
  var tgStateEl = document.getElementById('setTgState');
  var tgBotLink = document.getElementById('setTgBotLink');

  function tgShowMsg(text) {
    if (!tgMsg) return;
    tgMsg.textContent = text || '';
    tgMsg.hidden = !text;
  }

  function tgDeepLinkCode() {
    var m = /(?:^|[#&])tg-link=([A-Za-z0-9]+)/.exec(location.hash || '');
    return m ? m[1].toUpperCase() : '';
  }

  function tgClearDeepLink() {
    if (/tg-link=/.test(location.hash || '')) {
      history.replaceState(null, '', location.pathname + location.search);
    }
  }

  function renderTgStatus(data) {
    var linked = !!(data && data.linked);
    if (tgStateEl) tgStateEl.textContent = linked ? 'привязан' : 'не привязан';
    if (tgLinkForm) tgLinkForm.hidden = linked;
    if (tgUnlinkBtn) tgUnlinkBtn.hidden = !linked;
    if (tgLinkedInfo) {
      if (linked) {
        tgLinkedInfo.innerHTML = 'Привязан Telegram: <b>@' + esc((data && data.username) || '') +
          '</b>' + (data && data.linked_at ? ' · ' + esc(fmtDate(data.linked_at)) : '') +
          '. Откройте чат с ботом — он работает от вашей учётной записи.';
      } else {
        tgLinkedInfo.innerHTML = '';
      }
      tgLinkedInfo.hidden = !linked;
    }
    if (tgBotLink) {
      var username = (data && data.bot_username) || '';
      tgBotLink.hidden = !username;
      tgBotLink.href = username ? 'https://t.me/' + encodeURIComponent(username) : '#';
      tgBotLink.textContent = username ? '@' + username : '';
    }
  }

  function loadTelegram() {
    var session = getSession();
    if (!session || !API || !API.telegramLinkStatus) return;
    API.telegramLinkStatus(session.token)
      .then(function (data) {
        renderTgStatus(data);
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          onSessionLost('Сессия истекла — войдите заново.');
          return;
        }
        /* бот не настроен или сеть недоступна — карточка остаётся с подсказкой */
        tgShowMsg('Не удалось загрузить статус привязки: ' +
          (err && err.message ? err.message : 'ошибка сети'));
      });
  }

  function linkTelegram(code) {
    var session = getSession();
    if (!session || !API || !API.telegramLink) return;
    if (!code) {
      tgShowMsg('Введите код из /start бота.');
      return;
    }
    if (tgLinkBtn) tgLinkBtn.disabled = true;
    API.telegramLink(code, session.token)
      .then(function (data) {
        renderTgStatus(data);
        tgShowMsg('Telegram привязан. Откройте чат с ботом — можно писать.');
        tgClearDeepLink();
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          onSessionLost('Сессия истекла — войдите заново.');
          return;
        }
        tgShowMsg('Не удалось привязать: ' +
          ((err && (err.detail || err.message)) || 'ошибка сети'));
      })
      .then(function () { if (tgLinkBtn) tgLinkBtn.disabled = false; });
  }

  if (tgLinkBtn) {
    tgLinkBtn.addEventListener('click', function () {
      tgShowMsg('');
      linkTelegram((tgCodeInput && tgCodeInput.value || '').trim().toUpperCase());
    });
  }
  if (tgCodeInput) {
    tgCodeInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        tgShowMsg('');
        linkTelegram((tgCodeInput.value || '').trim().toUpperCase());
      }
    });
  }
  if (tgUnlinkBtn) {
    tgUnlinkBtn.addEventListener('click', function () {
      var session = getSession();
      if (!session || !API || !API.telegramUnlink) return;
      if (!window.confirm('Отвязать Telegram от учётной записи? Бот перестанет отвечать от вашего имени до повторной привязки.')) return;
      API.telegramUnlink(session.token)
        .then(function () {
          renderTgStatus({ linked: false });
          tgShowMsg('Telegram отвязан. Привязать заново — командой /start в боте.');
        })
        .catch(function (err) {
          if (err && err.status === 401) {
            onSessionLost('Сессия истекла — войдите заново.');
            return;
          }
          tgShowMsg('Не удалось отвязать: ' +
            ((err && (err.detail || err.message)) || 'ошибка сети'));
        });
    });
  }

  /* ---------- загрузка страницы ---------- */

  function load() {
    var session = getSession();
    if (!session) {
      renderState(false);
      return;
    }
    renderState(true);
    if (onboarding) {
      showMsg('Заполните «Данные пользователя» — это последний шаг регистрации. ' +
        'После сохранения откроется рабочий чат с навыком «Онбординг проекта».', 'note');
    } else {
      showMsg('');
    }
    showStatus('');
    loadProfile();
    loadLLM();
    loadTelegram();
    /* deep-link из бота (settings.html#tg-link=КОД): код подставляется и
       привязка подтверждается сразу; гостю хеш остаётся до входа */
    var deepCode = tgDeepLinkCode();
    if (deepCode) {
      if (tgCodeInput) tgCodeInput.value = deepCode;
      linkTelegram(deepCode);
    }
  }

  if (loginBtn) {
    loginBtn.addEventListener('click', function () {
      var btnLogin = document.getElementById('btnLogin');
      if (btnLogin) btnLogin.click(); /* модалку входа показывает auth.js */
    });
  }

  /* ---------- сворачиваемые разделы (как в «Кабинете») ---------- */

  var COLLAPSE_KEY = 'aios-settings-collapse-v1';

  function readCollapse() {
    try {
      var raw = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '{}');
      return raw && typeof raw === 'object' ? raw : {};
    } catch (e) {
      return {};
    }
  }

  function writeCollapse(key, collapsed) {
    var state = readCollapse();
    state[key] = !!collapsed;
    try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(state)); } catch (e) { /* приватный режим */ }
  }

  function bindSection(headId, bodyId, key) {
    var head = document.getElementById(headId);
    var body = document.getElementById(bodyId);
    if (!head || !body) return;
    var open = !readCollapse()[key];
    function apply() {
      body.hidden = !open;
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
      head.classList.toggle('cab-sec-head--closed', !open);
    }
    head.addEventListener('click', function () {
      open = !open;
      writeCollapse(key, !open);
      apply();
    });
    apply();
  }

  bindSection('setProfileHead', 'setProfileBody', 'profile');
  bindSection('setLLMHead', 'setLLMBody', 'llm');
  bindSection('setTgHead', 'setTgBody', 'telegram');
  bindSection('setSecurityHead', 'setSecurityBody', 'security');

  window.addEventListener('aios-auth-change', load);
  load();
})();
