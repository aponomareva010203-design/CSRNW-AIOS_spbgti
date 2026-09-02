/* Модальное окно входа + mock-авторизация */
(function () {
  var btn = document.getElementById('btnLogin');
  var modal = document.getElementById('loginModal');
  var closeBtn = document.getElementById('modalClose');
  var form = document.getElementById('loginForm');
  var msg = document.getElementById('loginMsg');
  var registerLink = document.querySelector('.btn-register');
  var userMenu = document.getElementById('userMenu');
  var logoutBtn = document.getElementById('btnLogout');
  if (!btn || !modal) return;

  /* Демо-учётная запись (заглушка до подключения серверной авторизации) */
  var MOCK_USER = {
    login: 'm.kharitonov@csr-nw.ru',
    password: '1234',
    name: 'Харитонов М.',
    role: 'Администратор'
  };
  var STORAGE_KEY = 'aios-auth-v1';

  function getSession() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function saveSession(user) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ name: user.name, role: user.role }));
    } catch (e) { /* приватный режим — живём без сохранения */ }
  }

  function clearSession() {
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
  }

  function showMsg(text, kind) {
    if (!msg) return;
    msg.textContent = text;
    msg.className = 'login-msg login-msg--' + kind;
    msg.hidden = false;
  }

  function hideMsg() {
    if (msg) msg.hidden = true;
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
  }

  function openUserMenu() {
    if (userMenu) userMenu.hidden = false;
  }

  function closeUserMenu() {
    if (userMenu) userMenu.hidden = true;
  }

  function renderAuthState() {
    var session = getSession();
    if (session) {
      btn.textContent = session.name;
      btn.classList.add('btn-login--user');
      btn.setAttribute('aria-label', session.name + ' — ' + session.role);
      if (userMenu) {
        var nameEl = userMenu.querySelector('.user-menu-name');
        var roleEl = userMenu.querySelector('.user-menu-role');
        if (nameEl) nameEl.textContent = session.name;
        if (roleEl) roleEl.textContent = session.role;
      }
    } else {
      btn.textContent = 'Войти';
      btn.classList.remove('btn-login--user');
      btn.removeAttribute('aria-label');
      closeUserMenu();
    }
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
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (!modal.hidden) closeModal();
      closeUserMenu();
    }
  });
  document.addEventListener('click', function (e) {
    if (userMenu && !userMenu.hidden && !userMenu.contains(e.target) && e.target !== btn) {
      closeUserMenu();
    }
  });

  /* Вход по mock-учётке */
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var login = form.elements.login.value.trim().toLowerCase();
      var password = form.elements.password.value;
      if (login === MOCK_USER.login && password === MOCK_USER.password) {
        saveSession(MOCK_USER);
        renderAuthState();
        closeModal();
      } else {
        showMsg('Неверный логин или пароль', 'error');
      }
    });
  }

  /* Регистрация — пока заглушка */
  if (registerLink) {
    registerLink.addEventListener('click', function (e) {
      e.preventDefault();
      showMsg('Регистрация появится позже. Сейчас доступна демо-учётная запись администратора.', 'note');
    });
  }

  /* Выход */
  if (logoutBtn) {
    logoutBtn.addEventListener('click', function () {
      clearSession();
      renderAuthState();
    });
  }

  renderAuthState();
})();
