/* ============================================================
   Модальное окно входа/регистрации + серверная авторизация.

   Сессия хранится в localStorage (ключ aios-auth-v1 — его же читают
   pilot.js и workspace.js): {token, login, name, role}. Запросы идут
   через window.AIOS_API (/api/auth/*). Роль «Администратор» открывает
   пункты меню «Пилот: режим администратора» и «Админ-панель».
   ============================================================ */
(function () {
  var btn = document.getElementById('btnLogin');
  var modal = document.getElementById('loginModal');
  if (!btn || !modal) return;

  var closeBtn = document.getElementById('modalClose');
  var form = document.getElementById('loginForm');
  var msg = document.getElementById('loginMsg');
  var title = document.getElementById('loginTitle');
  var nameField = document.getElementById('nameField');
  var password2Field = document.getElementById('password2Field');
  var submitBtn = document.getElementById('loginSubmit');
  var registerLink = document.querySelector('.btn-register');
  var loginLink = document.querySelector('.btn-login-alt');
  var altQuestion = document.getElementById('altQuestion');
  var resendLink = document.getElementById('resendLink');
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

  function saveSession(token, user) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        token: token, login: user.login, name: user.name, role: user.role
      }));
    } catch (e) { /* приватный режим — живём без сохранения */ }
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
    if (nameField) nameField.hidden = !isReg;
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
    /* смена пароля доступна любому вошедшему */
    if (changePassBtn) changePassBtn.hidden = !session;
    /* Оповещаем подсистемы (например, кнопку рабочего пространства) */
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
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (!modal.hidden) closeModal();
      if (passModal && !passModal.hidden) closePassModal();
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
        var name = form.elements.name.value.trim();
        var password2 = form.elements.password2.value;
        if (name && name.length < 2) { showMsg('Имя — минимум 2 символа (или оставьте поле пустым)', 'error'); return; }
        if (password.length < 4) { showMsg('Пароль — минимум 4 символа', 'error'); return; }
        if (password !== password2) { showMsg('Пароли не совпадают', 'error'); return; }
        submitBtn.disabled = true;
        API.register(login, password, name)
          .then(function () {
            /* автовхода нет: сначала подтверждение e-mail по ссылке из письма */
            setMode('login');
            var loginInput = form.elements.login;
            if (loginInput) loginInput.value = login;
            form.elements.name.value = '';
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
            saveSession(res.token, res.user);
            renderAuthState();
            closeModal();
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
      var nameInput = form && form.elements.name;
      if (nameInput) nameInput.focus();
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
