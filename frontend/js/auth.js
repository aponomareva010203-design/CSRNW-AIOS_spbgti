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
    '      <label id="password2Field" hidden>',
    '        <span>Повторите пароль</span>',
    '        <input type="password" name="password2" autocomplete="new-password" minlength="4">',
    '      </label>',
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

  function showSuggestMsg(text, cls) {
    if (!suggestMsg) return;
    suggestMsg.textContent = text;
    suggestMsg.className = 'login-msg' + (cls ? ' ' + cls : '');
    suggestMsg.hidden = false;
  }

  function openSuggestModal() {
    if (!suggestModal) return;
    if (suggestMsg) suggestMsg.hidden = true;
    if (suggestForm) suggestForm.reset();
    suggestModal.hidden = false;
    var area = suggestForm && suggestForm.elements.message;
    if (area) area.focus();
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

  /* Онбординг: профиль не заполнен — ведём в «Кабинет», где нужно
     заполнить «Данные пользователя» (cabinet.html после сохранения
     сам откроет рабочий чат с навыком «Онбординг проекта»). */
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

  /* ---------- переключение режимов «вход ⇄ регистрация» ---------- */

  var mode = 'login';

  function setMode(next) {
    mode = next;
    var isReg = mode === 'register';
    if (title) title.textContent = isReg ? 'Регистрация в AIOS' : 'Вход в AIOS';
    if (password2Field) password2Field.hidden = !isReg;
    var pass2 = form && form.elements.password2;
    if (pass2) pass2.required = isReg; /* скрытые поля браузер не валидирует — управляю сам */
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
    /* «Настройки» (смена пароля) — любому вошедшему */
    if (changePassBtn) changePassBtn.hidden = !session;
    if (btnSettings) btnSettings.hidden = !session;
    /* Оповещаем подсистемы (например, раздел «Рабочее пространство» в меню) */
    try {
      window.dispatchEvent(new CustomEvent('aios-auth-change'));
    } catch (e) {}
  }

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
  if (btnSettings) btnSettings.addEventListener('click', openPassModal);
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
        submitBtn.disabled = true;
        API.register(login, password)
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
            /* незаполненный профиль — сначала «Кабинет» (онбординг) */
            if (needsOnboarding()) {
              location.href = 'cabinet.html?onboarding=1';
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
