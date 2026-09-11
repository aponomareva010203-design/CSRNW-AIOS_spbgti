/* ============================================================
   Кабинет: блок «Данные пользователя» (user/{id}-profile.json).

   Форму заполняет сам пользователь: ФИО, должности (динамические
   строки, до 10), учёная степень, научные интересы. Данные привязаны
   к учётной записи: идентификатор берётся только из Bearer-токена
   (GET|PUT /api/cabinet/profile), чужие блоки недостижимы. Скрытый
   блок «Контекст о пользователе» платформа формирует на сервере по
   сессиям и упоминаниям — в интерфейс он не выносится.

   Разделы «Данные пользователя», «История сессий» и «Результаты работы»
   сворачиваются; состояние — в localStorage (aios-cabinet-collapse-v1).

   «История сессий» — GET /api/cabinet/sessions (сводки только своих
   сессий). Клик по сессии диспатчит 'aios-open-session' — панель рабочего
   чата открывает её (workspace.js). Кнопка удаления —
   DELETE /api/cabinet/sessions/{id}.

   «Результаты работы» — GET /api/cabinet/projects (свои карточки role:
   owner + проекты с участием role: participant) и GET /api/ontologies:
   онтологии проекта показываются карточками-ссылками на 3D-визуализацию;
   для будущих типов артефактов (RAG, модели, агенты, полигоны, дашборды)
   место уже размечено.

   Гость видит предложение войти; после входа (auth.js диспатчит
   событие 'aios-auth-change') разделы загружаются с сервера,
   при истечении сессии страница возвращается в гостевой режим.
   ============================================================ */
(function () {
  var API = window.AIOS_API;
  var AUTH_KEY = 'aios-auth-v1';
  var ONBOARDING_KEY = 'aios-onboarding-v1';
  var SKILL_KEY = 'aios-workspace-skill-v1';
  var MAX_POSITIONS = 10;

  /* Режим онбординга: после входа профиль не заполнен — auth.js ведёт
     сюда с ?onboarding=1; флаг живёт в sessionStorage, пока «Данные
     пользователя» не сохранены. После сохранения автоматически
     открываем рабочий чат с навыком «Онбординг проекта». */
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
  var consent = document.getElementById('cabConsent');
  var loginBtn = document.getElementById('cabLoginBtn');
  var sessionsWrap = document.getElementById('cabSessions');
  var sessionsMsg = document.getElementById('cabSessionsMsg');
  var sessionsCount = document.getElementById('cabSessionsCount');
  var resultsBody = document.getElementById('cabResultsBody');
  var resultsOwnerWrap = document.getElementById('cabResultsOwner');
  var resultsParticipantWrap = document.getElementById('cabResultsParticipant');
  var resultsOntoWrap = document.getElementById('cabResultsOntologies');
  var resultsRagWrap = document.getElementById('cabResultsRag');
  var resultsMsg = document.getElementById('cabResultsMsg');
  var resultsCount = document.getElementById('cabResultsCount');
  var progressCard = document.getElementById('cabProgressCard');
  var progressBar = document.getElementById('cabProgressBar');
  var progressHint = document.getElementById('cabProgressHint');
  var teamRows = document.getElementById('cabTeamRows');
  var teamWrap = document.getElementById('cabTeamWrap');
  var teamMsg = document.getElementById('cabTeamMsg');
  var teamCount = document.getElementById('cabTeamCount');
  var sessionsLoaded = false;
  var resultsLoaded = false;
  var teamLoaded = false;
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

  /* ---------- прогресс-бар этапов ----------
     1. Профиль заполнен — все поля блока «Данные пользователя».
     2. Проект присвоен — есть проект (свой или с участием).
     3. Гипотезы сформулированы — для какого-то проекта есть карточка
        гипотез (data/hypotheses/hyp-{id}.json, навык «Гипотезы проекта»);
        признак hypotheses_done отдаёт GET /api/cabinet/projects.
     4. Система знаний сформирована — в каком-то проекте есть онтология.
     5–7. Модель / Агент / Полигон — пока неактивны. */

  var progress = {
    profileDone: false,
    projectDone: false,
    hypDone: false,
    knowDone: false,
    progressLoaded: false
  };

  var PROGRESS_STEPS = [
    { key: 'profileDone', label: 'Заполнить профиль' },
    { key: 'projectDone', label: 'Участие в проекте' },
    { key: 'hypDone', label: 'Гипотезы сформулированы' },
    { key: 'knowDone', label: 'Система знаний сформирована' },
    { key: null, label: 'Модель обучена', locked: true },
    { key: null, label: 'Агент создан', locked: true },
    { key: null, label: 'Полигон создан', locked: true }
  ];

  var PROGRESS_HINTS = {
    profileDone: 'Шаг 1 из 4: заполните все поля блока «Данные пользователя».',
    projectDone: 'Шаг 2 из 4: проект присваивается навыком «Руководитель проекта» в рабочем чате.',
    hypDone: 'Шаг 3 из 4: гипотезы по артефактам проекта формулирует навык «Гипотезы проекта».',
    knowDone: 'Шаг 4 из 4: соберите онтологию проекта навыком «Конструктор онтологий».',
    allDone: 'Все доступные этапы пройдены — дальше платформа появится позже.'
  };

  function allHypothesesDone(card) {
    /* признак отдаёт GET /api/cabinet/projects (поле hypotheses_done);
       карточки гипотез живут в data/hypotheses/hyp-{id}.json, поле
       artifacts карточки проекта навык не заполняет */
    return !!(card && card.hypotheses_done);
  }

  function profileComplete(p) {
    p = p || {};
    var positions = (p.positions || []).filter(function (x) { return String(x || '').trim(); });
    return !!(String(p.last_name || '').trim() && String(p.first_name || '').trim() &&
      String(p.patronymic || '').trim() && positions.length &&
      String(p.degree || '').trim() && String(p.interests || '').trim());
  }

  function progressHintText() {
    if (!progress.profileDone) return PROGRESS_HINTS.profileDone;
    if (!progress.projectDone) return PROGRESS_HINTS.projectDone;
    if (!progress.hypDone) return PROGRESS_HINTS.hypDone;
    if (!progress.knowDone) return PROGRESS_HINTS.knowDone;
    return PROGRESS_HINTS.allDone;
  }

  function renderProgress() {
    if (!progressCard || !progressBar) return;
    progressBar.innerHTML = PROGRESS_STEPS.map(function (step) {
      var cls = 'cab-progress-step';
      if (step.locked) cls += ' cab-progress-step--locked';
      else if (progress[step.key]) cls += ' cab-progress-step--done';
      var mark = step.locked
        ? '<span class="cab-progress-dot">' + '•' + '</span>'
        : progress[step.key]
          ? '<span class="cab-progress-dot"><svg class="cab-progress-check" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5 6.5 12 13 4.5"/></svg></span>'
          : '<span class="cab-progress-dot">' + (PROGRESS_STEPS.indexOf(step) + 1) + '</span>';
      return '<li class="' + cls + '">' + mark +
        '<span class="cab-progress-label">' + esc(step.label) + '</span></li>';
    }).join('');
    if (progressHint) {
      progressHint.textContent = progressHintText();
      progressHint.hidden = false;
    }
  }

  function loadProgress() {
    var session = getSession();
    if (!session || !API || !API.cabinetListProjects) return;
    API.cabinetListProjects(session.token)
      .then(function (data) {
        var projects = (data && data.items) || [];
        progress.projectDone = projects.length > 0;
        progress.hypDone = projects.some(allHypothesesDone);
        var finish = function (ontologies) {
          var byId = {};
          (ontologies || []).forEach(function (o) {
            if (o && o.project_id) byId[o.project_id] = true;
          });
          progress.knowDone = projects.some(function (p) { return byId[p.id]; });
          progress.progressLoaded = true;
          renderProgress();
        };
        if (!API.ontologiesList) { finish([]); return; }
        API.ontologiesList(session.token)
          .then(function (ontoData) { finish((ontoData && ontoData.items) || []); })
          .catch(function () { finish([]); });
      })
      .catch(function () { /* прогресс не критичен — карточки ниже покажут ошибку */ });
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
    progress.profileDone = profileComplete(p);
    renderProgress();
  }

  function renderState(authed, guestText) {
    guest.hidden = !!authed;
    card.hidden = !authed;
    var sessionsCard = document.getElementById('cabSessionsCard');
    var resultsCard = document.getElementById('cabResultsCard');
    var teamCard = document.getElementById('cabTeamCard');
    if (sessionsCard) sessionsCard.hidden = !authed;
    if (resultsCard) resultsCard.hidden = !authed;
    if (teamCard) teamCard.hidden = !authed;
    if (!authed) {
      sessionsLoaded = false;
      resultsLoaded = false;
      teamLoaded = false;
      progress.progressLoaded = false;
      progress.profileDone = false;
      progress.projectDone = false;
      progress.hypDone = false;
      progress.knowDone = false;
      if (progressCard) progressCard.hidden = true;
      if (sessionsWrap) { sessionsWrap.innerHTML = ''; sessionsWrap.hidden = true; }
      [resultsOwnerWrap, resultsParticipantWrap, resultsOntoWrap, resultsRagWrap].forEach(function (w) {
        if (w) { w.innerHTML = ''; w.hidden = true; }
      });
      if (sessionsCount) sessionsCount.textContent = '';
      if (resultsCount) resultsCount.textContent = '';
    }
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
    ensureSessions();
    ensureResults();
    ensureTeam();
    if (progressCard) progressCard.hidden = false;
    if (!progress.progressLoaded) loadProgress();
    renderProgress();
    if (onboarding) {
      showMsg('Заполните «Данные пользователя» — это последний шаг регистрации. ' +
        'После сохранения откроется рабочий чат с навыком «Онбординг проекта».', 'note');
    } else {
      showMsg('');
    }
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
        progress.profileDone = profileComplete({
          last_name: form.elements.last_name.value,
          first_name: form.elements.first_name.value,
          patronymic: form.elements.patronymic.value,
          positions: positions,
          degree: form.elements.degree.value,
          interests: form.elements.interests.value
        });
        renderProgress();
        /* Онбординг завершён: помечаем профиль заполненным в локальной
           сессии, запускаем навык «Онбординг проекта» в рабочем чате
           и возвращаемся на главную (workspace.js подхватит флаг). */
        if (onboarding) {
          try {
            sessionStorage.removeItem(ONBOARDING_KEY);
            var session = getSession();
            if (session) {
              session.profile_filled = true;
              localStorage.setItem(AUTH_KEY, JSON.stringify(session));
            }
            localStorage.setItem(SKILL_KEY, JSON.stringify({
              skill_id: 'project-onboarding',
              name: 'Онбординг проекта',
              prompt: 'Начните онбординг: познакомьтесь со мной и помогите ' +
                'сформулировать тему и карточку моего первого проекта.',
              open: true
            }));
          } catch (e) { /* приватный режим — просто остаёмся в кабинете */ }
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

  /* ---------- моя команда ---------- */

  var TEAM_PERMS = [
    ['projects', 'Проекты'],
    ['ontologies', 'Онтологии'],
    ['rag', 'RAG-системы']
  ];

  function showTeamMsg(text, kind) {
    if (!teamMsg) return;
    teamMsg.textContent = text || '';
    teamMsg.className = 'login-msg login-msg--' + (kind || 'error');
    teamMsg.hidden = !text;
  }

  function teamPermCell(row) {
    var perms = row.permissions;
    /* себе права не выдаются — у руководителя всё уже есть */
    if (!perms) return '<span class="cab-team-self">—</span>';
    return TEAM_PERMS.map(function (p) {
      return '<label class="cab-team-perm" title="Право редактировать ' + p[1].toLowerCase() +
        ' (без удаления)">' +
        '<input type="checkbox" data-perm="' + p[0] + '"' + (perms[p[0]] ? ' checked' : '') + '> ' +
        esc(p[1]) + '</label>';
    }).join('');
  }

  /* список проектов чипа — данные для плавающей подсказки
     (data-projects, не data-tooltip: общий CSS-tooltip здесь обрезался бы) */
  function teamChipTooltip(row, status) {
    var projects = (row.projects || {})[status] || [];
    if (!projects.length) return '';
    return ' data-projects="' + esc(projects.join('\n')) + '"';
  }

  function teamRowHtml(row, index) {
    return '<tr data-user-id="' + esc(row.user_id) + '">' +
      '<td class="at-num">' + (index + 1) + '</td>' +
      '<td class="at-name">' + esc(row.full_name || 'Без имени') + '</td>' +
      '<td class="at-login">' + esc(row.position || '—') + '</td>' +
      '<td>' + (row.statuses || []).map(function (s) {
        return '<span class="cab-role cab-team-chip cab-role--' +
          (s === 'руководитель проекта' ? 'owner' : 'participant') + '"' +
          teamChipTooltip(row, s) + '>' + esc(s) + '</span>';
      }).join(' ') + '</td>' +
      '<td class="cab-team-perms">' + teamPermCell(row) + '</td>' +
      '</tr>';
  }  /* подсказка при наведении на чип статуса: о каких проектах речь.
     Обычный CSS-tooltip не подходит — обёртка таблицы режет его
     overflow:hidden, поэтому рисуем плавающий элемент поверх body. */
  var teamTip = null;

  function showTeamTooltip(chip) {
    hideTeamTooltip();
    var text = chip.getAttribute('data-projects');
    if (!text) return;
    teamTip = document.createElement('div');
    teamTip.className = 'cab-team-tip';
    teamTip.textContent = text;
    document.body.appendChild(teamTip);
    var r = chip.getBoundingClientRect();
    var tipR = teamTip.getBoundingClientRect();
    var left = Math.max(8, Math.min(r.left + r.width / 2 - tipR.width / 2,
      window.innerWidth - tipR.width - 8));
    var top = r.top - tipR.height - 8;
    if (top < 8) top = r.bottom + 8;
    teamTip.style.left = left + 'px';
    teamTip.style.top = top + 'px';
    teamTip.classList.add('cab-team-tip--visible');
  }

  function hideTeamTooltip() {
    if (teamTip) {
      teamTip.remove();
      teamTip = null;
    }
  }

  function renderTeam(items) {
    if (!teamRows || !teamWrap) return;
    if (teamCount) {
      teamCount.textContent = items.length
        ? items.length + ' ' + plural(items.length, 'человек', 'человека', 'человек')
        : '';
    }
    if (!items.length) {
      teamWrap.hidden = true;
      teamRows.innerHTML = '';
      showTeamMsg('Пока никого: команда появится, когда в ваших проектах появятся зарегистрированные участники.', 'note');
      return;
    }
    showTeamMsg('');
    teamRows.innerHTML = items.map(teamRowHtml).join('');
    teamWrap.hidden = false;
  }

  function loadTeam() {
    var session = getSession();
    if (!session || !API || !API.cabinetListTeam) return;
    API.cabinetListTeam(session.token)
      .then(function (data) {
        teamLoaded = true;
        renderTeam((data && data.items) || []);
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          onSessionLost('Сессия истекла — войдите заново.');
          return;
        }
        if (teamMsg) {
          teamMsg.textContent = 'Не удалось загрузить команду: ' +
            (err && err.message ? err.message : 'ошибка сети');
          teamMsg.hidden = false;
        }
      });
  }

  function saveTeamPerm(tr, permKey, checked) {
    var userId = tr.getAttribute('data-user-id');
    var session = getSession();
    if (!userId || !session || !API.cabinetSetTeamPermissions) return;
    /* собираем актуальное состояние всех чекбоксов строки */
    var permissions = {};
    Array.prototype.forEach.call(tr.querySelectorAll('[data-perm]'), function (input) {
      permissions[input.getAttribute('data-perm')] = input.checked;
    });
    API.cabinetSetTeamPermissions(session.token, userId, permissions)
      .catch(function (err) {
        /* откатываем переключение при ошибке */
        var box = tr.querySelector('[data-perm="' + permKey + '"]');
        if (box) box.checked = !checked;
        if (err && err.status === 401) {
          onSessionLost('Сессия истекла — войдите заново.');
          return;
        }
        showTeamMsg('Не удалось сохранить права: ' + (err && err.message ? err.message : 'ошибка сети'));
      });
  }

  if (teamRows) {
    teamRows.addEventListener('change', function (e) {
      var input = e.target && e.target.closest ? e.target.closest('[data-perm]') : null;
      if (!input) return;
      saveTeamPerm(input.closest('tr'), input.getAttribute('data-perm'), input.checked);
    });
    teamRows.addEventListener('mouseover', function (e) {
      var chip = e.target && e.target.closest ? e.target.closest('.cab-team-chip') : null;
      if (chip) showTeamTooltip(chip);
    });
    teamRows.addEventListener('mouseout', function (e) {
      var chip = e.target && e.target.closest ? e.target.closest('.cab-team-chip') : null;
      if (chip) hideTeamTooltip();
    });
  }

  bindSection('cabTeamHead', 'cabTeamBody', 'team', ensureTeam);

  if (loginBtn) {
    loginBtn.addEventListener('click', function () {
      var btnLogin = document.getElementById('btnLogin');
      if (btnLogin) btnLogin.click(); /* модалку входа показывает auth.js */
    });
  }

  /* ---------- сворачиваемые разделы ---------- */

  var COLLAPSE_KEY = 'aios-cabinet-collapse-v1';

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

  /* onOpen вызывается при раскрытии свернутого раздела — догрузить данные */
  function bindSection(headId, bodyId, key, onOpen) {
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
      if (open && onOpen) onOpen();
    });
    apply();
  }

  function plural(n, one, few, many) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  }

  function ensureSessions() { if (!sessionsLoaded && !readCollapse().sessions) loadSessions(); }
  function ensureResults() { if (!resultsLoaded && !readCollapse().results) loadResults(); }
  function ensureTeam() { if (!teamLoaded && !readCollapse().team) loadTeam(); }

  /* ---------- история сессий ---------- */

  function sessionRow(s) {
    var row = document.createElement('div');
    row.className = 'cab-session';
    row.setAttribute('data-id', s.id || '');
    row.innerHTML =
      '<button class="cab-session-open" type="button" title="Открыть в рабочем пространстве">' +
        '<span class="cab-session-title">' + esc(s.title || 'Без названия') + '</span>' +
        '<span class="cab-session-meta">' + esc(fmtDate(s.updated_at)) +
          ' · ' + (s.message_count || 0) + ' ' +
          plural(s.message_count || 0, 'сообщение', 'сообщения', 'сообщений') + '</span>' +
      '</button>' +
      '<button class="cab-session-del" type="button" title="Удалить сессию">✕</button>';
    return row;
  }

  function renderSessions(items) {
    if (!sessionsWrap) return;
    sessionsWrap.innerHTML = '';
    sessionsWrap.hidden = false;
    if (sessionsMsg) sessionsMsg.hidden = true;
    if (sessionsCount) {
      sessionsCount.textContent = items.length
        ? items.length + ' ' + plural(items.length, 'сессия', 'сессии', 'сессий')
        : '';
    }
    if (!items.length) {
      sessionsWrap.innerHTML = '<p class="cab-empty">Здесь появятся ваши диалоги рабочего чата.</p>';
      return;
    }
    items.forEach(function (s) { sessionsWrap.appendChild(sessionRow(s)); });
  }

  function loadSessions() {
    var session = getSession();
    if (!session || !API || !API.cabinetListSessions) return;
    API.cabinetListSessions(session.token)
      .then(function (data) {
        sessionsLoaded = true;
        renderSessions((data && data.items) || []);
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          onSessionLost('Сессия истекла — войдите заново.');
          return;
        }
        if (sessionsMsg) {
          sessionsMsg.textContent = 'Не удалось загрузить историю сессий: ' +
            (err && err.message ? err.message : 'ошибка сети');
          sessionsMsg.hidden = false;
        }
      });
  }

  function deleteSession(row) {
    var id = row ? row.getAttribute('data-id') : '';
    var session = getSession();
    if (!id || !session || !API || !API.cabinetDeleteSession) return;
    if (!window.confirm('Удалить сессию из истории? Действие необратимо.')) return;
    API.cabinetDeleteSession(session.token, id)
      .then(function () {
        row.remove();
        var left = sessionsWrap ? sessionsWrap.querySelectorAll('.cab-session').length : 0;
        if (!left) renderSessions([]);
        if (sessionsCount) {
          sessionsCount.textContent = left ? left + ' ' + plural(left, 'сессия', 'сессии', 'сессий') : '';
        }
        /* если удалённая сессия была открыта в чате — забываем её и там */
        try {
          if (localStorage.getItem('aios-workspace-session-v1') === id) {
            localStorage.removeItem('aios-workspace-session-v1');
          }
        } catch (e) { /* приватный режим */ }
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          onSessionLost('Сессия истекла — войдите заново.');
          return;
        }
        if (sessionsMsg) {
          sessionsMsg.textContent = 'Не удалось удалить сессию: ' +
            (err && err.message ? err.message : 'ошибка сети');
          sessionsMsg.hidden = false;
        }
      });
  }

  if (sessionsWrap) {
    sessionsWrap.addEventListener('click', function (e) {
      var del = e.target && e.target.closest ? e.target.closest('.cab-session-del') : null;
      if (del) {
        deleteSession(del.closest('.cab-session'));
        return;
      }
      var openBtn = e.target && e.target.closest ? e.target.closest('.cab-session-open') : null;
      if (!openBtn) return;
      var row = openBtn.closest('.cab-session');
      var id = row ? row.getAttribute('data-id') : '';
      if (id) {
        document.dispatchEvent(new CustomEvent('aios-open-session', { detail: { session_id: id } }));
      }
    });
  }

  /* ---------- результаты работы ---------- */

  function infoRow(label, value) {
    if (!value) return '';
    return '<div class="cab-project-row"><span class="cab-project-label">' + label +
      '</span><span class="cab-project-value">' + value + '</span></div>';
  }

  function ontologyCard(o) {
    var status = o.status === 'active' ? 'в каталоге' : 'черновик';
    var classes = typeof o.classes_count === 'number' ? o.classes_count : (Array.isArray(o.classes) ? o.classes.length : 0);
    var relations = typeof o.relations_count === 'number' ? o.relations_count : (Array.isArray(o.relations) ? o.relations.length : 0);
    var meta = status + ' · ' + classes + ' ' + plural(classes, 'класс', 'класса', 'классов') +
      ' · ' + relations + ' ' + plural(relations, 'связь', 'связи', 'связей');
    if (o.updated_at) meta += ' · ' + fmtDate(o.updated_at);
    var ontoId = o.id || '';
    var item = document.createElement('div');
    item.className = 'cab-project';
    item.setAttribute('data-onto-id', ontoId);
    item.innerHTML =
      '<button class="cab-project-head cab-onto-head" type="button" aria-expanded="false">' +
        '<span class="cab-project-title">' + esc(o.name || 'Без названия') + '</span>' +
        '<span class="cab-artifact-meta">' + esc(meta) + '</span>' +
        '<span class="cab-sec-chev" aria-hidden="true"></span>' +
      '</button>' +
      '<div class="cab-project-body cab-onto-body" hidden></div>';
    return item;
  }

  function loadOntoBody(body, ontoId) {
    var session = getSession();
    if (!session || !API.ontologiesGet) return;
    body.innerHTML = '<p class="mm-source">Загрузка…</p>';
    API.ontologiesGet(session.token, ontoId).then(function (card) {
      var cqs = Array.isArray(card.competency_questions) ? card.competency_questions : [];
      var cqHtml = cqs.length
        ? '<ul class="cab-onto-cq">' + cqs.map(function (q) {
            return '<li>' + esc(q.question || '') + '</li>';
          }).join('') + '</ul>'
        : '';
      body.innerHTML =
        (card.description ? '<p class="mm-desc">' + esc(card.description) + '</p>' : '') +
        '<p class="mm-source">Статус: ' + esc(card.status === 'active' ? 'в каталоге' : 'черновик') +
          (card.domain ? ' · домен: ' + esc(card.domain) : '') + '</p>' +
        (cqHtml ? '<p class="mm-source"><strong>Компетентностные вопросы:</strong></p>' + cqHtml : '') +
        '<div class="rag-publish-row">' +
          '<a class="btn-primary" href="ontology.html?id=' + encodeURIComponent(ontoId) + '" target="_blank" rel="noopener noreferrer">3D-визуализация</a>' +
        '</div>';
    }).catch(function (err) {
      body.innerHTML = '<p class="mm-source">Не удалось загрузить онтологию: ' +
        esc(err && err.message ? err.message : 'ошибка сети') + '</p>';
    });
  }

  /* ссылка на скачивание файла карточки (GET /api/projects/{id}/files/{name}) */
  function fileLinks(p, field) {
    var items = p[field] || [];
    if (!items.length) return '';
    var links = items.map(function (name) {
      return '<a href="#" data-prjfile="' + esc(p.id) + '/' + encodeURIComponent(name) + '">' +
        esc(name) + '</a>';
    });
    return infoRow(field === 'groundwork_files' ? 'Материалы задела' : 'Презентации',
      links.join('; '));
  }

  function projectDetails(p, ontos) {
    var html = '';
    html += infoRow('Цель', esc(p.goal));
    html += infoRow('Описание', esc(p.description));
    html += infoRow('Участники', esc(p.participants));
    html += infoRow('Подразделения', esc(Array.isArray(p.departments) ? p.departments.join('; ') : (p.departments || '')));
    html += infoRow('Бюджет', esc(p.budget));
    html += infoRow('Численность', esc(p.team_size));
    html += infoRow('Задел', esc(p.groundwork));
    if ((p.groundwork_links || []).length) {
      html += infoRow('Ссылки задела', esc(p.groundwork_links.join('; ')));
    }
    html += fileLinks(p, 'groundwork_files');
    html += fileLinks(p, 'presentation_files');
    html += '<div class="cab-project-sub">Гипотезы</div>' +
      '<div class="cab-hyps" data-hyp="' + esc(p.id || '') + '">' +
      '<p class="cab-empty">загрузка…</p></div>';
    html += '<div class="cab-project-sub">Артефакты проекта</div>';
    if (ontos.length) {
      html += '<div class="cab-artifacts">' + ontos.map(ontologyCard).join('') + '</div>';
    } else {
      html += '<p class="cab-empty">Онтологий в этом проекте пока нет — соберите их навыком «Конструктор онтологий» в рабочем чате.</p>';
    }
    html += '<p class="cab-soon">Модели, агенты и полигоны по этому проекту появятся позже.</p>';
    return html;
  }

  function resultItem(p, ontos) {
    var item = document.createElement('div');
    item.className = 'cab-project' + (p.status === 'draft' ? ' cab-project--draft' : '');
    item.setAttribute('data-id', p.id || '');
    var role = p.role === 'participant'
      ? '<span class="cab-role cab-role--participant">участник</span>'
      : '<span class="cab-role cab-role--owner">руководитель</span>';
    var status = p.status === 'draft' ? '<span class="prj-status prj-status--draft">черновик</span>' : '';
    /* править карточку может только руководитель (владелец) —
       форма встроена прямо в тело карточки */
    var edit = p.role !== 'participant' ? projectFormHtml(p) : '';
    item.innerHTML =
      '<button class="cab-project-head" type="button" aria-expanded="false">' +
        '<span class="cab-project-title">' + esc(p.title || 'Без названия') + '</span>' +
        role + status +
        '<span class="cab-sec-chev" aria-hidden="true"></span>' +
      '</button>' +
      '<div class="cab-project-body" hidden>' + projectDetails(p, ontos) + edit + '</div>';
    return item;
  }

  function ragCard(r) {
    var status = r.status === 'active' || r.status === 'published' ? 'в каталоге' : 'черновик';
    var docs = typeof r.documents_count === 'number' ? r.documents_count :
      (Array.isArray(r.documents) ? r.documents.length : 0);
    var meta = status + ' · ' + docs + ' ' + plural(docs, 'документ', 'документа', 'документов');
    if (r.updated_at) meta += ' · ' + fmtDate(r.updated_at);
    var ragId = r.rag_id || r.id || '';
    var item = document.createElement('div');
    item.className = 'cab-project';
    item.setAttribute('data-rag-id', ragId);
    item.innerHTML =
      '<button class="cab-project-head cab-rag-head" type="button" aria-expanded="false">' +
        '<span class="cab-project-title">' + esc(r.name || r.title || 'Без названия') + '</span>' +
        '<span class="cab-artifact-meta">' + esc(meta) + '</span>' +
        '<span class="cab-sec-chev" aria-hidden="true"></span>' +
      '</button>' +
      '<div class="cab-project-body cab-rag-body" hidden></div>';
    return item;
  }

  /* ---------- разворачиваемая карточка RAG-базы: загрузка/публикация прямо в кабинете ---------- */

  var DOC_STATUS_LABEL_CAB = {
    queued: 'в очереди',
    processing: 'обрабатывается',
    ready: 'готов',
    error: 'ошибка'
  };

  var ragPollTimers = {};

  function stopRagPolling(ragId) {
    if (ragPollTimers[ragId]) {
      clearInterval(ragPollTimers[ragId]);
      delete ragPollTimers[ragId];
    }
  }

  function stopAllRagPolling() {
    Object.keys(ragPollTimers).forEach(stopRagPolling);
  }

  function renderRagDocs(container, items) {
    if (!items.length) {
      container.innerHTML = '<p class="mm-source">Документов пока нет — загрузите первый файл.</p>';
      return;
    }
    container.innerHTML = items.map(function (d) {
      var status = d.status || 'queued';
      var label = DOC_STATUS_LABEL_CAB[status] || status;
      return '<div class="rag-doc-row">' +
        '<span>' + esc(d.filename || d.doc_id) + '</span>' +
        '<span class="rag-doc-status rag-doc-status--' + esc(status) + '">' + esc(label) + '</span>' +
        (d.error ? '<span class="mm-source">' + esc(d.error) + '</span>' : '') +
        '</div>';
    }).join('');
  }

  function pollRagDocs(session, ragId, docsEl) {
    if (!API.ragListDocuments) return;
    API.ragListDocuments(session.token, ragId).then(function (data) {
      renderRagDocs(docsEl, (data && data.items) || []);
    }).catch(function () { /* сеть моргнула — подождём следующий тик */ });
  }

  function loadRagBody(body, ragId) {
    var session = getSession();
    if (!session || !API.ragGet) return;
    body.innerHTML = '<p class="mm-source">Загрузка…</p>';
    API.ragGet(session.token, ragId).then(function (card) {
      body.innerHTML =
        (card.description ? '<p class="mm-desc">' + esc(card.description) + '</p>' : '') +
        '<p class="mm-source">Статус: ' + esc(card.status || 'draft') +
          (card.ontology_id ? ' · онтология: ' + esc(card.ontology_id) : '') + '</p>' +
        '<div class="rag-upload">' +
          '<label for="ragFile-' + esc(ragId) + '" class="btn-primary">Загрузить документ</label>' +
          '<input type="file" id="ragFile-' + esc(ragId) + '" accept=".pdf,.txt,.md" hidden>' +
          '<span class="mm-source rag-upload-status">До 10 МБ на файл (.pdf, .txt, .md)</span>' +
        '</div>' +
        '<div class="rag-doc-list rag-docs-container"></div>' +
        '<div class="rag-publish-row">' +
          '<span class="mm-source rag-publish-msg"></span>' +
          '<button type="button" class="btn-primary rag-publish-btn">Опубликовать базу</button>' +
        '</div>';

      var docsEl = body.querySelector('.rag-docs-container');
      var fileInput = body.querySelector('input[type="file"]');
      var uploadStatus = body.querySelector('.rag-upload-status');
      var publishBtn = body.querySelector('.rag-publish-btn');
      var publishMsg = body.querySelector('.rag-publish-msg');

      pollRagDocs(session, ragId, docsEl);
      stopRagPolling(ragId);
      ragPollTimers[ragId] = setInterval(function () { pollRagDocs(session, ragId, docsEl); }, 4000);

      fileInput.addEventListener('change', function () {
        var file = fileInput.files && fileInput.files[0];
        if (!file || !API.ragUploadDocument) return;
        if (file.size > 10 * 1024 * 1024) {
          uploadStatus.textContent = 'Файл превышает лимит 10 МБ — выберите файл меньшего размера.';
          fileInput.value = '';
          return;
        }
        uploadStatus.textContent = 'Загрузка «' + file.name + '»…';
        API.ragUploadDocument(session.token, ragId, file).then(function () {
          uploadStatus.textContent = 'Файл принят, идёт обработка.';
          pollRagDocs(session, ragId, docsEl);
        }).catch(function (err) {
          uploadStatus.textContent = (err && err.status === 413)
            ? 'Файл превышает лимит 10 МБ — выберите файл меньшего размера.'
            : 'Ошибка загрузки: ' + (err && err.message ? err.message : 'сеть');
        }).finally(function () {
          fileInput.value = '';
        });
      });

      publishBtn.addEventListener('click', function () {
        if (!API.ragPublish) return;
        publishMsg.textContent = 'Публикация…';
        API.ragPublish(session.token, ragId).then(function () {
          publishMsg.textContent = 'Опубликовано в каталоге «Данные и знания».';
        }).catch(function (err) {
          publishMsg.textContent = 'Не удалось опубликовать: ' + (err && err.message ? err.message : 'ошибка');
        });
      });
    }).catch(function (err) {
      body.innerHTML = '<p class="mm-source">Не удалось загрузить базу: ' +
        esc(err && err.message ? err.message : 'ошибка сети') + '</p>';
    });
  }

  /* ---------- пустая карточка-черновик проекта ---------- */

  /* поля формы пустой карточки: [ключ, подпись, input|textarea] */
  var DRAFT_FIELDS = [
    ['title', 'Название', 'input'],
    ['goal', 'Цель (SMART)', 'textarea'],
    ['description', 'Описание', 'textarea'],
    ['team_size', 'Количество участников', 'input'],
    ['participants', 'ФИО участников', 'textarea'],
    ['departments', 'Структурные подразделения', 'input'],
    ['budget', 'Бюджет запуска', 'input'],
    ['groundwork', 'Задел (публикации, патенты и т.п.)', 'textarea'],
    ['groundwork_links', 'Ссылки задела (по одной на строку)', 'textarea']
  ];

  /* файлы карточки прикрепляются как вложения (multipart),
     а не названиями: [ключ поля карточки, подпись] */
  var DRAFT_FILE_FIELDS = [
    ['groundwork_files', 'Файлы задела (.pdf)'],
    ['presentation_files', 'Файлы презентации (.pdf)']
  ];

  /* разделы JSON-карточки гипотез (data/hypotheses/hyp-{id}.json) */
  var HYP_SECTIONS = {
    ontology: 'Онтология',
    rag: 'RAG и поиск',
    models: 'Модели',
    agents: 'Агенты',
    polygons: 'Виртуальный полигон',
    dashboards: 'Дашборды'
  };

  var HYP_OPEN_FIELDS = {
    metric: 'Метрика подтверждения',
    deadline: 'Срок проверки',
    decision: 'Решение по итогам'
  };

  /* Форма карточки, встроенная в тело (без поп-апа): card задан — правка
     существующей (поля предзаполнены, PATCH), иначе — новый проект (POST).
     Файлы к карточке только добавляются. */
  function projectFormHtml(card) {
    var html = '<form class="prj-edit-form cab-project-form" data-pid="' +
      (card && card.id ? esc(card.id) : '') + '">';
    DRAFT_FIELDS.forEach(function (f) {
      var value = card ? card[f[0]] : '';
      value = Array.isArray(value) ? value.join('\n') : String(value || '');
      html += '    <label><span>' + f[1] + '</span>' +
        (f[2] === 'textarea'
          ? '<textarea rows="3" name="' + f[0] + '">' + esc(value) + '</textarea>'
          : '<input type="text" name="' + f[0] + '" value="' + esc(value) + '">') +
        '</label>';
    });
    DRAFT_FILE_FIELDS.forEach(function (f) {
      html += '    <label><span>' + f[1] + '</span>' +
        '<input type="file" name="' + f[0] + '" multiple accept=".pdf,application/pdf"></label>';
    });
    html +=
      '    <label class="prj-edit-check"><input type="checkbox" name="presentation"' +
      (card && card.presentation ? ' checked' : '') + '> Нужна презентация проекта</label>' +
      '    <p class="login-msg cab-form-msg" hidden></p>' +
      '    <button class="btn-primary" type="submit">Сохранить</button>' +
      '  </form>';
    return html;
  }

  function showFormMsg(form, text, kind) {
    var el = form.querySelector('.cab-form-msg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'login-msg cab-form-msg' + (text ? ' login-msg--' + (kind || 'error') : '');
    el.hidden = !text;
  }

  function saveProjectForm(form) {
    var session = getSession();
    if (!session || !API || !API.projectsCreate) return;
    var projectId = form.getAttribute('data-pid') || '';
    var fields = {};
    DRAFT_FIELDS.forEach(function (f) {
      fields[f[0]] = form.elements[f[0]].value.trim();
    });
    fields.presentation = form.elements.presentation.checked;
    var btn = form.querySelector('[type="submit"]');
    if (btn) btn.disabled = true;
    var files = [];
    DRAFT_FILE_FIELDS.forEach(function (f) {
      Array.prototype.forEach.call(form.elements[f[0]].files || [], function (file) {
        files.push({ kind: f[0], file: file });
      });
    });
    /* projectId задан — правим существующую карточку (PATCH),
       иначе создаём новую (POST) */
    var saved = projectId
      ? API.projectsUpdate(session.token, projectId, fields)
      : API.projectsCreate(session.token, fields);
    saved
      .then(function (card) {
        var id = projectId || card.id;
        /* файлы прикрепляются к сохранённой карточке по одному */
        return files.reduce(function (chain, item) {
          return chain.then(function () {
            return API.projectsUploadFile(session.token, id, item.kind, item.file);
          });
        }, Promise.resolve());
      })
      .then(function () {
        loadResults();
      })
      .catch(function (err) {
        showFormMsg(form, 'Не удалось сохранить: ' + (err && err.message ? err.message : 'ошибка сети'));
        if (btn) btn.disabled = false;
      });
  }

  /* Пустая карточка-черновик в «Результаты работы — Проекты» — показывается
   всегда, даже когда свои проекты есть; первое сохранение (POST
   /api/projects) делает её полноценной карточкой-черновиком. */
  function draftCard() {
    var item = document.createElement('div');
    item.className = 'cab-project cab-project--draft';
    item.innerHTML =
      '<button class="cab-project-head" type="button">' +
        '<span class="cab-project-title">Черновик проекта</span>' +
        '<span class="cab-role cab-role--owner">руководитель</span>' +
        '<span class="prj-status prj-status--draft">черновик</span>' +
        '<span class="cab-sec-chev" aria-hidden="true"></span>' +
      '</button>' +
      '<div class="cab-project-body" hidden>' +
        '<p class="cab-empty">Пустая карточка: заполните поля и нажмите «Сохранить» — она станет карточкой проекта. ' +
        'Навык «Онбординг проекта» в рабочем чате также может собрать её за несколько вопросов.</p>' +
        projectFormHtml(null) +
      '</div>';
    return item;
  }

  function renderProjectGroup(wrap, items, byProject, emptyText) {
    if (!wrap) return;
    wrap.innerHTML = '';
    if (!items.length) {
      wrap.innerHTML = '<p class="cab-empty">' + (emptyText || 'Проектов пока нет.') + '</p>';
      wrap.hidden = false;
      return;
    }
    items.forEach(function (p) {
      wrap.appendChild(resultItem(p, byProject[p.id] || []));
    });
    wrap.hidden = false;
  }

  function renderResults(projects, ontologies, ragBases) {
    if (!resultsOwnerWrap) return;
    if (resultsMsg) resultsMsg.hidden = true;
    if (resultsCount) {
      resultsCount.textContent = projects.length
        ? projects.length + ' ' + plural(projects.length, 'проект', 'проекта', 'проектов')
        : '';
    }
    var byProject = {};
    (ontologies || []).forEach(function (o) {
      var pid = o.project_id || '';
      if (!pid) return;
      if (!byProject[pid]) byProject[pid] = [];
      byProject[pid].push(o);
    });
    var owned = projects.filter(function (p) { return p.role !== 'participant'; });
    var joined = projects.filter(function (p) { return p.role === 'participant'; });
    /* черновик-заготовка показывается всегда, даже когда свои проекты есть */
    renderProjectGroup(resultsOwnerWrap, owned, byProject);
    if (!owned.length) resultsOwnerWrap.appendChild(draftCard());
    renderProjectGroup(
      resultsParticipantWrap, joined, byProject,
      'Проектов с вашим участием пока нет — вы появитесь здесь, когда вас укажут среди участников карточки.'
    );
    /* системы знаний */
    if (resultsOntoWrap) {
      resultsOntoWrap.innerHTML = '';
      var ob = ontologies || [];
      if (!ob.length) {
        resultsOntoWrap.innerHTML = '<p class="cab-empty">Онтологий пока нет — соберите их навыком «Конструктор онтологий» в рабочем чате.</p>';
      } else {
        ob.forEach(function (o) { resultsOntoWrap.appendChild(ontologyCard(o)); });
      }
      resultsOntoWrap.hidden = false;
    }
    if (resultsRagWrap) {
      stopAllRagPolling();
      resultsRagWrap.innerHTML = '';
      var rb = ragBases || [];
      if (!rb.length) {
        resultsRagWrap.innerHTML = '<p class="cab-empty">RAG-систем пока нет.</p>';
      } else {
        rb.forEach(function (r) { resultsRagWrap.appendChild(ragCard(r)); });
      }
      resultsRagWrap.hidden = false;
    }
  }

  function loadResults() {
    var session = getSession();
    if (!session || !API || !API.cabinetListProjects) return;
    var fail = function (err) {
      if (err && err.status === 401) {
        onSessionLost('Сессия истекла — войдите заново.');
        return;
      }
      if (resultsMsg) {
        resultsMsg.textContent = 'Не удалось загрузить проекты: ' +
          (err && err.message ? err.message : 'ошибка сети');
        resultsMsg.hidden = false;
      }
    };
    API.cabinetListProjects(session.token)
      .then(function (data) {
        resultsLoaded = true;
        var projects = (data && data.items) || [];
        var ontosPromise = API.ontologiesList
          ? API.ontologiesList(session.token).catch(function () { return { items: [] }; })
          : Promise.resolve({ items: [] });
        var ragPromise = API.ragList
          ? API.ragList(session.token).catch(function () { return { items: [] }; })
          : Promise.resolve({ items: [] });
        Promise.all([ontosPromise, ragPromise]).then(function (res) {
          renderResults(projects, (res[0] && res[0].items) || [], (res[1] && res[1].items) || []);
        });
      })
      .catch(fail);
  }

  /* ---------- гипотезы проекта в карточке ---------- */

  function hypItem(hyp) {
    var html = '<div class="cab-hyp"><strong>' + esc(hyp.title || hyp.id || 'Гипотеза') + '</strong>';
    if (hyp.skipped) {
      html += ' <span class="prj-status prj-status--draft">пропущена</span>';
    }
    Object.keys(HYP_OPEN_FIELDS).forEach(function (key) {
      if (hyp[key]) html += infoRow(HYP_OPEN_FIELDS[key], esc(hyp[key]));
    });
    var extras = [];
    ['statement', 'as_is', 'verification', 'falsification', 'users', 'groundwork'].forEach(function (key) {
      var titles = {
        statement: 'Формулировка (если–то–проверим)',
        as_is: 'Как есть',
        verification: 'Способ проверки',
        falsification: 'Условия опровержения',
        users: 'Для кого',
        groundwork: 'Задел'
      };
      if (hyp[key]) extras.push([titles[key], esc(hyp[key])]);
    });
    if (extras.length) {
      html += '<details class="prj-hyp-details"><summary>Подробнее</summary>' +
        extras.map(function (pair) { return infoRow(pair[0], pair[1]); }).join('') + '</details>';
    }
    return html + '</div>';
  }

  function renderHyps(container, data) {
    var total = 0;
    var groups = '';
    Object.keys(HYP_SECTIONS).forEach(function (section) {
      var items = Array.isArray(data[section]) ? data[section] : [];
      total += items.length;
      if (!items.length) return;
      groups += '<div class="cab-project-sub">' + esc(HYP_SECTIONS[section]) + '</div>' +
        items.map(hypItem).join('');
    });
    container.innerHTML = total
      ? groups
      : '<p class="cab-empty">Гипотезы ещё не сформулированы — их готовит навык «Онбординг проекта» в рабочем чате.</p>';
  }

  function loadProjectHyps(container) {
    var session = getSession();
    var pid = container.getAttribute('data-hyp');
    if (!session || !pid || !API || !API.projectsHypotheses) {
      container.innerHTML = '<p class="cab-empty">Гипотезы ещё не сформулированы — их готовит навык «Онбординг проекта» в рабочем чате.</p>';
      return;
    }
    API.projectsHypotheses(session.token, pid)
      .then(function (data) { renderHyps(container, data || {}); })
      .catch(function () {
        /* 404 — карточки гипотез ещё нет */
        container.innerHTML = '<p class="cab-empty">Гипотезы ещё не сформулированы — их готовит навык «Онбординг проекта» в рабочем чате.</p>';
      });
  }

  /* скачивание файла карточки: ссылка без токена не работает,
     поэтому тянем файл fetch-ем с Bearer и отдаём blob-ом */
  function downloadProjectFile(token, ref, link) {
    fetch('/api/projects/' + ref, { headers: { 'Authorization': 'Bearer ' + token } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = decodeURIComponent(ref.split('/')[1]);
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      })
      .catch(function () {
        if (resultsMsg) {
          resultsMsg.textContent = 'Не удалось скачать файл.';
          resultsMsg.hidden = false;
        }
      });
  }

  if (resultsBody) {
    /* отправка инлайн-формы карточки (submit всплывает до resultsBody) */
    resultsBody.addEventListener('submit', function (e) {
      var form = e.target && e.target.closest ? e.target.closest('.cab-project-form') : null;
      if (!form) return;
      e.preventDefault();
      saveProjectForm(form);
    });
    resultsBody.addEventListener('click', function (e) {
      var fileLink = e.target && e.target.closest ? e.target.closest('[data-prjfile]') : null;
      if (fileLink) {
        e.preventDefault();
        var session = getSession();
        if (session) downloadProjectFile(session.token, fileLink.getAttribute('data-prjfile'), fileLink);
        return;
      }
      var head = e.target && e.target.closest ? e.target.closest('.cab-project-head') : null;
      if (!head) return;
      var body = head.parentElement ? head.parentElement.querySelector('.cab-project-body') : null;
      if (!body) return;
      var open = body.hidden;
      body.hidden = !open;
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
      head.classList.toggle('cab-project-head--open', open);
      /* RAG-база: форма загрузки/публикации догружается при первом раскрытии,
         опрос статусов документов останавливается при сворачивании */
      if (body.classList.contains('cab-rag-body')) {
        var ragItem = head.closest('[data-rag-id]');
        var ragId = ragItem ? ragItem.getAttribute('data-rag-id') : '';
        if (open) {
          if (!body.dataset.loaded) {
            body.dataset.loaded = '1';
            loadRagBody(body, ragId);
          }
        } else {
          stopRagPolling(ragId);
        }
      }
      if (body.classList.contains('cab-onto-body')) {
        var ontoItem = head.closest('[data-onto-id]');
        var ontoId = ontoItem ? ontoItem.getAttribute('data-onto-id') : '';
        if (open && !body.dataset.loaded) {
          body.dataset.loaded = '1';
          loadOntoBody(body, ontoId);
        }
      }
      /* гипотезы догружаются при первом раскрытии карточки */
      var hyps = body.querySelector('.cab-hyps');
      if (open && hyps && !hyps.dataset.loaded) {
        hyps.dataset.loaded = '1';
        loadProjectHyps(hyps);
      }
    });
  }

  bindSection('cabProfileHead', 'cabProfileBody', 'profile', null);
  bindSection('cabSessionsHead', 'cabSessionsBody', 'sessions', ensureSessions);
  bindSection('cabResultsHead', 'cabResultsBody', 'results', ensureResults);

  window.addEventListener('aios-auth-change', load);

  /* aios-launch-skill ловит workspace.js: сохраняет навык, открывает
     панель чата и сразу отправляет prompt — агент начинает диалог */
  function launchSkill(skillId, skillName, prompt) {
    document.dispatchEvent(new CustomEvent('aios-launch-skill', {
      detail: { skill_id: skillId, name: skillName, prompt: prompt }
    }));
  }

  var startBtn = document.getElementById('prjStartOnboarding');
  if (startBtn) {
    startBtn.addEventListener('click', function () {
      launchSkill('project-onboarding', 'Онбординг проекта',
        'Запусти онбординг проекта: познакомься со мной и задай вопросы, ' +
        'нужные для карточки проекта (название, цель, команда, подразделения, бюджет, задел).');
    });
  }

  /* те же навыки, что и кнопки-CTA на странице «Данные и знания» */
  var ontoBtn = document.getElementById('btnCabOntoCreate');
  if (ontoBtn) {
    ontoBtn.addEventListener('click', function () {
      launchSkill('ontology-engineer', 'Конструктор онтологий');
    });
  }
  var ragBtn = document.getElementById('btnCabRagCreate');
  if (ragBtn) {
    ragBtn.addEventListener('click', function () {
      launchSkill('rag-builder', 'Конструктор RAG');
    });
  }

  load();
})();