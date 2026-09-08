/* ============================================================
Страница «Данные и знания» (knowledge.html).
Карточки RAG-баз и онтологий приходят из JSON-файлов
(data/rag_bases.json, data/ontologies.json) — в коде не хардкодятся.
Данные грузятся лениво при первом разворачивании спойлера.
Код зеркалит frontend/js/agents-2.js — те же классы из style.css
(.model-card, .spoiler, .modal-overlay, .tag-chip и т.д.),
чтобы страница визуально не отличалась от «Моделей»/«Агентов».
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

  function renderChips(container, pairs) {
    container.innerHTML = '';
    pairs
      .filter(function (p) { return p[1] !== undefined && p[1] !== null && p[1] !== ''; })
      .forEach(function (p) {
        var chip = document.createElement('span');
        chip.className = 'tag-chip';
        chip.textContent = p[0] + ': ' + p[1];
        container.appendChild(chip);
      });
  }

  function renderTags(container, tags) {
    container.innerHTML = '';
    (tags || []).forEach(function (tag) {
      var chip = document.createElement('span');
      chip.className = 'tag-chip';
      chip.textContent = '#' + tag;
      container.appendChild(chip);
    });
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

  function makeCard(item, onClick) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'model-card';
    var media = document.createElement('span');
    media.className = 'model-card-media';
    var img = document.createElement('img');
    img.src = item.image_url || PLACEHOLDER_IMG;
    img.alt = item.name;
    img.loading = 'lazy';
    img.addEventListener('error', function () {
      if (img.src.indexOf(PLACEHOLDER_IMG) === -1) img.src = PLACEHOLDER_IMG;
    });
    media.appendChild(img);
    var body = document.createElement('span');
    body.className = 'model-card-body';
    var name = document.createElement('span');
    name.className = 'model-card-name';
    name.textContent = item.name;
    var sub = document.createElement('span');
    sub.className = 'model-card-sub';
    sub.textContent = item.domain || item.project || '';
    body.appendChild(name);
    body.appendChild(sub);
    card.appendChild(media);
    card.appendChild(body);
    card.addEventListener('click', function () { onClick(item); });
    return card;
  }

  /* ---------- RAG-базы ---------- */

  var ragGrid = document.getElementById('gridRag');
  var ragModal = bindModal(
    document.getElementById('ragModal'),
    document.getElementById('ragModalClose')
  );
  var ragImg = document.getElementById('ragModalImg');
  ragImg.addEventListener('error', function () {
    if (ragImg.src.indexOf(PLACEHOLDER_IMG) === -1) ragImg.src = PLACEHOLDER_IMG;
  });

  function openRag(base) {
    ragImg.src = base.image_url || PLACEHOLDER_IMG;
    ragImg.alt = base.name;
    document.getElementById('ragModalName').textContent = base.name;
    document.getElementById('ragModalDesc').textContent = base.description_ru || '';
    renderChips(document.getElementById('ragModalChips'), [
      ['Документов', base.documents_count],
      ['Векторная БД', base.vector_db],
      ['Эмбеддинги', base.embedding_model],
      ['Статус', base.status]
    ]);
    renderTags(document.getElementById('ragModalTags'), base.tags);
    document.getElementById('ragModalSource').textContent =
      'Проект: ' + (base.project || '—') + (base.domain ? ' · ' + base.domain : '');
    var actions = document.getElementById('ragModalActions');
    actions.innerHTML = '';
    if (base.access_url) {
      var link = document.createElement('a');
      link.className = 'btn-primary mm-btn';
      link.href = base.access_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Открыть реестр';
      actions.appendChild(link);
    }
    ragModal.open();
  }

  function loadRag() {
    if (!ragGrid) return;
    showSkeletons(ragGrid, 4);
    fetchJSON('/data/rag_bases.json')
      .then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) {
          showEmpty(ragGrid, 'Реестр RAG-баз пока пуст.');
          return;
        }
        ragGrid.innerHTML = '';
        items.forEach(function (base) {
          ragGrid.appendChild(makeCard(base, openRag));
        });
      })
      .catch(function () { showError(ragGrid, loadRag); });
  }

  /* ---------- Онтологии ---------- */

  var ontoGrid = document.getElementById('gridOnto');
  var ontoModal = bindModal(
    document.getElementById('ontoModal'),
    document.getElementById('ontoModalClose')
  );
  var ontoImg = document.getElementById('ontoModalImg');
  ontoImg.addEventListener('error', function () {
    if (ontoImg.src.indexOf(PLACEHOLDER_IMG) === -1) ontoImg.src = PLACEHOLDER_IMG;
  });

  function openOnto(onto) {
    ontoImg.src = onto.image_url || PLACEHOLDER_IMG;
    ontoImg.alt = onto.name;
    document.getElementById('ontoModalName').textContent = onto.name;
    document.getElementById('ontoModalDesc').textContent = onto.description_ru || '';
    renderChips(document.getElementById('ontoModalChips'), [
      ['Классов', onto.classes_count],
      ['Связей', onto.relations_count],
      ['Формат', onto.format],
      ['Статус', onto.status]
    ]);
    document.getElementById('ontoModalProjects').textContent =
      'Проекты: ' + ((onto.projects || []).join(', ') || '—');
    renderTags(document.getElementById('ontoModalTags'), onto.tags);
    var actions = document.getElementById('ontoModalActions');
    actions.innerHTML = '';
    if (onto.file_url) {
      var dl = document.createElement('a');
      dl.className = 'btn-primary mm-btn';
      dl.href = onto.file_url;
      dl.download = '';
      dl.textContent = 'Скачать онтологию';
      actions.appendChild(dl);
    }
    ontoModal.open();
  }

  function loadOnto() {
    if (!ontoGrid) return;
    showSkeletons(ontoGrid, 4);
    fetchJSON('/data/ontologies.json')
      .then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) {
          showEmpty(ontoGrid, 'Онтологии пока не добавлены — скоро здесь появится первая карточка.');
          return;
        }
        ontoGrid.innerHTML = '';
        items.forEach(function (onto) {
          ontoGrid.appendChild(makeCard(onto, openOnto));
        });
      })
      .catch(function () { showError(ontoGrid, loadOnto); });
  }

  /* ---------- спойлеры с ленивой загрузкой ---------- */

  var loaded = {};

  var LOADERS = {
    spoilerRag: loadRag,
    spoilerOnto: loadOnto
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
