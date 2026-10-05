/* ============================================================
   Кабинет: прогресс этапов, «История сессий», «Результаты работы»
   и «Моя команда».

   Блок «Данные пользователя» переехал на вкладку «Настройки»
   (frontend/settings.html + js/settings.js); здесь остался только
   индикатор прогресса «Заполнить профиль» — заполненность читается
   из GET /api/cabinet/profile.

   Разделы «История сессий» и «Результаты работы» сворачиваются;
   состояние — в localStorage (aios-cabinet-collapse-v1).

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

  var guest = document.getElementById('cabGuest');
  var guestMsg = document.getElementById('cabGuestMsg');
  var loginBtn = document.getElementById('cabLoginBtn');
  var sessionsWrap = document.getElementById('cabSessions');
  var sessionsMsg = document.getElementById('cabSessionsMsg');
  var sessionsCount = document.getElementById('cabSessionsCount');
  var resultsBody = document.getElementById('cabResultsBody');
  var resultsOwnerWrap = document.getElementById('cabResultsOwner');
  var resultsParticipantWrap = document.getElementById('cabResultsParticipant');
  var resultsHypWrap = document.getElementById('cabResultsHyp');
  var resultsOntoWrap = document.getElementById('cabResultsOntologies');
  var resultsRagWrap = document.getElementById('cabResultsRag');
  var resultsSkillsWrap = document.getElementById('cabResultsSkills');
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
  if (!guest) return;

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

  /* ---------- прогресс-бар этапов ----------
     1. Профиль заполнен — все поля блока «Данные пользователя».
     2. Проект присвоен — есть проект (свой или с участием).
     3. Гипотезы сформулированы — для какого-то проекта есть карточка
        гипотез (data/hypotheses/hyp-{id}.json, навык «Гипотезы проекта»);
        признак hypotheses_done отдаёт GET /api/cabinet/projects.
     4. Система знаний сформирована — в каком-то проекте есть онтология.
     5–7. Агент / Модель / Полигон — пока неактивны. */

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
    { key: null, label: 'Агент создан', locked: true },
    { key: null, label: 'Модель обучена', locked: true },
    { key: null, label: 'Полигон создан', locked: true }
  ];

  var PROGRESS_HINTS = {
    profileDone: 'Шаг 1 из 4: заполните все поля «Настройки → Данные пользователя».',
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

  /* ---------- заполненность профиля (индикатор «Заполнить профиль») ----------

     Форма живёт на вкладке «Настройки» (settings.js); кабинет только
     читает GET /api/cabinet/profile и отмечает шаг прогресса. */

  function applyProfileToProgress(p) {
    progress.profileDone = profileComplete(p);
    renderProgress();
  }

  function renderState(authed, guestText) {
    guest.hidden = !!authed;
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
      [resultsOwnerWrap, resultsParticipantWrap, resultsOntoWrap, resultsRagWrap, resultsSkillsWrap].forEach(function (w) {
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
    /* привязка Telegram-бота переехала в «Настройки» (settings.js);
       старые deep-link из кнопки бота (cabinet.html#tg-link=КОД)
       переправляем туда с сохранением кода */
    if (/tg-link=/.test(location.hash || '')) {
      location.replace('settings.html' + location.hash);
      return;
    }
    /* заполненность профиля (шаг 1 прогресса) — профиль редактируется
       на вкладке «Настройки», здесь только читаем */
    API.cabinetGetProfile(session.token)
      .then(applyProfileToProgress)
      .catch(function () { /* шаг «Заполнить профиль» останется непройденным */ });
  }

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

  /* Логотип артефакта: платформа генерирует SVG по содержанию карточки
     в /data/icons/{id}.svg (artifact_icons). Приватные карточки не несут
     image_url, поэтому просто пробуем файл; отсутствует (генерация ещё
     идёт или не удалась) — onerror возвращает привычную заглушку. */
  function artifactLogo(id) {
    return (id && /^[a-z0-9][a-z0-9-]*$/i.test(id))
      ? '/data/icons/' + id + '.svg' : '';
  }

  function ontologyCard(o) {
    var status = o.status === 'active' ? 'в каталоге' : 'черновик';
    var classes = typeof o.classes_count === 'number' ? o.classes_count : (Array.isArray(o.classes) ? o.classes.length : 0);
    var relations = typeof o.relations_count === 'number' ? o.relations_count : (Array.isArray(o.relations) ? o.relations.length : 0);
    var meta = status + ' · ' + classes + ' ' + plural(classes, 'класс', 'класса', 'классов') +
      ' · ' + relations + ' ' + plural(relations, 'связь', 'связи', 'связей');
    if (o.updated_at) meta += ' · ' + fmtDate(o.updated_at);
    if (o.admin_only) meta += ' · тест';
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'model-card cab-project-card';
    var media = document.createElement('span');
    media.className = 'model-card-media';
    var img = document.createElement('img');
    img.src = o.image_url || artifactLogo(o.id) || 'assets/model-placeholder.svg';
    img.alt = o.name || '';
    img.loading = 'lazy';
    img.addEventListener('error', function () {
      if (img.src.indexOf('model-placeholder.svg') === -1) img.src = 'assets/model-placeholder.svg';
    });
    media.appendChild(img);
    var body = document.createElement('span');
    body.className = 'model-card-body';
    var name = document.createElement('span');
    name.className = 'model-card-name';
    name.textContent = o.name || 'Без названия';
    var sub = document.createElement('span');
    sub.className = 'model-card-sub';
    sub.textContent = meta;
    body.appendChild(name);
    body.appendChild(sub);
    card.appendChild(media);
    card.appendChild(body);
    card.setAttribute('data-onto-id', o.id || '');
    card.addEventListener('click', function () { openOntoModal(o); });
    return card;
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
    html += '<div class="cab-project-sub">Артефакты проекта</div>';
    if (ontos.length) {
      html += '<div class="cab-artifacts">' + ontos.map(ontologyCard).join('') + '</div>';
    } else {
      html += '<p class="cab-empty">Онтологий в этом проекте пока нет — соберите их навыком «Конструктор онтологий» в рабочем чате.</p>';
    }
    html += '<p class="cab-soon">Агенты, модели и полигоны по этому проекту появятся позже.</p>';
    return html;
  }

  function hypListItem(p) {
    var item = document.createElement('div');
    item.className = 'cab-project' + (p.status === 'draft' ? ' cab-project--draft' : '');
    item.setAttribute('data-id', p.id || '');
    var role = p.role === 'participant'
      ? '<span class="cab-role cab-role--participant">участник</span>'
      : '<span class="cab-role cab-role--owner">руководитель</span>';
    var status = p.status === 'draft' ? '<span class="prj-status prj-status--draft">черновик</span>' : '';
    /* тестовый проект (admin_only): пометка «тест» рядом со статусом */
    var testMark = p.admin_only ? '<span class="prj-status prj-status--test">тест</span>' : '';
    /* список проектов остаётся раскрывающимся, как раньше — только для
       выбора проекта; сами гипотезы — карточки внутри (renderHyps),
       клик по карточке гипотезы открывает модалку правки */
    item.innerHTML =
      '<button class="cab-project-head" type="button" aria-expanded="false">' +
        '<span class="cab-project-title">' + esc(p.title || 'Без названия') + '</span>' +
        role + status + testMark +
        '<span class="cab-sec-chev" aria-hidden="true"></span>' +
      '</button>' +
      '<div class="cab-project-body" hidden>' +
        '<div class="cab-hyps" data-hyp="' + esc(p.id || '') + '"' +
        (p.role !== 'participant' ? ' data-hyp-editable="1"' : '') + '>' +
        '<p class="cab-empty">загрузка…</p></div>' +
      '</div>';
    return item;
  }

  /* ---------- агенты (блок 3): черновики и опубликованные сценарии-агенты ----------
     Агент платформы — это текстовый сценарий (.md), который любой сотрудник
     запускает в чате. Путь агента: черновик в кабинете (виден только автору,
     проверяется кнопкой «Протестировать») → публикация человеком в каталог
     «Агенты» → при необходимости снятие. Создаёт агентов навык «Конструктор
     агентов» в рабочем чате. Сервер — /api/catalog/skills/drafts
     (routers/catalog.py, runtime/skill_drafts.py). */

  var AGENT_ICON_SVG = 'assets/icons/agent/default.svg';
  var SKILLS_URL = '/catalog/skills/drafts';
  var SKILL_CATEGORIES = [
    ['special', 'Специальный — привязан к предметной области'],
    ['general', 'Общий — универсальный помощник']
  ];
  var skillsCache = [];
  var skillModalOverlay = null;
  var skillState = null; /* { id, mode: 'view'|'edit'|'publish', text, msg, error, busy } */

  function skillApi(method, path, body) {
    var s = getSession();
    if (!API || !API.request || !s || !s.token) return Promise.reject(new Error('Нужен вход в AIOS'));
    return API.request(method, path, body, s.token);
  }

  function findSkill(id) {
    for (var i = 0; i < skillsCache.length; i++) {
      if (skillsCache[i].id === id) return skillsCache[i];
    }
    return null;
  }

  function skillStatusText(s) {
    if (s.status === 'published') {
      return s.dirty ? 'опубликован · есть неопубликованные правки' : 'опубликован в каталоге';
    }
    return 'черновик';
  }

  function skillCard(s) {
    var meta = skillStatusText(s);
    if (s.updated_at) meta += ' · ' + fmtDate(s.updated_at);
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'model-card cab-project-card';
    card.setAttribute('data-skill-id', s.id);
    var media = document.createElement('span');
    media.className = 'model-card-media';
    var img = document.createElement('img');
    img.src = AGENT_ICON_SVG;
    img.alt = s.name || '';
    img.loading = 'lazy';
    media.appendChild(img);
    var body = document.createElement('span');
    body.className = 'model-card-body';
    var name = document.createElement('span');
    name.className = 'model-card-name';
    name.textContent = s.name || 'Агент без названия';
    var sub = document.createElement('span');
    sub.className = 'model-card-sub';
    sub.textContent = meta;
    body.appendChild(name);
    body.appendChild(sub);
    card.appendChild(media);
    card.appendChild(body);
    card.addEventListener('click', function () { openSkillModal(s.id); });
    return card;
  }

  function renderSkillCards(skills) {
    skillsCache = skills || [];
    if (!resultsSkillsWrap) return;
    resultsSkillsWrap.classList.add('cab-project-grid');
    resultsSkillsWrap.innerHTML = '';
    if (!skillsCache.length) {
      resultsSkillsWrap.innerHTML = '<p class="cab-empty" style="grid-column:1/-1">Агентов пока нет — соберите первого навыком «Конструктор агентов» в рабочем чате: сначала черновик здесь, потом проверка в чате и публикация для всех.</p>';
    } else {
      skillsCache.forEach(function (s) { resultsSkillsWrap.appendChild(skillCard(s)); });
    }
    resultsSkillsWrap.hidden = false;
  }

  function refreshSkills() {
    return skillApi('GET', SKILLS_URL).then(function (d) {
      renderSkillCards((d && d.items) || []);
    }).catch(function () { /* блок не критичен: карточки остаются как были */ });
  }

  function skillErrorsHtml(text) {
    var items = String(text || '').split(/;\s+/).filter(Boolean);
    var style = ' style="color:#9a3b2f"';
    if (items.length < 2) return '<p class="mm-desc"' + style + '>' + esc(text) + '</p>';
    return '<ul class="mm-desc"' + style + '>' + items.map(function (i) { return '<li>' + esc(i) + '</li>'; }).join('') + '</ul>';
  }

  function skillButton(act, label, kind, disabled) {
    var cls = kind === 'primary' ? 'btn-primary ag-btn'
      : kind === 'danger' ? 'btn-primary ag-btn ag-btn--danger' : 'btn-primary ag-btn ag-btn--ghost';
    return '<button class="' + cls + '" type="button" data-act="' + act + '"' + (disabled ? ' disabled' : '') + '>' +
      esc(label) + '</button>';
  }

  function skillModalHtml(s, st) {
    var busy = !!st.busy;
    var html = '<p class="mm-desc">' + esc(s.description || '') + '</p>' +
      '<p class="mm-desc"><strong>Статус:</strong> ' + esc(skillStatusText(s)) + '</p>';
    if (s.source_agent_id) html += '<p class="mm-desc"><strong>Создан по агенту:</strong> ' + esc(s.source_agent_id) + '</p>';
    if (st.msg) html += '<p class="mm-desc" style="color:#3f6b3a">' + esc(st.msg) + '</p>';
    if (st.error) html += skillErrorsHtml(st.error);
    var row = function (inner) { return '<div class="mm-actions" style="display:flex;flex-wrap:wrap;gap:8px">' + inner + '</div>'; };
    if (st.mode === 'edit') {
      html += '<p class="mm-desc">Правьте текст агента. Платформа проверит его при сохранении: в чате не работают ' +
        'save_record, get_records, call_agent и notify; нельзя личные данные; нужны 3–6 фраз-триггеров и раздел «Шаги».</p>' +
        '<textarea id="cabSkillText" rows="18" spellcheck="false" ' +
        'style="width:100%;box-sizing:border-box;font-family:monospace;font-size:13px"></textarea>' +
        row(skillButton('save', busy ? 'Сохраняю…' : 'Сохранить', 'primary', busy) + skillButton('cancel', 'Отмена', 'ghost', busy));
    } else if (st.mode === 'publish') {
      html += '<p class="mm-desc"><strong>Категория в каталоге</strong></p>' +
        '<select id="cabSkillCategory">' + SKILL_CATEGORIES.map(function (c) {
          return '<option value="' + esc(c[0]) + '">' + esc(c[1]) + '</option>';
        }).join('') + '</select>' +
        '<p class="mm-desc">После публикации агента увидят все сотрудники на странице «Агенты» и смогут запустить его ' +
        'в чате. Перед публикацией платформа проверяет текст и покажет причину, если что-то не так.</p>' +
        row(skillButton('publish', busy ? 'Публикую…' : 'Опубликовать', 'primary', busy) + skillButton('cancel', 'Отмена', 'ghost', busy));
    } else {
      var published = s.status === 'published';
      var parts = skillButton('test-draft', 'Протестировать черновик в чате', 'primary', busy);
      if (published) parts += skillButton('test-public', 'Запустить опубликованный', 'ghost', busy);
      parts += skillButton('edit', 'Править текст', 'ghost', busy);
      if (!published || s.dirty) {
        parts += skillButton('publish-open', published ? 'Опубликовать изменения' : 'Опубликовать', 'primary', busy);
      }
      if (published) parts += skillButton('unpublish', 'Снять с публикации', 'ghost', busy);
      else parts += skillButton('delete', 'Удалить черновик', 'danger', busy);
      html += row(parts);
    }
    return html;
  }

  function renderSkillModal() {
    var s = skillState ? findSkill(skillState.id) : null;
    var body = document.getElementById('cabSkillModalBody');
    var title = document.getElementById('cabSkillModalTitle');
    if (!s || !body) return;
    if (title) title.textContent = s.name || 'Агент';
    body.innerHTML = skillModalHtml(s, skillState);
    var ta = document.getElementById('cabSkillText');
    if (ta && skillState.mode === 'edit') ta.value = skillState.text || '';
  }

  function closeSkillModal() {
    if (skillModalOverlay) {
      skillModalOverlay.hidden = true;
      document.body.classList.remove('modal-open');
    }
    skillState = null;
  }

  function skillDo(request, onDone, skipRefresh) {
    skillState.busy = true;
    skillState.error = '';
    skillState.msg = '';
    renderSkillModal();
    request().then(function (res) {
      skillState.busy = false;
      onDone(res);
      return skipRefresh ? null : refreshSkills();
    }).then(function () {
      if (skillState && skillModalOverlay && !skillModalOverlay.hidden) renderSkillModal();
    }).catch(function (err) {
      if (!skillState) return;
      skillState.busy = false;
      skillState.error = (err && err.message) || 'ошибка';
      renderSkillModal();
    });
  }

  function onSkillModalClick(e) {
    var btn = e && e.target && e.target.closest ? e.target.closest('[data-act]') : null;
    if (!btn || !skillState || skillState.busy) return;
    var s = findSkill(skillState.id);
    if (!s) return;
    var act = btn.getAttribute('data-act');
    var base = SKILLS_URL + '/' + encodeURIComponent(s.id);
    if (act === 'test-draft') {
      launchSkill('draft:' + s.id, (s.name || s.id) + ' (черновик)');
      closeSkillModal();
    } else if (act === 'test-public') {
      launchSkill(s.id, s.name || s.id);
      closeSkillModal();
    } else if (act === 'edit') {
      skillDo(function () { return skillApi('GET', base); },
        function (d) { skillState.mode = 'edit'; skillState.text = (d && d.text) || ''; }, true);
    } else if (act === 'cancel') {
      skillState.mode = 'view';
      skillState.error = '';
      renderSkillModal();
    } else if (act === 'save') {
      var ta = document.getElementById('cabSkillText');
      var text = ta ? ta.value : '';
      skillState.text = text;
      skillDo(function () { return skillApi('PUT', base, { text: text }); },
        function () { skillState.mode = 'view'; skillState.msg = 'Сохранено: платформа проверила текст.'; });
    } else if (act === 'publish-open') {
      skillState.mode = 'publish';
      skillState.error = '';
      renderSkillModal();
    } else if (act === 'publish') {
      var sel = document.getElementById('cabSkillCategory');
      var category = sel && sel.value ? sel.value : 'special';
      skillDo(function () { return skillApi('POST', base + '/publish', { category: category }); },
        function () { skillState.mode = 'view'; skillState.msg = 'Опубликовано: агент виден всем на странице «Агенты».'; });
    } else if (act === 'unpublish') {
      if (!window.confirm('Снять агента с публикации? Он пропадёт из каталога «Агенты»; черновик останется.')) return;
      skillDo(function () { return skillApi('POST', base + '/unpublish', {}); },
        function () { skillState.mode = 'view'; skillState.msg = 'Снято с публикации. Черновик сохранён.'; });
    } else if (act === 'delete') {
      if (!window.confirm('Удалить черновик агента? Он пропадёт из кабинета и API, но останется '
        + 'в закрытом архиве на сервере — администратор сможет вернуть его через терминал.')) return;
      skillDo(function () { return skillApi('DELETE', base); }, function () { closeSkillModal(); });
    }
  }

  function ensureSkillModal() {
    if (skillModalOverlay) return skillModalOverlay;
    skillModalOverlay = document.createElement('div');
    skillModalOverlay.className = 'modal-overlay';
    skillModalOverlay.hidden = true;
    skillModalOverlay.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="cabSkillModalTitle">' +
      '  <button class="modal-close" id="cabSkillModalClose" type="button" aria-label="Закрыть">✕</button>' +
      '  <h2 id="cabSkillModalTitle"></h2>' +
      '  <div id="cabSkillModalBody"></div>' +
      '</div>';
    document.body.appendChild(skillModalOverlay);
    skillModalOverlay.addEventListener('click', function (e) {
      if (e.target === skillModalOverlay) closeSkillModal();
    });
    var close = document.getElementById('cabSkillModalClose');
    if (close) close.addEventListener('click', closeSkillModal);
    var body = document.getElementById('cabSkillModalBody');
    if (body) body.addEventListener('click', onSkillModalClick);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && skillModalOverlay && !skillModalOverlay.hidden) closeSkillModal();
    });
    return skillModalOverlay;
  }

  function openSkillModal(id) {
    if (!findSkill(id)) return;
    ensureSkillModal();
    skillState = { id: id, mode: 'view', text: '', msg: '', error: '', busy: false };
    renderSkillModal();
    skillModalOverlay.hidden = false;
    document.body.classList.add('modal-open');
  }

  function ragCard(r) {
    var status = r.status === 'active' || r.status === 'published' ? 'в каталоге' : 'черновик';
    var docs = typeof r.documents_count === 'number' ? r.documents_count :
      (Array.isArray(r.documents) ? r.documents.length : 0);
    var meta = status + ' · ' + docs + ' ' + plural(docs, 'документ', 'документа', 'документов');
    if (r.updated_at) meta += ' · ' + fmtDate(r.updated_at);
    if (r.admin_only) meta += ' · тест';
    var ragId = r.rag_id || r.id || '';
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'model-card cab-project-card';
    var media = document.createElement('span');
    media.className = 'model-card-media';
    var img = document.createElement('img');
    img.src = r.image_url || artifactLogo(ragId) || 'assets/icons/rag/default.svg';
    img.alt = r.name || r.title || '';
    img.loading = 'lazy';
    img.addEventListener('error', function () {
      if (img.src.indexOf('rag/default.svg') === -1) img.src = 'assets/icons/rag/default.svg';
    });
    media.appendChild(img);
    var body = document.createElement('span');
    body.className = 'model-card-body';
    var name = document.createElement('span');
    name.className = 'model-card-name';
    name.textContent = r.name || r.title || 'Без названия';
    var sub = document.createElement('span');
    sub.className = 'model-card-sub';
    sub.textContent = meta;
    body.appendChild(name);
    body.appendChild(sub);
    card.appendChild(media);
    card.appendChild(body);
    card.setAttribute('data-rag-id', ragId);
    card.addEventListener('click', function () { openRagModal(r); });
    return card;
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
    items = items.filter(function (d) { return !(d.error && d.error.indexOf('Не добавлено') === 0); }).concat(container._skipped || []);
    var sess = container._session;
    API.ragRenderDocs(container, items, {
      token: sess && sess.token,
      ragId: container._ragId,
      refresh: function () { pollRagDocs(container._session, container._ragId, container); },
      onError: function (m) { window.alert(m); }
    });
  }

  function pollRagDocs(session, ragId, docsEl) {
    if (!API.ragListDocuments) return;
    docsEl._session = session; docsEl._ragId = ragId;
    API.ragListDocuments(session.token, ragId).then(function (data) {
      renderRagDocs(docsEl, (data && data.items) || []);
    }).catch(function () { /* сеть моргнула — подождём следующий тик */ });
  }

  function renderOntologyCheckResult(container, ragId, session, data) {
    if (!data || !data.has_ontology) { container.innerHTML = ''; return; }

    var parts = [];
    parts.push('<p class="mm-source">Онтология: ' + esc(data.ontology_name || '') + '</p>');
    if (data.classes_added && data.classes_added.length) {
      parts.push('<p class="mm-source">Добавлены классы: ' +
        esc(data.classes_added.map(function (c) { return c.name; }).join(', ')) + '</p>');
    }
    if (data.classes_removed && data.classes_removed.length) {
      parts.push('<p class="mm-source">Удалены классы: ' +
        esc(data.classes_removed.map(function (c) { return c.name; }).join(', ')) + '</p>');
    }
    if (data.classes_changed && data.classes_changed.length) {
      parts.push('<p class="mm-source">Изменены классы: ' +
        esc(data.classes_changed.map(function (c) { return c.name; }).join(', ')) + '</p>');
    }
    if (data.up_to_date) {
      parts.push('<p class="mm-source">Разметка актуальна — все ' + data.total_chunks +
        ' ' + plural(data.total_chunks, 'чанк', 'чанка', 'чанков') + ' соответствуют текущей онтологии.</p>');
    } else {
      parts.push('<p class="mm-source">Изменится: ' + data.will_change + ' из ' + data.total_chunks +
        ' ' + plural(data.total_chunks, 'чанка', 'чанков', 'чанков') +
        ' (без изменений останется ' + data.unchanged + ').</p>');
      parts.push('<button type="button" class="btn-primary rag-reclassify-btn">Актуализировать RAG-базу</button>');
    }
    parts.push('<span class="mm-source rag-reclassify-msg"></span>');
    container.innerHTML = parts.join('');

    var reclassifyBtn = container.querySelector('.rag-reclassify-btn');
    var msg = container.querySelector('.rag-reclassify-msg');
    if (reclassifyBtn) {
      reclassifyBtn.addEventListener('click', function () {
        if (!API.ragReclassify) return;
        reclassifyBtn.disabled = true;
        msg.textContent = 'Актуализация…';
        API.ragReclassify(session.token, ragId).then(function (result) {
          msg.textContent = 'Готово: обновлено ' + result.updated + ' из ' + result.total +
            ' (не тронуто ' + result.unchanged + ').';
          return API.ragOntologyCheck(session.token, ragId).then(function (fresh) {
            renderOntologyCheckResult(container, ragId, session, fresh);
          });
        }).catch(function (err) {
          msg.textContent = 'Ошибка: ' + (err && err.message ? err.message : 'сеть');
          reclassifyBtn.disabled = false;
        });
      });
    }
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
        (card.ontology_id ?
          '<div class="rag-ontology-sync">' +
            '<button type="button" class="btn-primary prj-btn-ghost rag-check-btn">Проверить сходство с онтологией</button>' +
            '<div class="rag-check-result"></div>' +
          '</div>'
        : '') +
        '<div class="rag-upload">' +
          '<label for="ragFile-' + esc(ragId) + '" class="btn-primary">Загрузить документы</label>' +
          '<input type="file" id="ragFile-' + esc(ragId) + '" accept=".pdf,.txt,.md,.docx,.zip" multiple hidden>' +
          '<span class="mm-source rag-upload-status">До 10 МБ на файл (.pdf, .txt, .md, .docx), архив .zip до 100 МБ; можно выбрать сразу несколько</span>' +
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

      var checkBtn = body.querySelector('.rag-check-btn');
      var checkResult = body.querySelector('.rag-check-result');
      if (checkBtn && checkResult) {
        checkBtn.addEventListener('click', function () {
          if (!API.ragOntologyCheck) return;
          checkBtn.disabled = true;
          checkResult.innerHTML = '<p class="mm-source">Проверка…</p>';
          API.ragOntologyCheck(session.token, ragId).then(function (data) {
            checkBtn.disabled = false;
            renderOntologyCheckResult(checkResult, ragId, session, data);
          }).catch(function (err) {
            checkBtn.disabled = false;
            checkResult.innerHTML = '<p class="mm-source">Ошибка проверки: ' +
              esc(err && err.message ? err.message : 'сеть') + '</p>';
          });
        });
      }

      fileInput.addEventListener('change', function () {
        var files = fileInput.files ? Array.prototype.slice.call(fileInput.files) : [];
        if (!files.length || !API.ragUploadDocument) return;
        function isZip(f) { return /\.zip$/i.test(f.name); }
        function limitMb(f) { return isZip(f) ? 100 : 10; }
        var toUpload = files.filter(function (f) { return f.size <= limitMb(f) * 1024 * 1024; });
        var tooBig = files.filter(function (f) { return f.size > limitMb(f) * 1024 * 1024; });
        var archSkipped = [];
        function bigText() {
          if (tooBig.length === 1) {
            return 'Файл «' + tooBig[0].name + '» не добавлен: превышает лимит ' + limitMb(tooBig[0]) + ' МБ';
          }
          return 'Не добавлены, так как превышают лимит размера: ' + tooBig.map(function (f) {
            return '«' + f.name + '» (лимит ' + limitMb(f) + ' МБ)';
          }).join(', ');
        }
        function archText() {
          var shown = archSkipped.slice(0, 5).map(function (x) { return '«' + x.name + '» — ' + x.reason; }).join(', ');
          var more = archSkipped.length > 5 ? ' и ещё ' + (archSkipped.length - 5) : '';
          return 'Из архива не добавлено ' + archSkipped.length + ': ' + shown + more;
        }
        function addLocal() {
          var arr = docsEl._skipped || [];
          tooBig.forEach(function (f) {
            arr.push({ filename: f.name, status: 'error', error: 'Не добавлено: превышен лимит ' + limitMb(f) + ' МБ' });
          });
          archSkipped.forEach(function (x) {
            var i = x.name.lastIndexOf('/');
            arr.push({ filename: x.name.slice(i + 1), folder: i > 0 ? x.name.slice(0, i) : '', status: 'error', error: 'Не добавлено: ' + x.reason });
          });
          docsEl._skipped = arr;
        }
        if (!toUpload.length) {
          addLocal();
          pollRagDocs(session, ragId, docsEl);
          uploadStatus.textContent = bigText();
          fileInput.value = '';
          return;
        }
        function uploadNext(i, okCount, errCount) {
          if (i >= toUpload.length) {
            var parts = [];
            if (okCount) parts.push(okCount + ' файл(ов) принято, идёт обработка');
            if (errCount) parts.push(errCount + ' с ошибкой загрузки');
            if (tooBig.length) parts.push(bigText());
            if (archSkipped.length) parts.push(archText());
            uploadStatus.textContent = parts.join('. ') || 'Готово';
            addLocal();
            pollRagDocs(session, ragId, docsEl);
            fileInput.value = '';
            return;
          }
          var file = toUpload[i];
          uploadStatus.textContent = 'Загрузка «' + file.name + '» (' + (i + 1) + ' из ' + toUpload.length + ')…';
          API.ragUploadDocument(session.token, ragId, file).then(function (res) {
            var isArch = res && res.archive;
            if (isArch && res.skipped) res.skipped.forEach(function (x) { archSkipped.push(x); });
            uploadNext(i + 1, okCount + (isArch ? (res.queued || 0) : 1), errCount);
          }).catch(function () {
            uploadNext(i + 1, okCount, errCount + 1);
          });
        }
        uploadNext(0, 0, 0);
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
    agents: 'Агенты',
    models: 'Модели',
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
        closeProjectModal();
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
  /* ---------- карточки проектов (блок 1): SVG-иконка + модалка ---------- */

  var PLACEHOLDER_ICON_SVG = '<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">' +
    '<circle cx="32" cy="32" r="5" fill="#C2A054"/>' +
    '<circle cx="14" cy="18" r="3" fill="#7C8B6C"/>' +
    '<circle cx="50" cy="18" r="3" fill="#7C8B6C"/>' +
    '<circle cx="14" cy="46" r="3" fill="#7C8B6C"/>' +
    '<circle cx="50" cy="46" r="3" fill="#7C8B6C"/>' +
    '<line x1="32" y1="32" x2="14" y2="18" stroke="#C2A054" stroke-width="1.5"/>' +
    '<line x1="32" y1="32" x2="50" y2="18" stroke="#C2A054" stroke-width="1.5"/>' +
    '<line x1="32" y1="32" x2="14" y2="46" stroke="#C2A054" stroke-width="1.5"/>' +
    '<line x1="32" y1="32" x2="50" y2="46" stroke="#C2A054" stroke-width="1.5"/>' +
    '</svg>';

  var iconRequested = {};

  /* иконка генерируется один раз ИИ по описанию проекта (POST
     /api/projects/{id}/icon, идемпотентно на сервере) — запрашиваем лениво
     при первом рендере карточки без сохранённой icon_svg */
  function loadProjectIcon(p, mediaEl) {
    if (p.icon_svg || iconRequested[p.id] || !API || !API.projectsGenerateIcon) return;
    iconRequested[p.id] = true;
    var session = getSession();
    if (!session) return;
    API.projectsGenerateIcon(session.token, p.id).then(function (res) {
      var svg = res && res.icon_svg;
      if (svg && mediaEl && mediaEl.isConnected) {
        mediaEl.innerHTML = svg;
        mediaEl.classList.remove('model-card-media--placeholder');
      }
    }).catch(function () { /* тихо — карточка остаётся с плейсхолдером */ });
  }

  function projectCard(p, ontos) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'model-card cab-project-card';
    var media = document.createElement('span');
    media.className = 'model-card-media model-card-media--svg' + (p.icon_svg ? '' : ' model-card-media--placeholder');
    media.innerHTML = p.icon_svg || PLACEHOLDER_ICON_SVG;
    var body = document.createElement('span');
    body.className = 'model-card-body';
    var name = document.createElement('span');
    name.className = 'model-card-name';
    name.textContent = p.title || 'Без названия';
    var sub = document.createElement('span');
    sub.className = 'model-card-sub';
    var subBits = [p.role === 'participant' ? 'участник' : 'руководитель'];
    if (p.status === 'draft') subBits.push('черновик');
    if (p.admin_only) subBits.push('тест');
    sub.textContent = subBits.join(' · ');
    body.appendChild(name);
    body.appendChild(sub);
    card.appendChild(media);
    card.appendChild(body);
    card.addEventListener('click', function () { openProjectModal(p, ontos); });
    if (!p.icon_svg) loadProjectIcon(p, media);
    return card;
  }

  /* Пустая карточка-черновик в «Результаты работы — Проекты» — показывается
     всегда, даже когда свои проекты есть; открывает ту же модалку с пустой
     формой, первое сохранение (POST /api/projects) делает её полноценной. */
  function draftProjectCard() {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'model-card cab-project-card cab-project-card--draft';
    var media = document.createElement('span');
    media.className = 'model-card-media model-card-media--svg model-card-media--placeholder';
    media.innerHTML = PLACEHOLDER_ICON_SVG;
    var body = document.createElement('span');
    body.className = 'model-card-body';
    var name = document.createElement('span');
    name.className = 'model-card-name';
    name.textContent = 'Черновик проекта';
    var sub = document.createElement('span');
    sub.className = 'model-card-sub';
    sub.textContent = 'руководитель · черновик';
    body.appendChild(name);
    body.appendChild(sub);
    card.appendChild(media);
    card.appendChild(body);
    card.addEventListener('click', function () { openProjectModal(null, []); });
    return card;
  }

  var projectModalOverlay = null;

  function ensureProjectModal() {
    if (projectModalOverlay) return projectModalOverlay;
    projectModalOverlay = document.createElement('div');
    projectModalOverlay.className = 'modal-overlay';
    projectModalOverlay.hidden = true;
    projectModalOverlay.innerHTML =
      '<div class="modal modal--project" role="dialog" aria-modal="true" aria-labelledby="cabPrjModalTitle">' +
      '  <button class="modal-close" id="cabPrjModalClose" type="button" aria-label="Закрыть">✕</button>' +
      '  <h2 id="cabPrjModalTitle"></h2>' +
      '  <div id="cabPrjModalBody"></div>' +
      '</div>';
    document.body.appendChild(projectModalOverlay);
    projectModalOverlay.addEventListener('click', function (e) {
      if (e.target === projectModalOverlay) closeProjectModal();
    });
    var close = document.getElementById('cabPrjModalClose');
    if (close) close.addEventListener('click', closeProjectModal);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && projectModalOverlay && !projectModalOverlay.hidden) closeProjectModal();
    });
    /* форма правки/создания (projectFormHtml) отрисовывается внутри модалки —
       submit и скачивание файлов карточки навешиваем здесь же (делегирование) */
    projectModalOverlay.addEventListener('submit', function (e) {
      var form = e.target && e.target.closest ? e.target.closest('.cab-project-form') : null;
      if (!form) return;
      e.preventDefault();
      saveProjectForm(form);
    });
    projectModalOverlay.addEventListener('click', function (e) {
      var fileLink = e.target && e.target.closest ? e.target.closest('[data-prjfile]') : null;
      if (!fileLink) return;
      e.preventDefault();
      var session = getSession();
      if (session) downloadProjectFile(session.token, fileLink.getAttribute('data-prjfile'), fileLink);
    });
    return projectModalOverlay;
  }

  function closeProjectModal() {
    if (projectModalOverlay) {
      projectModalOverlay.hidden = true;
      document.body.classList.remove('modal-open');
    }
  }

  /* p === null — создание нового проекта (пустая форма); иначе — просмотр
     (projectDetails) плюс форма правки (projectFormHtml), если владелец. */
  function openProjectModal(p, ontos) {
    ensureProjectModal();
    var title = document.getElementById('cabPrjModalTitle');
    var body = document.getElementById('cabPrjModalBody');
    if (!p) {
      title.textContent = 'Новый проект';
      body.innerHTML = projectFormHtml(null);
    } else {
      title.textContent = p.title || 'Карточка проекта';
      var edit = p.role !== 'participant' ? projectFormHtml(p) : '';
      body.innerHTML = projectDetails(p, ontos || []) + edit;
    }
    projectModalOverlay.hidden = false;
    document.body.classList.add('modal-open');
  }

  var ontoModalOverlay = null;

  function ensureOntoModal() {
    if (ontoModalOverlay) return ontoModalOverlay;
    ontoModalOverlay = document.createElement('div');
    ontoModalOverlay.className = 'modal-overlay';
    ontoModalOverlay.hidden = true;
    ontoModalOverlay.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="cabOntoModalTitle">' +
      '  <button class="modal-close" id="cabOntoModalClose" type="button" aria-label="Закрыть">✕</button>' +
      '  <h2 id="cabOntoModalTitle"></h2>' +
      '  <div id="cabOntoModalBody"></div>' +
      '</div>';
    document.body.appendChild(ontoModalOverlay);
    ontoModalOverlay.addEventListener('click', function (e) {
      if (e.target === ontoModalOverlay) closeOntoModal();
    });
    var close = document.getElementById('cabOntoModalClose');
    if (close) close.addEventListener('click', closeOntoModal);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && ontoModalOverlay && !ontoModalOverlay.hidden) closeOntoModal();
    });
    return ontoModalOverlay;
  }

  function closeOntoModal() {
    if (ontoModalOverlay) {
      ontoModalOverlay.hidden = true;
      document.body.classList.remove('modal-open');
    }
  }

  /* карточка онтологии открывает модалку — тело догружается той же
     loadOntoBody(), что раньше рисовала содержимое раскрывающегося блока */
  function openOntoModal(o) {
    ensureOntoModal();
    document.getElementById('cabOntoModalTitle').textContent = o.name || 'Онтология';
    var body = document.getElementById('cabOntoModalBody');
    loadOntoBody(body, o.id || '');
    ontoModalOverlay.hidden = false;
    document.body.classList.add('modal-open');
  }

  var ragModalOverlay = null;
  var ragModalOpenId = null;

  function ensureRagModal() {
    if (ragModalOverlay) return ragModalOverlay;
    ragModalOverlay = document.createElement('div');
    ragModalOverlay.className = 'modal-overlay';
    ragModalOverlay.hidden = true;
    ragModalOverlay.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="cabRagModalTitle">' +
      '  <button class="modal-close" id="cabRagModalClose" type="button" aria-label="Закрыть">✕</button>' +
      '  <h2 id="cabRagModalTitle"></h2>' +
      '  <div id="cabRagModalBody"></div>' +
      '</div>';
    document.body.appendChild(ragModalOverlay);
    ragModalOverlay.addEventListener('click', function (e) {
      if (e.target === ragModalOverlay) closeRagModal();
    });
    var close = document.getElementById('cabRagModalClose');
    if (close) close.addEventListener('click', closeRagModal);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && ragModalOverlay && !ragModalOverlay.hidden) closeRagModal();
    });
    return ragModalOverlay;
  }

  /* закрытие модалки RAG-базы останавливает опрос статусов документов —
     тот же принцип, что раньше срабатывал при сворачивании блока */
  function closeRagModal() {
    if (ragModalOverlay) {
      ragModalOverlay.hidden = true;
      document.body.classList.remove('modal-open');
    }
    if (ragModalOpenId) {
      stopRagPolling(ragModalOpenId);
      ragModalOpenId = null;
    }
  }

  /* карточка RAG-базы открывает модалку — тело догружается той же
     loadRagBody(), что раньше рисовала форму загрузки/публикации внутри
     раскрывающегося блока (загрузка файла, список документов, публикация) */
  function openRagModal(r) {
    ensureRagModal();
    var ragId = r.rag_id || r.id || '';
    document.getElementById('cabRagModalTitle').textContent = r.name || r.title || 'RAG-база';
    var body = document.getElementById('cabRagModalBody');
    ragModalOpenId = ragId;
    loadRagBody(body, ragId);
    ragModalOverlay.hidden = false;
    document.body.classList.add('modal-open');
  }

  function renderProjectGroup(wrap, items, byProject, emptyText) {
    if (!wrap) return;
    wrap.classList.add('cab-project-grid');
    wrap.innerHTML = '';
    if (!items.length) {
      wrap.innerHTML = '<p class="cab-empty" style="grid-column:1/-1">' + (emptyText || 'Проектов пока нет.') + '</p>';
      wrap.hidden = false;
      return;
    }
    items.forEach(function (p) {
      wrap.appendChild(projectCard(p, byProject[p.id] || []));
    });
    wrap.hidden = false;
  }

  function renderResults(projects, ontologies, ragBases, skills) {
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
    if (!owned.length) resultsOwnerWrap.appendChild(draftProjectCard());
    renderProjectGroup(
      resultsParticipantWrap, joined, byProject,
      'Проектов с вашим участием пока нет — вы появитесь здесь, когда вас укажут среди участников карточки.'
    );
    /* гипотезы (блок 2): список проектов остаётся раскрывающимся, как
       раньше — внутри, при раскрытии, карточки гипотез (hypListItem +
       renderHyps), правка — через отдельную модалку */
    if (resultsHypWrap) {
      resultsHypWrap.innerHTML = '';
      if (!projects.length) {
        resultsHypWrap.innerHTML = '<p class="cab-empty">Гипотезы появятся здесь, когда навык «Онбординг проекта» в рабочем чате соберёт карточку проекта.</p>';
      } else {
        projects.forEach(function (p) {
          resultsHypWrap.appendChild(hypListItem(p));
        });
      }
      resultsHypWrap.hidden = false;
    }
    /* агенты (блок 3): черновики и опубликованные сценарии-агенты автора */
    renderSkillCards(skills);

    /* системы знаний */
    if (resultsOntoWrap) {
      resultsOntoWrap.classList.add('cab-project-grid');
      resultsOntoWrap.innerHTML = '';
      var ob = ontologies || [];
      if (!ob.length) {
        resultsOntoWrap.innerHTML = '<p class="cab-empty" style="grid-column:1/-1">Онтологий пока нет — соберите их навыком «Конструктор онтологий» в рабочем чате.</p>';
      } else {
        ob.forEach(function (o) { resultsOntoWrap.appendChild(ontologyCard(o)); });
      }
      resultsOntoWrap.hidden = false;
    }
    if (resultsRagWrap) {
      stopAllRagPolling();
      resultsRagWrap.classList.add('cab-project-grid');
      resultsRagWrap.innerHTML = '';
      var rb = ragBases || [];
      if (!rb.length) {
        resultsRagWrap.innerHTML = '<p class="cab-empty" style="grid-column:1/-1">RAG-систем пока нет.</p>';
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
        var skillsPromise = skillApi('GET', SKILLS_URL).catch(function () { return { items: [] }; });
        Promise.all([ontosPromise, ragPromise, skillsPromise]).then(function (res) {
          renderResults(projects, (res[0] && res[0].items) || [], (res[1] && res[1].items) || [],
            (res[2] && res[2].items) || []);
        });
      })
      .catch(fail);
  }

  /* ---------- гипотезы проекта в карточке ---------- */

  function hypItem(hyp, editCtx) {
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
    /* правка в кабинете — только title/statement; остальные поля (as_is,
       verification, falsification, users, groundwork, skipped, metric,
       deadline, decision) формирует только навык «Гипотезы проекта» */
    if (editCtx) {
      html += '<form class="prj-edit-form cab-hyp-edit" data-pid="' + esc(editCtx.pid) +
        '" data-section="' + esc(editCtx.section) + '" data-index="' + editCtx.index + '">' +
        '<label><span>Название гипотезы</span>' +
        '<input type="text" name="title" maxlength="200" value="' + esc(hyp.title || '') + '"></label>' +
        '<label><span>Формулировка (если–то–проверим)</span>' +
        '<textarea rows="4" name="statement">' + esc(hyp.statement || '') + '</textarea></label>' +
        '<p class="login-msg cab-form-msg" hidden></p>' +
        '<button class="btn-primary" type="submit">Сохранить</button>' +
        '</form>';
    }
    return html + '</div>';
  }

  /* ---------- модалка правки одной гипотезы (блок 2) ---------- */

  var HYP_TEASER_LEN = 140;
  var hypModalOverlay = null;
  var hypModalCtx = null; /* { container } — куда вернуться после сохранения */

  function ensureHypModal() {
    if (hypModalOverlay) return hypModalOverlay;
    hypModalOverlay = document.createElement('div');
    hypModalOverlay.className = 'modal-overlay';
    hypModalOverlay.hidden = true;
    hypModalOverlay.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="cabHypModalTitle">' +
      '  <button class="modal-close" id="cabHypModalClose" type="button" aria-label="Закрыть">✕</button>' +
      '  <h2 id="cabHypModalTitle"></h2>' +
      '  <div id="cabHypModalBody"></div>' +
      '</div>';
    document.body.appendChild(hypModalOverlay);
    hypModalOverlay.addEventListener('click', function (e) {
      if (e.target === hypModalOverlay) closeHypModal();
    });
    var close = document.getElementById('cabHypModalClose');
    if (close) close.addEventListener('click', closeHypModal);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && hypModalOverlay && !hypModalOverlay.hidden) closeHypModal();
    });
    hypModalOverlay.addEventListener('submit', function (e) {
      var form = e.target && e.target.closest ? e.target.closest('.cab-hyp-edit') : null;
      if (!form) return;
      e.preventDefault();
      saveHypForm(form);
    });
    return hypModalOverlay;
  }

  function closeHypModal() {
    if (hypModalOverlay) {
      hypModalOverlay.hidden = true;
      document.body.classList.remove('modal-open');
    }
    hypModalCtx = null;
  }

  function openHypModal(container, pid, section, index, hyp, editable) {
    ensureHypModal();
    hypModalCtx = { container: container };
    var title = document.getElementById('cabHypModalTitle');
    var body = document.getElementById('cabHypModalBody');
    title.textContent = hyp.title || hyp.id || 'Гипотеза';
    body.innerHTML = hypItem(hyp, editable ? { pid: pid, section: section, index: index } : null);
    hypModalOverlay.hidden = false;
    document.body.classList.add('modal-open');
  }

  function saveHypForm(form) {
    var session = getSession();
    var btn = form.querySelector('button[type="submit"]');
    if (!session || !API.projectsHypothesisUpdate || !hypModalCtx) return;
    btn.disabled = true;
    showFormMsg(form, '', '');
    API.projectsHypothesisUpdate(session.token, form.getAttribute('data-pid'), {
      section: form.getAttribute('data-section'),
      index: parseInt(form.getAttribute('data-index'), 10),
      title: form.elements.title.value,
      statement: form.elements.statement.value
    }).then(function () {
      var container = hypModalCtx && hypModalCtx.container;
      closeHypModal();
      if (container) loadProjectHyps(container);
    }).catch(function (err) {
      btn.disabled = false;
      showFormMsg(form, (err && err.message) || 'Не удалось сохранить, попробуйте ещё раз.', 'error');
    });
  }

  function hypCard(container, pid, section, index, hyp, editable) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'cab-hyp-card';
    var title = document.createElement('span');
    title.className = 'cab-hyp-card-title';
    title.textContent = hyp.title || hyp.id || 'Гипотеза';
    card.appendChild(title);
    if (hyp.skipped) {
      var skip = document.createElement('span');
      skip.className = 'prj-status prj-status--draft';
      skip.textContent = 'пропущена';
      card.appendChild(skip);
    }
    var teaserText = String(hyp.statement || '').trim();
    if (teaserText) {
      if (teaserText.length > HYP_TEASER_LEN) teaserText = teaserText.slice(0, HYP_TEASER_LEN) + '…';
      var teaser = document.createElement('span');
      teaser.className = 'cab-hyp-card-teaser';
      teaser.textContent = teaserText;
      card.appendChild(teaser);
    }
    card.addEventListener('click', function () {
      openHypModal(container, pid, section, index, hyp, editable);
    });
    return card;
  }

  /* карточка на каждую гипотезу (не раздел): плоский список по всем
     разделам HYP_SECTIONS, клик по карточке открывает модалку правки —
     по аналогии с карточками моделей/онтологий (вместо вкладок) */
  function renderHyps(container, data, editable) {
    var pid = container.getAttribute('data-hyp') || '';
    var items = [];
    Object.keys(HYP_SECTIONS).forEach(function (section) {
      (Array.isArray(data[section]) ? data[section] : []).forEach(function (hyp, idx) {
        items.push({ section: section, index: idx, hyp: hyp });
      });
    });
    if (!items.length) {
      container.innerHTML = '<p class="cab-empty">Гипотезы ещё не сформулированы — их готовит навык «Онбординг проекта» в рабочем чате.</p>';
      return;
    }
    container.innerHTML = '';
    var grid = document.createElement('div');
    grid.className = 'cab-hyp-cards';
    items.forEach(function (it) {
      grid.appendChild(hypCard(container, pid, it.section, it.index, it.hyp, editable));
    });
    container.appendChild(grid);
  }

  function loadProjectHyps(container) {
    var session = getSession();
    var pid = container.getAttribute('data-hyp');
    var editable = container.getAttribute('data-hyp-editable') === '1';
    if (!session || !pid || !API || !API.projectsHypotheses) {
      container.innerHTML = '<p class="cab-empty">Гипотезы ещё не сформулированы — их готовит навык «Онбординг проекта» в рабочем чате.</p>';
      return;
    }
    API.projectsHypotheses(session.token, pid)
      .then(function (data) { renderHyps(container, data || {}, editable); })
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

  var agentBtn = document.getElementById('btnCabAgentCreate');
  if (agentBtn) {
    agentBtn.addEventListener('click', function () {
      launchSkill('agent-builder', 'Конструктор агентов');
    });
  }

  load();
})();
