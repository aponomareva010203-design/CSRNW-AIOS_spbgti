/* ============================================================
   Страница «Агенты и навыки».
   Карточки агентов и навыков приходят из JSON-файлов
   (data/agents.json, data/skills.json) — в коде не хардкодятся.
   Данные грузятся лениво при первом разворачивании спойлера.
   ============================================================ */

(function () {
  var PLACEHOLDER_IMG = 'assets/model-placeholder.svg';

  /* ---------- утилиты ---------- */

  function fetchJSON(url) {
    return fetch(url, { headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      });
  }

  function showSkeletons(grid, count) {
    grid.innerHTML = '';
    for (var i = 0; i < count; i++) {
      var sk = document.createElement('div');
      sk.className = 'model-card model-card--skeleton';
      sk.setAttribute('aria-hidden', 'true');
      grid.appendChild(sk);
    }
  }

  function showError(grid, retry) {
    grid.innerHTML = '';
    var box = document.createElement('div');
    box.className = 'models-error';
    var text = document.createElement('span');
    text.textContent = 'Не удалось загрузить данные. Проверьте соединение и попробуйте ещё раз.';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-register models-retry';
    btn.textContent = 'Повторить';
    btn.addEventListener('click', retry);
    box.appendChild(text);
    box.appendChild(btn);
    grid.appendChild(box);
  }

  function showEmpty(grid, message) {
    grid.innerHTML = '';
    var box = document.createElement('div');
    box.className = 'models-empty';
    box.textContent = message;
    grid.appendChild(box);
  }

  /* ---------- модальные окна ---------- */

  function bindModal(overlay, closeBtn) {
    function close() {
      overlay.hidden = true;
      document.body.classList.remove('modal-open');
    }
    closeBtn.addEventListener('click', close);
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) close();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !overlay.hidden) close();
    });
    return {
      open: function () {
        overlay.hidden = false;
        document.body.classList.add('modal-open');
        closeBtn.focus();
      },
      close: close
    };
  }

  /* ---------- агенты ---------- */

  var agentsGrid = document.getElementById('gridAgents');
  var agentModal = bindModal(
    document.getElementById('agentModal'),
    document.getElementById('agentModalClose')
  );
  var agentImg = document.getElementById('agentModalImg');
  agentImg.addEventListener('error', function () {
    if (agentImg.src.indexOf(PLACEHOLDER_IMG) === -1) agentImg.src = PLACEHOLDER_IMG;
  });

  function openAgent(agent) {
    agentImg.src = agent.image_url || PLACEHOLDER_IMG;
    agentImg.alt = agent.name;
    document.getElementById('agentModalName').textContent = agent.name;
    document.getElementById('agentModalDesc').textContent = agent.description_ru || '';
    var tags = document.getElementById('agentModalTags');
    tags.innerHTML = '';
    (agent.tags || []).forEach(function (tag) {
      var chip = document.createElement('span');
      chip.className = 'tag-chip';
      chip.textContent = tag;
      tags.appendChild(chip);
    });
    document.getElementById('agentModalSource').textContent =
      'Источник: GitHub' + (agent.provider ? ' · ' + agent.provider : '');
    var actions = document.getElementById('agentModalActions');
    actions.innerHTML = '';
    if (agent.access_url) {
      var link = document.createElement('a');
      link.className = 'btn-primary mm-btn';
      link.href = agent.access_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Перейти к репозиторию';
      actions.appendChild(link);
    }
    agentModal.open();
  }

  function makeAgentCard(agent) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'model-card';
    var media = document.createElement('span');
    media.className = 'model-card-media';
    var img = document.createElement('img');
    img.src = agent.image_url || PLACEHOLDER_IMG;
    img.alt = agent.name;
    img.loading = 'lazy';
    img.addEventListener('error', function () {
      if (img.src.indexOf(PLACEHOLDER_IMG) === -1) img.src = PLACEHOLDER_IMG;
    });
    media.appendChild(img);
    var body = document.createElement('span');
    body.className = 'model-card-body';
    var name = document.createElement('span');
    name.className = 'model-card-name';
    name.textContent = agent.name;
    var sub = document.createElement('span');
    sub.className = 'model-card-sub';
    sub.textContent = agent.provider || '';
    body.appendChild(name);
    body.appendChild(sub);
    card.appendChild(media);
    card.appendChild(body);
    card.addEventListener('click', function () { openAgent(agent); });
    return card;
  }

  function loadAgents() {
    if (!agentsGrid) return;
    showSkeletons(agentsGrid, 4);
    fetchJSON('/data/agents.json')
      .then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) {
          showEmpty(agentsGrid, 'Каталог агентов пока пуст.');
          return;
        }
        agentsGrid.innerHTML = '';
        items.forEach(function (agent) {
          agentsGrid.appendChild(makeAgentCard(agent));
        });
      })
      .catch(function () { showError(agentsGrid, loadAgents); });
  }

  /* ---------- навыки ---------- */

  var skillsGrid = document.getElementById('gridSkills');
  var skillModal = bindModal(
    document.getElementById('skillModal'),
    document.getElementById('skillModalClose')
  );

  function openSkill(skill) {
    document.getElementById('skillModalName').textContent = skill.name;
    document.getElementById('skillModalDomain').textContent = skill.domain || '—';
    document.getElementById('skillModalDesc').textContent = skill.description_ru || '';
    document.getElementById('skillModalAuthor').textContent = skill.author || '—';
    document.getElementById('skillModalModel').textContent = skill.recommended_model || '—';
    var dl = document.getElementById('skillModalDownload');
    if (skill.file_url) {
      dl.href = skill.file_url;
      dl.style.display = '';
    } else {
      dl.style.display = 'none';
    }
    /* «Запустить в рабочем чате»: панель workspace.js уже подключена
       на этой же странице (плавающая панель, есть на любой странице AIOS) —
       поэтому не нужен localStorage-флаг «на следующую загрузку», как было
       раньше (баг: чат не открывался сам, пока страницу не перезагрузят).
       Диспатчим aios-launch-skill — тот же путь, что и кнопка «Запустить
       онбординг проекта» в cabinet.js: чат откроется и стартует сразу. */
    var launch = document.getElementById('skillModalLaunch');
    if (launch) {
      launch.onclick = function () {
        skillModal.close();
        document.dispatchEvent(new CustomEvent('aios-launch-skill', {
          detail: { skill_id: skill.id, name: skill.name || '' }
        }));
      };
    }
    skillModal.open();
  }

  function makeSkillCard(skill) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'skill-card';
    var icon = document.createElement('span');
    icon.className = 'skill-card-icon';
    icon.setAttribute('aria-hidden', 'true');
    var name = document.createElement('span');
    name.className = 'skill-card-name';
    name.textContent = skill.name;
    var domain = document.createElement('span');
    domain.className = 'skill-card-domain';
    domain.textContent = skill.domain || '';
    card.appendChild(icon);
    card.appendChild(name);
    card.appendChild(domain);
    card.addEventListener('click', function () { openSkill(skill); });
    return card;
  }

  function loadSkills() {
    if (!skillsGrid) return;
    showSkeletons(skillsGrid, 3);
    fetchJSON('/data/skills.json')
      .then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) {
          showEmpty(skillsGrid, 'Навыки пока не добавлены — скоро здесь появится первая карточка.');
          return;
        }
        skillsGrid.innerHTML = '';
        items.forEach(function (skill) {
          skillsGrid.appendChild(makeSkillCard(skill));
        });
      })
      .catch(function () { showError(skillsGrid, loadSkills); });
  }

  /* ---------- спойлеры с ленивой загрузкой ---------- */

  var loaded = {};

  var LOADERS = {
    spoilerAgents: loadAgents,
    spoilerSkills: loadSkills
  };

  function toggleSpoiler(spoiler) {
    var btn = spoiler.querySelector('.spoiler-toggle');
    var panel = spoiler.querySelector('.spoiler-body');
    if (!btn || !panel) return;
    var open = panel.hidden;
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    spoiler.classList.toggle('spoiler--open', open);
    if (open && !loaded[spoiler.id]) {
      loaded[spoiler.id] = true;
      var loader = LOADERS[spoiler.id];
      if (loader) loader();
    }
  }

  document.querySelectorAll('.spoiler').forEach(function (spoiler) {
    var btn = spoiler.querySelector('.spoiler-toggle');
    if (btn) {
      btn.addEventListener('click', function () { toggleSpoiler(spoiler); });
    }
  });
})();
