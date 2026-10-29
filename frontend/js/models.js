/* ============================================================
   Страница «Модели».
   - Доступ: «Модели проектов» — только роль «Администратор»; гостям и
     обычным пользователям вместо них — плашка; переключение живое, по
     событию 'aios-auth-change' из auth.js. «Модели офиса проректора»
     (js/innovation_models.js) и витрина открытых моделей открыты всем.
   - «Модели проектов» — реестр моделей, обученных на опытах
     проектных команд (GET /api/models/registry): карточка с погрешностью,
     точностью относительно угадывания средним и статусом; клик ведёт
     на страницу модели model.html?id=…; «Создать модель» запускает
     в чате агента «Конструктор моделей»; «Импортировать ONNX» — модель,
     обученная вне платформы.
   - «Открытые модели (Hugging Face)» — витрина Hugging Face
     (GET /api/models?source=...), как раньше.
   ============================================================ */

(function () {
  var content = document.getElementById('modelsContent');
  var locked = document.getElementById('modelsLocked');
  var teamGrid = document.getElementById('gridTeam');
  if (!content || !locked || !teamGrid || !window.AIOS_API) return;

  /* --- доступ: роль из сессии (aios-auth-v1), как в js/pish.js --- */

  function isAdmin() {
    var s = null;
    try { s = JSON.parse(localStorage.getItem('aios-auth-v1') || 'null'); }
    catch (e) { s = null; }
    return !!(s && s.token && s.role === 'Администратор');
  }

  function renderAccess() {
    var admin = isAdmin();
    content.hidden = !admin;
    locked.hidden = admin;
    return admin;
  }

  var KIND_ICONS = {
    table: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 10h16M4 14.5h16M10 5v14"/></svg>',
    formula: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5h-3.2a2 2 0 0 0-2 1.7L8 18.3A2 2 0 0 1 6 20H5"/><path d="M7 11h8M14 14l5 5M19 14l-5 5"/></svg>',
    kinetics: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19c4 0 5-14 9-14 3 0 3.5 6 7 6"/><path d="M4 5v14h16"/></svg>',
    onnx: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/></svg>'
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function num(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    var a = Math.abs(v);
    var digits = a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3;
    return Number(v).toLocaleString('ru-RU', { maximumFractionDigits: digits });
  }

  function ring(skill) {
    var p = Math.max(0, Math.min(1, skill || 0));
    var c = 2 * Math.PI * 16;
    return '<svg class="mdl-ring" viewBox="0 0 40 40" aria-hidden="true">' +
      '<circle cx="20" cy="20" r="16" class="mdl-ring-bg"/>' +
      '<circle cx="20" cy="20" r="16" class="mdl-ring-fg" stroke-dasharray="' + (c * p).toFixed(1) + ' ' + c.toFixed(1) + '"/>' +
      '</svg>';
  }

  function teamCard(m) {
    var a = document.createElement('a');
    a.className = 'mdl-card mdl-card--' + (m.status || 'draft');
    a.href = m.url || ('model.html?id=' + encodeURIComponent(m.id));
    var h = m.headline || {};
    var skill = typeof h.skill === 'number' ? h.skill : null;
    var out = (m.outputs || [])[0] || {};
    a.innerHTML =
      '<span class="mdl-card-top">' +
        '<span class="mdl-kind">' + (KIND_ICONS[m.kind] || '') + esc(m.kind_title || '') + '</span>' +
        '<span class="mdl-status mdl-status--' + esc(m.status) + '">' + esc(m.status_title || '') + '</span>' +
      '</span>' +
      '<span class="mdl-card-name">' + esc(m.name) + '</span>' +
      '<span class="mdl-card-out">' + (out.title ? 'Предсказывает: ' + esc(out.title) + (out.unit ? ', ' + esc(out.unit) : '') : '') + '</span>' +
      '<span class="mdl-card-metric">' +
        (skill !== null ? ring(skill) : '') +
        '<span class="mdl-card-metric-text">' +
          (h.mae !== undefined && h.mae !== null
            ? '<b>±' + num(h.mae) + (h.unit ? ' ' + esc(h.unit) : '') + '</b>'
            : '<b>точность не измерена</b>') +
          (skill !== null ? '<span>точнее угадывания средним на ' + Math.round(skill * 100) + ' %</span>' : '') +
        '</span>' +
      '</span>' +
      '<span class="mdl-card-foot">' +
        esc([m.project_title || m.author || '', h.n ? h.n + ' опытов' : '', m.version ? 'версия ' + m.version : '']
          .filter(Boolean).join(' · ')) +
      '</span>';
    return a;
  }

  function loadTeam() {
    teamGrid.innerHTML = '<div class="model-card model-card--skeleton"></div><div class="model-card model-card--skeleton"></div>';
    window.AIOS_API.teamModelsList().then(function (data) {
      var items = (data && data.items) || [];
      teamGrid.innerHTML = '';
      if (!items.length) {
        var box = document.createElement('div');
        box.className = 'models-empty';
        box.textContent = 'Моделей пока нет. Создайте первую по таблице опытов — конструктор в чате проведёт по шагам: ' +
          'что предсказываем, от чего, какую ошибку готовы принять.';
        teamGrid.appendChild(box);
        return;
      }
      items.forEach(function (m) { teamGrid.appendChild(teamCard(m)); });
    }).catch(function () {
      teamGrid.innerHTML = '<div class="models-error">Не удалось загрузить модели проектов.</div>';
    });
  }

  function loggedIn() {
    try {
      var raw = localStorage.getItem('aios-auth-v1');
      return !!(raw && JSON.parse(raw).token);
    } catch (e) {
      return false;
    }
  }

  function askLogin() {
    var btn = document.getElementById('btnLogin');
    if (btn) btn.click();
  }

  var createBtn = document.getElementById('btnModelCreate');
  if (createBtn) {
    createBtn.addEventListener('click', function () {
      if (!loggedIn()) { askLogin(); return; }
      document.dispatchEvent(new CustomEvent('aios-launch-skill', {
        detail: { skill_id: 'model-builder', name: 'Конструктор моделей' }
      }));
    });
  }

  /* ---------- импорт ONNX ---------- */

  var importModal = document.getElementById('importModal');
  var importForm = document.getElementById('importForm');
  var importError = document.getElementById('importError');

  function parseLines(text, withRange) {
    return String(text || '').split(/\n+/).map(function (line) {
      return line.split(';').map(function (p) { return p.trim(); });
    }).filter(function (parts) { return parts[0]; }).map(function (parts) {
      var item = { title: parts[0], column: parts[0], unit: parts[1] || '' };
      if (withRange && parts[2] !== undefined && parts[3] !== undefined && parts[2] !== '' && parts[3] !== '') {
        item.min = parseFloat(parts[2].replace(',', '.'));
        item.max = parseFloat(parts[3].replace(',', '.'));
      }
      return item;
    });
  }

  function closeImport() {
    importModal.hidden = true;
    document.body.classList.remove('modal-open');
  }

  var importBtn = document.getElementById('btnModelImport');
  if (importBtn && importModal) {
    importBtn.addEventListener('click', function () {
      if (!loggedIn()) { askLogin(); return; }
      importModal.hidden = false;
      document.body.classList.add('modal-open');
      document.getElementById('importName').focus();
    });
    document.getElementById('importClose').addEventListener('click', closeImport);
    importModal.addEventListener('click', function (e) { if (e.target === importModal) closeImport(); });
    importForm.addEventListener('submit', function (e) {
      e.preventDefault();
      importError.hidden = true;
      var inputs = parseLines(document.getElementById('importInputs').value, true);
      var outputs = parseLines(document.getElementById('importOutputs').value, false);
      var spec = { inputs: inputs, outputs: outputs };
      var thr = parseFloat(String(document.getElementById('importThreshold').value || '').replace(',', '.'));
      if (!isNaN(thr)) spec.threshold = { max: thr };
      var submit = document.getElementById('importSubmit');
      submit.disabled = true;
      submit.textContent = 'Проверяем модель…';
      window.AIOS_API.teamModelImport({
        name: document.getElementById('importName').value,
        description: document.getElementById('importDesc').value,
        spec: JSON.stringify(spec)
      }, document.getElementById('importOnnx').files[0], document.getElementById('importHoldout').files[0])
        .then(function (res) {
          window.location.href = 'model.html?id=' + encodeURIComponent(res.model.id);
        })
        .catch(function (err) {
          importError.textContent = err.message || 'Импорт не удался';
          importError.hidden = false;
        })
        .then(function () {
          submit.disabled = false;
          submit.textContent = 'Проверить и импортировать';
        });
    });
  }

  /* реестр грузим только администратору: остальным API отвечает 403,
     а содержание страницы спрятано замком */
  if (renderAccess()) loadTeam();
  window.addEventListener('aios-auth-change', function () {
    if (renderAccess()) loadTeam();
  });
})();

(function () {
  var PLACEHOLDER_IMG = 'assets/model-placeholder.svg';
  var SOURCE_LABELS = {
    spbti: 'Университет',
    huggingface: 'Hugging Face'
  };

  var sections = [
    { source: 'spbti', grid: document.getElementById('gridSpbti'), empty: 'Пока здесь пусто — скоро появятся модели университета.' },
    { source: 'huggingface', grid: document.getElementById('gridOpen'), empty: 'Каталог открытых моделей пуст.' }
  ];

  var modal = document.getElementById('modelModal');
  var modalImg = document.getElementById('modelModalImg');
  var modalName = document.getElementById('modelModalName');
  var modalDesc = document.getElementById('modelModalDesc');
  var modalTags = document.getElementById('modelModalTags');
  var modalSource = document.getElementById('modelModalSource');
  var modalActions = document.getElementById('modelModalActions');
  var modalClose = document.getElementById('modelModalClose');

  if (!modal || !window.AIOS_API) return;

  /* ---------- подробности модели ---------- */

  function openDetails(model) {
    modalImg.src = model.image_url || PLACEHOLDER_IMG;
    modalImg.alt = model.name;
    modalName.textContent = model.name;
    modalDesc.textContent = model.description_ru || '';
    modalTags.innerHTML = '';
    (model.tags || []).forEach(function (tag) {
      var chip = document.createElement('span');
      chip.className = 'tag-chip';
      chip.textContent = tag;
      modalTags.appendChild(chip);
    });
    modalSource.textContent = 'Источник: ' + (SOURCE_LABELS[model.source] || model.provider || '—');
    modalActions.innerHTML = '';
    if (model.access_url && !model.is_placeholder) {
      var link = document.createElement('a');
      link.className = 'btn-primary mm-btn';
      link.href = model.access_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Перейти к модели';
      modalActions.appendChild(link);
    } else {
      var stub = document.createElement('button');
      stub.className = 'btn-primary mm-btn';
      stub.type = 'button';
      stub.disabled = true;
      stub.textContent = 'Доступ появится позже';
      modalActions.appendChild(stub);
    }
    modal.hidden = false;
    document.body.classList.add('modal-open');
    modalClose.focus();
  }

  function closeDetails() {
    modal.hidden = true;
    document.body.classList.remove('modal-open');
  }

  modalImg.addEventListener('error', function () {
    if (modalImg.src.indexOf(PLACEHOLDER_IMG) === -1) modalImg.src = PLACEHOLDER_IMG;
  });
  modalClose.addEventListener('click', closeDetails);
  modal.addEventListener('click', function (e) {
    if (e.target === modal) closeDetails();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !modal.hidden) closeDetails();
  });

  /* ---------- карточки ---------- */

  function makeCard(model) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'model-card' + (model.is_placeholder ? ' model-card--placeholder' : '');

    var media = document.createElement('span');
    media.className = 'model-card-media';
    var img = document.createElement('img');
    img.src = model.image_url || PLACEHOLDER_IMG;
    img.alt = model.name;
    img.loading = 'lazy';
    img.addEventListener('error', function () {
      if (img.src.indexOf(PLACEHOLDER_IMG) === -1) img.src = PLACEHOLDER_IMG;
    });
    media.appendChild(img);

    var body = document.createElement('span');
    body.className = 'model-card-body';
    var name = document.createElement('span');
    name.className = 'model-card-name';
    name.textContent = model.name;
    var sub = document.createElement('span');
    sub.className = 'model-card-sub';
    sub.textContent = model.provider || '';
    body.appendChild(name);
    body.appendChild(sub);

    card.appendChild(media);
    card.appendChild(body);
    card.addEventListener('click', function () { openDetails(model); });
    return card;
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

  function showError(grid, section) {
    grid.innerHTML = '';
    var box = document.createElement('div');
    box.className = 'models-error';
    var text = document.createElement('span');
    text.textContent = 'Не удалось загрузить модели. Проверьте соединение и попробуйте ещё раз.';
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn-register models-retry';
    retry.textContent = 'Повторить';
    retry.addEventListener('click', function () { loadSection(section); });
    box.appendChild(text);
    box.appendChild(retry);
    grid.appendChild(box);
  }

  function showEmpty(grid, message) {
    grid.innerHTML = '';
    var box = document.createElement('div');
    box.className = 'models-empty';
    box.textContent = message;
    grid.appendChild(box);
  }

  function loadSection(section) {
    if (!section.grid) return;
    showSkeletons(section.grid, section.source === 'spbti' ? 1 : 4);
    window.AIOS_API.getModels(section.source)
      .then(function (data) {
        var items = (data && data.items) || [];
        /* блок «Сделано в университете» показывается, только когда в нём есть модели */
        var wrap = section.source === 'spbti' ? document.getElementById('sectionSpbti') : null;
        if (wrap) wrap.hidden = !items.length;
        if (!items.length) {
          showEmpty(section.grid, section.empty);
          return;
        }
        section.grid.innerHTML = '';
        items.forEach(function (model) {
          section.grid.appendChild(makeCard(model));
        });
      })
      .catch(function () { showError(section.grid, section); });
  }

  /* ---------- спойлеры ---------- */

  var repoLoaded = false;

  function toggleSpoiler(spoiler) {
    var btn = spoiler.querySelector('.spoiler-toggle');
    var panel = spoiler.querySelector('.spoiler-body');
    if (!btn || !panel) return;
    var open = panel.hidden;
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    spoiler.classList.toggle('spoiler--open', open);
    if (open && spoiler.id === 'spoilerRepo' && !repoLoaded) {
      repoLoaded = true;
      sections.forEach(loadSection);
    }
  }

  document.querySelectorAll('.spoiler').forEach(function (spoiler) {
    var btn = spoiler.querySelector('.spoiler-toggle');
    if (btn) {
      btn.addEventListener('click', function () { toggleSpoiler(spoiler); });
    }
  });
})();
