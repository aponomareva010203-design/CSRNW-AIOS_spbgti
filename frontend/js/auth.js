/* ============================================================
   Авторизация AIOS Harness: вход, регистрация, подтверждение
   e-mail, смена пароля, выход. Кнопка «Войти» и меню пользователя
   живут в левом меню на всех страницах; модальные окна
   (loginModal/passModal) инъецируются скриптом — в HTML страниц
   их больше нет. Сессия хранится в localStorage (ключ aios-auth-v1
   — его же читают pilot.js и workspace.js): {token, login, name,
   role}. Запросы идут через window.AIOS_API (/api/auth/*).
   Роль «Администратор» открывает пункт меню «Пилот: режим
   администратора» и ссылку «Админ-панель» в левом меню.
   ============================================================ */
(function () {
  var btn = document.getElementById('btnLogin');
  if (!btn) return; /* кнопка входа есть в левом меню на всех страницах */

  /* ---------- модалки входа и смены пароля: единая разметка ----------
     В HTML страниц модалок больше нет: если их нет в DOM, auth.js
     инъецирует их в конец body (тот же приём, что PANEL_HTML в
     workspace.js). Классы и id совпадают с прежней разметкой. */
  var MODALS_HTML = [
    '<div class="modal-overlay" id="loginModal" hidden>',
    '  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="loginTitle">',
    '    <button class="modal-close" id="modalClose" type="button" aria-label="Закрыть">✕</button>',
    '    <h2 id="loginTitle">Вход в AIOS</h2>',
    '    <form class="login-form" id="loginForm">',
    '      <label>',
    '        <span>E-mail</span>',
    '        <input type="email" name="login" autocomplete="username" required>',
    '      </label>',
    '      <label>',
    '        <span>Пароль</span>',
    '        <input type="password" name="password" autocomplete="current-password" required>',
    '      </label>',
    '      <label style="position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden" aria-hidden="true">',
    '        <span>Сайт (не заполняйте)</span>',
    '        <input type="text" name="website" tabindex="-1" autocomplete="off">',
    '      </label>',
    '      <label id="password2Field" hidden>',
    '        <span>Повторите пароль</span>',
    '        <input type="password" name="password2" autocomplete="new-password" minlength="4">',
    '      </label>',
    '      <div id="consentFields" hidden>',
    '        <label class="login-consent">',
    '          <input type="checkbox" name="consent_pdn">',
    '          <span>Я даю согласие на обработку моих персональных данных (e-mail, пароль в виде хеша, необязательное имя) в целях регистрации и предоставления доступа к платформе AIOS — <a href="privacy-policy.html" target="_blank" rel="noopener">подробнее в Политике</a></span>',
    '        </label>',
    '        <label class="login-consent">',
    '          <input type="checkbox" name="consent_policy">',
    '          <span>Я ознакомлен(а) и согласен(на) с <a href="privacy-policy.html" target="_blank" rel="noopener">Политикой обработки персональных данных и использования файлов cookie</a></span>',
    '        </label>',
    '      </div>',
    '      <p class="login-msg" id="loginMsg" hidden></p>',
    '      <a class="resend-link" id="resendLink" href="#" hidden>Отправить письмо подтверждения ещё раз</a>',
    '      <button class="btn-primary" id="loginSubmit" type="submit">Войти</button>',
    '    </form>',
    '    <div class="modal-alt">',
    '      <span id="altQuestion">Нет аккаунта?</span>',
    '      <a class="btn-register" href="#">Зарегистрироваться</a>',
    '      <a class="btn-register btn-login-alt" href="#" hidden>Войти</a>',
    '    </div>',
    '  </div>',
    '</div>',
    '<div class="modal-overlay" id="passModal" hidden>',
    '  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="passTitle">',
    '    <button class="modal-close" id="passClose" type="button" aria-label="Закрыть">✕</button>',
    '    <h2 id="passTitle">Смена пароля</h2>',
    '    <form class="login-form" id="passForm">',
    '      <label>',
    '        <span>Текущий пароль</span>',
    '        <input type="password" name="old_password" autocomplete="current-password" required>',
    '      </label>',
    '      <label>',
    '        <span>Новый пароль (минимум 4 символа)</span>',
    '        <input type="password" name="new_password" autocomplete="new-password" minlength="4" required>',
    '      </label>',
    '      <label>',
    '        <span>Новый пароль ещё раз</span>',
    '        <input type="password" name="new_password2" autocomplete="new-password" minlength="4" required>',
    '      </label>',
    '      <p class="login-msg" id="passMsg" hidden></p>',
    '      <button class="btn-primary" id="passSubmit" type="submit">Сохранить пароль</button>',
    '    </form>',
    '    <div class="modal-alt">',
    '      <span>После смены пароля другие сессии будут завершены.</span>',
    '    </div>',
    '  </div>',
    '</div>',
    '<div class="modal-overlay" id="suggestModal" hidden>',
    '  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="suggestTitle">',
    '    <button class="modal-close" id="suggestClose" type="button" aria-label="Закрыть">✕</button>',
    '    <h2 id="suggestTitle">Предложить изменения</h2>',
    '    <form class="login-form" id="suggestForm">',
    '      <label>',
    '        <span>Что вы предлагаете улучшить на платформе?</span>',
    '        <textarea name="message" rows="6" required placeholder="Здесь можно описать найденные ошибки, функции, которые хотелось бы иметь на платформе, или другую обратную связь. Чем подробнее, тем быстрее мы всё рассмотрим."></textarea>',
    '      </label>',
    '      <p class="login-msg" id="suggestMsg" hidden></p>',
    '      <button class="btn-primary" id="suggestSubmit" type="submit">Отправить предложение</button>',
    '    </form>',
    '    <div class="suggest-status" id="suggestStatus" hidden>',
    '      <div class="suggest-status-title">Статусы предложений</div>',
    '      <div class="suggest-status-list" id="suggestStatusList"></div>',
    '      <p class="login-msg" id="suggestStatusMsg" hidden></p>',
    '    </div>',
    '  </div>',
    '</div>'
  ].join('\n');

  if (!document.getElementById('loginModal')) {
    document.body.insertAdjacentHTML('beforeend', MODALS_HTML);
  }

  var modal = document.getElementById('loginModal');
  if (!modal) return;

  var closeBtn = document.getElementById('modalClose');
  var form = document.getElementById('loginForm');
  var msg = document.getElementById('loginMsg');
  var title = document.getElementById('loginTitle');
  var password2Field = document.getElementById('password2Field');
  var submitBtn = document.getElementById('loginSubmit');
  var registerLink = document.querySelector('.btn-register');
  var loginLink = document.querySelector('.btn-login-alt');
  var altQuestion = document.getElementById('altQuestion');
  var resendLink = document.getElementById('resendLink');
  var btnSettings = document.getElementById('btnSettings');
  var userMenu = document.getElementById('userMenu');
  var logoutBtn = document.getElementById('btnLogout');
  var menuAdmin = document.getElementById('menuAdmin');
  var menuPilot = document.getElementById('menuPilot');
  /* раздел «Передовая инженерная школа» в меню: администраторам — ссылка,
     остальным — пункт «в разработке» */
  var menuPish = document.getElementById('menuPish');
  var menuPishLock = document.getElementById('menuPishLock');
  /* раздел «Модели» (пункт меню и плитка на главной) — только администраторам */
  var menuModels = document.getElementById('menuModels');
  var tileModels = document.getElementById('tileModels');
  var changePassBtn = document.getElementById('btnChangePass');
  var passModal = document.getElementById('passModal');
  var passClose = document.getElementById('passClose');
  var passForm = document.getElementById('passForm');
  var passMsg = document.getElementById('passMsg');
  var passSubmit = document.getElementById('passSubmit');

  /* ---------- «Предложить изменения»: модалка и отправка ---------- */
  var suggestModal = document.getElementById('suggestModal');
  var suggestClose = document.getElementById('suggestClose');
  var suggestForm = document.getElementById('suggestForm');
  var suggestMsg = document.getElementById('suggestMsg');
  var suggestSubmit = document.getElementById('suggestSubmit');
  var btnSuggest = document.getElementById('btnSuggest');
  /* статусы предложений этого аккаунта («в работе» / «выполнено») */
  var suggestStatus = document.getElementById('suggestStatus');
  var suggestStatusList = document.getElementById('suggestStatusList');
  var suggestStatusMsg = document.getElementById('suggestStatusMsg');

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function showSuggestMsg(text, cls) {
    if (!suggestMsg) return;
    suggestMsg.textContent = text;
    suggestMsg.className = 'login-msg' + (cls ? ' ' + cls : '');
    suggestMsg.hidden = false;
  }

  /* ---------- статусы предложений этого аккаунта ---------- */

  function fmtSuggestDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear();
  }

  function suggestStatusRow(r) {
    var text = String(r.message || '');
    var short = text.length > 140 ? text.slice(0, 140) + '…' : text;
    var date = fmtSuggestDate(r.created_at);
    /* done ставится администратором в разделе «Предложения пользователей»:
       до отметки предложение считается «в работе» */
    var pill = r.done
      ? '<span class="suggest-status-pill suggest-status-pill--done"' +
        (r.done_at ? ' title="Отмечено выполнено ' + esc(fmtSuggestDate(r.done_at)) + '"' : '') +
        '>✓ выполнено</span>'
      : '<span class="suggest-status-pill" title="Отправлено и ждёт рассмотрения администратором">в работе</span>';
    return '<div class="suggest-status-row">' +
      '<span class="suggest-status-text" title="' + esc(text) + '">' + esc(short) + '</span>' +
      (date ? '<span class="suggest-status-date">' + esc(date) + '</span>' : '') +
      pill +
      '</div>';
  }

  function loadSuggestStatuses() {
    if (!suggestStatus) return;
    var session = getSession();
    var API = window.AIOS_API;
    if (!session || !API || !API.suggestMine) {
      suggestStatus.hidden = true;
      return;
    }
    API.suggestMine(session.token)
      .then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) {
          suggestStatus.hidden = true;
          return;
        }
        if (suggestStatusList) {
          suggestStatusList.innerHTML = items.map(suggestStatusRow).join('');
        }
        if (suggestStatusMsg) suggestStatusMsg.hidden = true;
        suggestStatus.hidden = false;
      })
      .catch(function (err) {
        if (err && err.status === 401) return; /* сессия истекла — модалку входа покажет verifySession */
        suggestStatus.hidden = true;
      });
  }

  function openSuggestModal() {
    if (!suggestModal) return;
    if (suggestMsg) suggestMsg.hidden = true;
    if (suggestForm) suggestForm.reset();
    suggestModal.hidden = false;
    var area = suggestForm && suggestForm.elements.message;
    if (area) area.focus();
    loadSuggestStatuses();
  }

  function closeSuggestModal() { if (suggestModal) suggestModal.hidden = true; }

  if (suggestForm) {
    suggestForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (suggestMsg) suggestMsg.hidden = true;
      var API = window.AIOS_API;
      var message = suggestForm.elements.message.value.trim();
      if (!message) return;
      if (!API || !API.sendSuggestion) {
        showSuggestMsg('API недоступен: запустите бэкенд (uvicorn api.main:app)', 'error');
        return;
      }
      var session = getSession();
      if (suggestSubmit) suggestSubmit.disabled = true;
      API.sendSuggestion({ message: message, login: session ? session.login : null }, session ? session.token : null)
        .then(function () {
          if (suggestSubmit) suggestSubmit.disabled = false;
          closeSuggestModal();
          loadSuggestStatuses(); /* новое предложение появилось со статусом «в работе» */
        })
        .catch(function (err) {
          if (suggestSubmit) suggestSubmit.disabled = false;
          showSuggestMsg('Не удалось отправить предложение: ' + (err && err.message ? err.message : 'ошибка сети'), 'error');
        });
    });
  }
  if (btnSuggest) btnSuggest.addEventListener('click', openSuggestModal);
  if (suggestClose) suggestClose.addEventListener('click', closeSuggestModal);
  if (suggestModal) {
    suggestModal.addEventListener('click', function (e) {
      if (e.target === suggestModal) closeSuggestModal();
    });
  }

  var STORAGE_KEY = 'aios-auth-v1';

  function getSession() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      var session = raw ? JSON.parse(raw) : null;
      /* записи старой mock-авторизации (без токена) считаем устаревшими */
      return session && session.token ? session : null;
    } catch (e) {
      return null;
    }
  }

  function saveSession(token, user, profileFilled) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        token: token, login: user.login, name: user.name, role: user.role,
        profile_filled: !!profileFilled
      }));
    } catch (e) { /* приватный режим — живём без сохранения */ }
  }

  /* Онбординг: профиль не заполнен — ведём во вкладку «Настройки»,
     где заполняется «Данные пользователя» (settings.html после
     сохранения сам откроет рабочий чат с навыком «Онбординг проекта»). */
  function needsOnboarding() {
    try {
      var session = getSession();
      return !!(session && !session.profile_filled);
    } catch (e) {
      return false;
    }
  }

  function clearSession() {
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
  }

  /* ---------- гостевой режим: без сессии страница закрыта ----------
     Незарегистрированным доступно только окно регистрации: содержимое
     страницы скрывается CSS-классом aios-guest (css/style.css), модалку
     нельзя закрыть, пока пользователь не войдёт или не зарегистрируется. */
  var guestLocked = false;

  function lockGuest() {
    if (guestLocked) return;
    guestLocked = true;
    try { document.body.classList.add('aios-guest'); } catch (e) {}
    if (closeBtn) closeBtn.hidden = true;
    setMode('register');
    openModal();
  }

  function unlockGuest() {
    if (!guestLocked) return;
    guestLocked = false;
    try { document.body.classList.remove('aios-guest'); } catch (e) {}
    if (closeBtn) closeBtn.hidden = false;
  }

  /* ---------- переключение режимов «вход ⇄ регистрация» ---------- */

  var mode = 'login';

  function setMode(next) {
    mode = next;
    var isReg = mode === 'register';
    if (title) title.textContent = isReg ? 'Регистрация в AIOS' : 'Вход в AIOS';
    if (password2Field) password2Field.hidden = !isReg;
    var pass2 = form && form.elements.password2;
    if (pass2) pass2.required = isReg; /* скрытые поля браузер не валидирует — управляю сам */
    /* согласия (обработка ПД + ознакомление с Политикой) — только при регистрации;
       обязательность проверяю сам в submit, как и у password2 */
    var consentFields = document.getElementById('consentFields');
    if (consentFields) consentFields.hidden = !isReg;
    ['consent_pdn', 'consent_policy'].forEach(function (n) {
      var cb = form && form.elements[n];
      if (cb) cb.required = isReg;
    });
    if (submitBtn) submitBtn.textContent = isReg ? 'Создать аккаунт' : 'Войти';
    if (altQuestion) altQuestion.textContent = isReg ? 'Уже есть аккаунт?' : 'Нет аккаунта?';
    if (registerLink) registerLink.hidden = isReg;
    if (loginLink) loginLink.hidden = !isReg;
    var pass = form && form.elements.password;
    if (pass) pass.setAttribute('autocomplete', isReg ? 'new-password' : 'current-password');
  }

  function showMsg(text, kind) {
    if (!msg) return;
    msg.textContent = text;
    msg.className = 'login-msg login-msg--' + kind;
    msg.hidden = false;
  }

  function hideMsg() {
    if (msg) msg.hidden = true;
    if (resendLink) resendLink.hidden = true;
  }

  function showPassMsg(text, kind) {
    if (!passMsg) return;
    passMsg.textContent = text;
    passMsg.className = 'login-msg login-msg--' + kind;
    passMsg.hidden = false;
  }

  function hidePassMsg() {
    if (passMsg) passMsg.hidden = true;
  }

  function openModal() {
    modal.hidden = false;
    document.body.classList.add('modal-open');
    hideMsg();
    var first = modal.querySelector('input');
    if (first) first.focus();
  }

  function closeModal() {
    if (guestLocked) return; /* гостевую модалку закрыть нельзя — только войти/зарегистрироваться */
    modal.hidden = true;
    document.body.classList.remove('modal-open');
    hideMsg();
    if (form) form.reset();
    setMode('login');
  }

  function openPassModal() {
    if (!passModal) return;
    closeUserMenu();
    passModal.hidden = false;
    document.body.classList.add('modal-open');
    hidePassMsg();
    var first = passModal.querySelector('input');
    if (first) first.focus();
  }

  function closePassModal() {
    if (!passModal) return;
    passModal.hidden = true;
    document.body.classList.remove('modal-open');
    hidePassMsg();
    if (passForm) passForm.reset();
  }

  function openUserMenu() {
    if (userMenu) userMenu.hidden = false;
  }

  function closeUserMenu() {
    if (userMenu) userMenu.hidden = true;
  }

  function renderAuthState() {
    var session = getSession();
    var isAdmin = !!(session && session.role === 'Администратор');
    /* гостевой режим: без сессии на странице видно только окно регистрации */
    if (session) { unlockGuest(); } else { lockGuest(); }
    if (session) {
      var displayName = session.name || session.login; /* имя опционально */
      btn.textContent = displayName;
      btn.classList.add('btn-login--user');
      btn.setAttribute('aria-label', displayName + ' — ' + session.role);
      if (userMenu) {
        var nameEl = userMenu.querySelector('.user-menu-name');
        var roleEl = userMenu.querySelector('.user-menu-role');
        if (nameEl) nameEl.textContent = displayName;
        if (roleEl) roleEl.textContent = session.role;
      }
    } else {
      btn.textContent = 'Войти';
      btn.classList.remove('btn-login--user');
      btn.removeAttribute('aria-label');
      closeUserMenu();
    }
    /* админ-пункты меню видны только роли «Администратор» */
    if (menuAdmin) menuAdmin.hidden = !isAdmin;
    if (menuPilot) menuPilot.hidden = !isAdmin;
    /* ПИШ: ссылка — администраторам, остальным — «в разработке» */
    if (menuPish) menuPish.hidden = !isAdmin;
    if (menuPishLock) menuPishLock.hidden = isAdmin;
    /* «Модели»: пункт меню и плитка на главной — только администраторам */
    if (menuModels) menuModels.hidden = !isAdmin;
    if (tileModels) tileModels.hidden = !isAdmin;
    /* «Настройки» и «Сменить пароль» — любому вошедшему */
    if (changePassBtn) changePassBtn.hidden = !session;
    if (btnSettings) btnSettings.hidden = !session;
    /* Оповещаем подсистемы (например, раздел «Рабочее пространство» в меню) */
    try {
      window.dispatchEvent(new CustomEvent('aios-auth-change'));
    } catch (e) {}
  }

  /* ---------- счётчик суточных токенов под именем в левом меню ----------

     Пользователь видит, сколько токенов LLM потратил за текущие сутки и
     какой у него лимит (GET /api/usage/me). Виджет обновляется при смене
     авторизации, после каждого ответа чата (событие 'aios-usage-refresh'
     из workspace.js) и раз в минуту; обнуление счётчика — 00:00 МСК. */

  function fmtTokensShort(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (Math.round(n / 1e5) / 10).toFixed(1).replace('.', ',') + ' млн';
    if (n >= 1e4) return Math.round(n / 1e3) + ' тыс.';
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, '\u2009');
  }

  function ensureTokensWidget() {
    if (document.getElementById('userTokens')) return;
    var wrap = btn.parentElement; /* .sidebar-user на всех страницах */
    if (!wrap) return;
    wrap.insertAdjacentHTML('beforeend',
      '<div class="user-tokens" id="userTokens" hidden>' +
      '  <div class="user-tokens-text">' +
      '    <span class="user-tokens-used">…</span>' +
      '    <span class="user-tokens-limit"></span>' +
      '  </div>' +
      '  <div class="user-tokens-bar" aria-hidden="true"><span class="user-tokens-fill"></span></div>' +
      '</div>');
  }

  function refreshTokenUsage() {
    ensureTokensWidget();
    var widget = document.getElementById('userTokens');
    if (!widget) return;
    var session = getSession();
    if (!session || !window.AIOS_API || !window.AIOS_API.usageMe) {
      widget.hidden = true;
      return;
    }
    window.AIOS_API.usageMe(session.token)
      .then(function (data) {
        if (!getSession()) return; /* успели разлогиниться */
        var used = Number(data && data.used_today) || 0;
        /* лимит null — у пользователя личные настройки внешнего API
           («Настройки → Внешний API»): работает по своему ключу, платформа
           показывает только потраченное за сутки */
        var limit = data && data.limit == null ? null : (Number(data.limit) || 0);
        var usedEl = widget.querySelector('.user-tokens-used');
        var limitEl = widget.querySelector('.user-tokens-limit');
        var bar = widget.querySelector('.user-tokens-bar');
        var fill = widget.querySelector('.user-tokens-fill');
        if (usedEl) usedEl.textContent = fmtTokensShort(used);
        if (limitEl) {
          limitEl.textContent = limit == null
            ? 'токенов за сегодня'
            : '/ ' + fmtTokensShort(limit) + ' токенов';
        }
        if (bar) bar.hidden = limit == null;
        if (fill) fill.style.width = (limit > 0 ? Math.min(100, Math.round(used * 100 / limit)) : 0) + '%';
        var over = limit != null && limit > 0 && used >= limit;
        widget.classList.toggle('user-tokens--over', over);
        widget.title = limit == null
          ? 'Токены LLM за текущие сутки: ' + used.toLocaleString('ru-RU') +
            ' · личные настройки внешнего API, без суточного лимита'
          : 'Токены LLM за текущие сутки: ' + used.toLocaleString('ru-RU') +
            ' из ' + limit.toLocaleString('ru-RU') + ' · счётчик обнуляется в 00:00 по Москве';
        widget.hidden = false;
      })
      .catch(function () { /* бэкенд недоступен — виджет не мешает */ });
  }

  ensureTokensWidget();
  window.addEventListener('aios-auth-change', refreshTokenUsage);
  document.addEventListener('aios-usage-refresh', refreshTokenUsage);
  setInterval(refreshTokenUsage, 60000);

  /* Кнопка в шапке: не авторизован — открыть модалку, авторизован — меню */
  btn.addEventListener('click', function () {
    if (getSession()) {
      if (userMenu && userMenu.hidden) { openUserMenu(); } else { closeUserMenu(); }
    } else {
      openModal();
    }
  });

  closeBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', function (e) {
    if (e.target === modal) closeModal();
  });
  if (passClose) passClose.addEventListener('click', closePassModal);
  if (passModal) {
    passModal.addEventListener('click', function (e) {
      if (e.target === passModal) closePassModal();
    });
  }
  if (changePassBtn) changePassBtn.addEventListener('click', openPassModal);
  /* любые элементы с data-open-pass (например, кнопка «Сменить пароль»
     на вкладке «Настройки») открывают ту же модалку */
  Array.prototype.forEach.call(document.querySelectorAll('[data-open-pass]'), function (el) {
    el.addEventListener('click', openPassModal);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (!modal.hidden) closeModal();
      if (passModal && !passModal.hidden) closePassModal();
      if (suggestModal && !suggestModal.hidden) closeSuggestModal();
      closeUserMenu();
    }
  });
  document.addEventListener('click', function (e) {
    if (userMenu && !userMenu.hidden && !userMenu.contains(e.target) && e.target !== btn) {
      closeUserMenu();
    }
  });

  /* ---------- отправка формы (вход / регистрация) ---------- */

  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      hideMsg();
      var API = window.AIOS_API;
      if (!API || !API.login) {
        showMsg('API недоступен: запустите бэкенд (uvicorn api.main:app)', 'error');
        return;
      }

      var login = form.elements.login.value.trim().toLowerCase();
      var password = form.elements.password.value;

      if (mode === 'register') {
        var password2 = form.elements.password2.value;
        if (password.length < 4) { showMsg('Пароль — минимум 4 символа', 'error'); return; }
        if (password !== password2) { showMsg('Пароли не совпадают', 'error'); return; }
        var consentPdn = !!(form.elements.consent_pdn && form.elements.consent_pdn.checked);
        var consentPolicy = !!(form.elements.consent_policy && form.elements.consent_policy.checked);
        if (!consentPdn || !consentPolicy) {
          showMsg('Для регистрации нужны оба согласия: на обработку персональных данных и с Политикой их обработки', 'error');
          return;
        }
        submitBtn.disabled = true;
        API.register(login, password, form.elements.website ? form.elements.website.value : '', consentPdn, consentPolicy)
          .then(function () {
            /* автовхода нет: сначала подтверждение e-mail по ссылке из письма */
            setMode('login');
            var loginInput = form.elements.login;
            if (loginInput) loginInput.value = login;
            form.elements.password2.value = '';
            showMsg('Аккаунт создан. Письмо со ссылкой подтверждения отправлено на ' +
              login + '. Перейдите по ссылке из письма и войдите.', 'note');
            if (resendLink) resendLink.hidden = false;
          })
          .catch(function (err) {
            showMsg(err && err.message ? err.message : 'Не удалось зарегистрироваться', 'error');
          })
          .then(function () { submitBtn.disabled = false; });
      } else {
        submitBtn.disabled = true;
        API.login(login, password)
          .then(function (res) {
            saveSession(res.token, res.user, res.profile_filled);
            renderAuthState();
            closeModal();
            /* незаполненный профиль — сначала «Настройки» (онбординг):
               заполнить «Данные пользователя» */
            if (needsOnboarding()) {
              location.href = 'settings.html?onboarding=1';
            }
          })
          .catch(function (err) {
            if (err && err.status === 403) {
              /* e-mail не подтверждён — даём запросить письмо повторно */
              showMsg(err && err.message ? err.message : 'E-mail не подтверждён', 'error');
              if (resendLink) resendLink.hidden = false;
            } else {
              showMsg(err && err.message ? err.message : 'Не удалось войти', 'error');
            }
          })
          .then(function () { submitBtn.disabled = false; });
      }
    });
  }

  /* Повторная отправка письма подтверждения (после 403 или сразу после регистрации) */
  if (resendLink) {
    resendLink.addEventListener('click', function (e) {
      e.preventDefault();
      var API = window.AIOS_API;
      if (!API || !API.resendConfirmation) return;
      var loginValue = form && form.elements.login ? form.elements.login.value.trim().toLowerCase() : '';
      var passwordValue = form && form.elements.password ? form.elements.password.value : '';
      resendLink.hidden = true;
      API.resendConfirmation(loginValue, passwordValue)
        .then(function () {
          showMsg('Письмо отправлено ещё раз — проверьте ящик ' + (loginValue || ''), 'note');
        })
        .catch(function (err) {
          showMsg(err && err.message ? err.message : 'Не удалось отправить письмо', 'error');
        })
        .then(function () { if (resendLink) resendLink.hidden = false; });
    });
  }

  /* Переключение «вход ⇄ регистрация» */
  if (registerLink) {
    registerLink.addEventListener('click', function (e) {
      e.preventDefault();
      setMode('register');
      hideMsg();
      var emailInput = form && form.elements.login;
      if (emailInput) emailInput.focus();
    });
  }
  if (loginLink) {
    loginLink.addEventListener('click', function (e) {
      e.preventDefault();
      setMode('login');
      hideMsg();
    });
  }

  /* Смена пароля (текущая сессия остаётся, остальные отзываются) */
  if (passForm) {
    passForm.addEventListener('submit', function (e) {
      e.preventDefault();
      hidePassMsg();
      var session = getSession();
      var API = window.AIOS_API;
      if (!session || !API || !API.changePassword) {
        showPassMsg('Сессия не найдена — войдите заново', 'error');
        return;
      }
      var oldPass = passForm.elements.old_password.value;
      var newPass = passForm.elements.new_password.value;
      var newPass2 = passForm.elements.new_password2.value;
      if (newPass.length < 4) { showPassMsg('Новый пароль — минимум 4 символа', 'error'); return; }
      if (newPass !== newPass2) { showPassMsg('Новые пароли не совпадают', 'error'); return; }
      if (passSubmit) passSubmit.disabled = true;
      API.changePassword(session.token, oldPass, newPass)
        .then(function () {
          closePassModal();
        })
        .catch(function (err) {
          showPassMsg(err && err.message ? err.message : 'Не удалось сменить пароль', 'error');
        })
        .then(function () { if (passSubmit) passSubmit.disabled = false; });
    });
  }

  /* Выход: чистим локальную сессию и инвалидируем токен на сервере */
  if (logoutBtn) {
    logoutBtn.addEventListener('click', function () {
      var session = getSession();
      clearSession();
      renderAuthState();
      if (session && window.AIOS_API && window.AIOS_API.logout) {
        window.AIOS_API.logout(session.token).catch(function () { /* токен мог истечь */ });
      }
    });
  }

  /* Фоновая проверка токена: истёкший/отозванный — разлогиниваем */
  (function verifySession() {
    var session = getSession();
    if (!session || !window.AIOS_API || !window.AIOS_API.me) return;
    window.AIOS_API.me(session.token).catch(function () {
      if (getSession()) {
        clearSession();
        renderAuthState();
      }
    });
  })();

  /* Возврат из письма: /index.html?confirmed=1|0 — открываем форму с сообщением */
  (function handleConfirmRedirect() {
    var match = /[?&]confirmed=(1|0)\b/.exec(location.search);
    if (!match) return;
    try { history.replaceState(null, '', location.pathname); } catch (e) { /* старые браузеры */ }
    openModal();
    if (match[1] === '1') {
      showMsg('E-mail подтверждён — теперь войдите', 'note');
    } else {
      setMode('login');
      showMsg('Ссылка подтверждения недействительна или устарела. Введите e-mail и пароль — и отправьте письмо ещё раз.', 'error');
      if (resendLink) resendLink.hidden = false;
    }
  })();

  renderAuthState();
})();
