/* ============================================================
   Кабинет: блок «Данные пользователя» (user/{id}-profile.json).

   Форму заполняет сам пользователь: ФИО, должности (динамические
   строки, до 10), учёная степень, научные интересы. Данные привязаны
   к учётной записи: идентификатор берётся только из Bearer-токена
   (GET|PUT /api/cabinet/profile), чужие блоки недостижимы. Скрытый
   блок «Контекст о пользователе» платформа формирует на сервере по
   сессиям и упоминаниям — в интерфейс он не выносится.

   Гость видит предложение войти; после входа (auth.js диспатчит
   событие 'aios-auth-change') анкета загружается с сервера,
   при истечении сессии страница возвращается в гостевой режим.
   ============================================================ */
(function () {
  var API = window.AIOS_API;
  var AUTH_KEY = 'aios-auth-v1';
  var MAX_POSITIONS = 10;

  var guest = document.getElementById('cabGuest');
  var guestMsg = document.getElementById('cabGuestMsg');
  var card = document.getElementById('cabCard');
  var form = document.getElementById('cabForm');
  var posWrap = document.getElementById('cabPositions');
  var posAdd = document.getElementById('cabPosAdd');
  var msg = document.getElementById('cabMsg');
  var status = document.getElementById('cabStatus');
  var updated = document.getElementById('cabUpdated');
  var saveBtn = document.getElementById('cabSave');
  var loginBtn = document.getElementById('cabLoginBtn');
  if (!guest || !card || !form) return;

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

  function fmtDate(ts) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '';
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear() +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
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

  /* ---------- загрузка и сохранение ---------- */

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
    card.hidden = !authed;
    if (guestMsg) {
      guestMsg.textContent = guestText || 'Данные кабинета доступны только после входа в AIOS.';
    }
  }

  function onSessionLost(text) {
    try { localStorage.removeItem(AUTH_KEY); } catch (e) { /* приватный режим */ }
    renderState(false, text);
  }

  function load() {
    var session = getSession();
    if (!session || !API || !API.cabinetGetProfile) {
      renderState(false);
      return;
    }
    renderState(true);
    showMsg('');
    showStatus('');
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
        showMsg('Данные сохранены', 'note');
        if (updated && data && data.updated_at) {
          updated.textContent = 'обновлено: ' + fmtDate(data.updated_at);
        }
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

  if (loginBtn) {
    loginBtn.addEventListener('click', function () {
      var btnLogin = document.getElementById('btnLogin');
      if (btnLogin) btnLogin.click(); /* модалку входа показывает auth.js */
    });
  }

  window.addEventListener('aios-auth-change', load);

  load();
})();