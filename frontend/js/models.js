/* ============================================================
   Страница «Модели»: два раздела с карточками из API.
   Данные приходят только через window.AIOS_API.getModels()
   (GET /api/models?source=...). Никаких моков на фронтенде.
   ============================================================ */

(function () {
  var PLACEHOLDER_IMG = 'assets/model-placeholder.svg';
  var SOURCE_LABELS = {
    spbti: 'Технологический институт',
    huggingface: 'Hugging Face'
  };

  var sections = [
    { source: 'spbti', grid: document.getElementById('gridSpbti'), empty: 'Пока здесь пусто — скоро появятся модели Технологического института.' },
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
