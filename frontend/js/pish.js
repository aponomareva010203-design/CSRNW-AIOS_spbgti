/* ============================================================
   Страница «Передовая инженерная школа» (pish.html).

   Доступ: пока только роль «Администратор». Гостям и обычным
   пользователям показывается экран «В разработке» (пункт меню при
   этом тоже приглушён с припиской — переключает auth.js).

   Роль берётся из сессии в localStorage (ключ aios-auth-v1), как в
   js/pilot.js. Переключение живое: auth.js после входа/выхода
   dispatch-ит событие 'aios-auth-change'.

   Кластеры раскрываются спойлерами (разметка в pish.html). В каждый
   спойлер подгружаются карточки проектов кластера из GET /api/projects —
   какие проекты в какой кластер входят, задано статически атрибутом
   data-projects у блока .pish-projects.
   ============================================================ */

(function () {
  var content = document.getElementById('pishContent');
  var locked = document.getElementById('pishLocked');
  if (!content || !locked) return;

  var AUTH_KEY = 'aios-auth-v1';

  function getSession() {
    var s = null;
    try { s = JSON.parse(localStorage.getItem(AUTH_KEY) || 'null'); }
    catch (e) { s = null; }
    return s && s.token ? s : null;
  }

  function isAdmin() {
    var s = getSession();
    return !!(s && s.role === 'Администратор');
  }

  function render() {
    var admin = isAdmin();
    content.hidden = !admin;
    locked.hidden = admin;
    if (admin) loadClusterProjects();
  }

  /* --- карточки проектов кластеров (раздел «Проекты») ---

     В каждом спойлере кластера стоит пустой <div class="pish-projects"
     data-projects="id …">. Список проектов приходит из GET /api/projects
     (как на странице «Проекты»); карточки, чьи id перечислены в
     data-projects, отрисовываются ссылками projects.html?id=… —
     глубокая ссылка открывает карточку проекта в разделе. */

  function makeProjectCard(p) {
    var card = document.createElement('a');
    card.className = 'prj-card';
    card.href = 'projects.html?id=' + encodeURIComponent(p.id);
    var head = document.createElement('span');
    head.className = 'prj-card-head';
    var title = document.createElement('span');
    title.className = 'prj-card-title';
    title.textContent = p.title || 'Без названия';
    var status = document.createElement('span');
    status.className = 'prj-status prj-status--' + (p.status === 'active' ? 'active' : 'draft');
    status.textContent = p.status === 'active' ? 'активен' : 'черновик';
    head.appendChild(title);
    head.appendChild(status);
    card.appendChild(head);
    if (p.owner) {
      var sub = document.createElement('span');
      sub.className = 'prj-card-sub';
      sub.textContent = 'руководитель: ' + p.owner;
      card.appendChild(sub);
    }
    if (p.goal) {
      var desc = document.createElement('span');
      desc.className = 'prj-card-desc';
      desc.textContent = p.goal.length > 120 ? p.goal.slice(0, 120) + '…' : p.goal;
      card.appendChild(desc);
    }
    return card;
  }

  function renderClusterProjects(items) {
    var byId = {};
    items.forEach(function (p) { byId[p.id] = p; });
    document.querySelectorAll('.pish-projects').forEach(function (box) {
      box.innerHTML = '';
      /* карточки, которых пользователь не видит (черновик чужой команды,
         удалённые), просто пропускаются */
      var ids = (box.getAttribute('data-projects') || '')
        .split(/[\s,]+/).filter(Boolean);
      var cards = ids.map(function (id) { return byId[id]; }).filter(Boolean);
      if (!cards.length) { box.hidden = true; return; }
      var label = document.createElement('div');
      label.className = 'pish-projects-label';
      var labelText = document.createElement('span');
      labelText.textContent = 'Проекты кластера';
      var labelLink = document.createElement('a');
      labelLink.href = 'projects.html';
      labelLink.textContent = 'Все проекты →';
      label.appendChild(labelText);
      label.appendChild(labelLink);
      var grid = document.createElement('div');
      grid.className = 'pish-projects-grid';
      cards.forEach(function (p) { grid.appendChild(makeProjectCard(p)); });
      box.appendChild(label);
      box.appendChild(grid);
      box.hidden = false;
    });
  }

  function loadClusterProjects() {
    var session = getSession();
    var API = window.AIOS_API;
    if (!session || !API || !API.projectsList) return;
    API.projectsList(session.token)
      .then(function (data) { renderClusterProjects((data && data.items) || []); })
      .catch(function () { /* проект недоступен — блоки остаются скрытыми */ });
  }

  window.addEventListener('aios-auth-change', render);
  render();

  /* --- спойлеры кластеров --- */

  function toggleSpoiler(spoiler) {
    var btn = spoiler.querySelector('.spoiler-toggle');
    var panel = spoiler.querySelector('.spoiler-body');
    if (!btn || !panel) return;
    var open = panel.hidden;
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    spoiler.classList.toggle('spoiler--open', open);
  }

  document.querySelectorAll('.spoiler').forEach(function (spoiler) {
    var btn = spoiler.querySelector('.spoiler-toggle');
    if (btn) btn.addEventListener('click', function () { toggleSpoiler(spoiler); });
  });

})();
