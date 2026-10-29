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

  /* админ ли текущая сессия (aios-auth-v1 в localStorage): тестовые
     карточки каталога с флагом admin_only показываем только админам */
  function isAdminSession() {
    var s = getSession();
    return !!(s && s.role === 'Администратор');
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

  function makeCard(item, onClick, opts) {
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
    if (item.admin_only) {
      /* тестовая карточка каталога: показывается только админам */
      var test = document.createElement('span');
      test.className = 'prj-status prj-status--test';
      test.textContent = 'тест';
      body.appendChild(test);
    }
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

  /* ссылка на карточку проекта (projects.html?id=…) в новом окне —
     единый формат для RAG-баз и онтологий */
  function makeProjectLink(name, projectId) {
    var a = document.createElement('a');
    a.className = 'prj-link';
    a.href = 'projects.html?id=' + encodeURIComponent(projectId);
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = name || projectId;
    return a;
  }

  /* «Проект: <ссылки>» — названия из item.project(s), адреса из
     item.project_id(s); без привязки к проекту — прочерк */
  function fillProjectLine(el, prefix, names, ids) {
    el.textContent = '';
    el.appendChild(document.createTextNode(prefix + ' '));
    var written = false;
    (names || []).forEach(function (name, i) {
      if (i) el.appendChild(document.createTextNode(', '));
      el.appendChild(makeProjectLink(name, (ids || [])[i] || ''));
      written = true;
    });
    if (!written) el.appendChild(document.createTextNode('—'));
  }

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
    fillProjectLine(
      document.getElementById('ragModalSource'),
      'Проект:',
      base.project ? [base.project] : [],
      base.project_id ? [base.project_id] : []
    );
    if (base.domain) {
      var src = document.getElementById('ragModalSource');
      src.appendChild(document.createTextNode(' · ' + base.domain));
    }
    var actions = document.getElementById('ragModalActions');
    actions.innerHTML = '';
    /* демо-базы платформы: документы корпуса открываются прямо из карточки */
    if (base.corpus && base.documents && base.documents.length) {
      var list = document.createElement('div');
      list.className = 'mm-source rag-corpus-list';
      list.appendChild(document.createTextNode('Документы: '));
      base.documents.forEach(function (name, i) {
        if (i) list.appendChild(document.createTextNode(' · '));
        var a = document.createElement('a');
        a.href = base.corpus + encodeURIComponent(name);
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = name.replace(/^\d+_/, '').replace(/\.(md|txt)$/, '').replace(/_/g, ' ');
        list.appendChild(a);
      });
      actions.appendChild(list);
    }
    if (base.ontology_id) {
      var onto = document.createElement('a');
      onto.className = 'btn-register mm-btn';
      onto.href = 'ontology.html?id=' + encodeURIComponent(base.ontology_id);
      onto.textContent = 'Онтология базы в 3D';
      actions.appendChild(onto);
    }
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
        var items = ((data && data.items) || []).filter(function (b) {
          return isAdminSession() || !b.admin_only;
        });
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
      ['Статус', onto.status],
      ['Автор', onto.author]
    ]);
    var ontoIds = onto.project_ids || (onto.projects || []).map(function () { return ''; });
    fillProjectLine(
      document.getElementById('ontoModalProjects'),
      'Проекты:',
      onto.projects || [],
      ontoIds
    );
    renderTags(document.getElementById('ontoModalTags'), onto.tags);
    var actions = document.getElementById('ontoModalActions');
    actions.innerHTML = '';
    if (onto.file_url) {
      var view3d = document.createElement('a');
      view3d.className = 'btn-primary mm-btn';
      view3d.href = 'ontology.html?id=' + encodeURIComponent(onto.id);
      view3d.textContent = '3D-визуализация';
      actions.appendChild(view3d);
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
        var items = ((data && data.items) || []).filter(function (onto) {
          return isAdminSession() || !onto.admin_only;
        });
        if (!items.length) {
          showEmpty(ontoGrid, 'Онтологии пока не добавлены — соберите первую навыком «Конструктор онтологий» прямо в чате.');
          return;
        }
        ontoGrid.innerHTML = '';
        items.forEach(function (onto) {
          ontoGrid.appendChild(makeCard(onto, openOnto));
        });
      })
      .catch(function () { showError(ontoGrid, loadOnto); });
  }

  /* CTA «Сконструировать …»: кладём флаг навыка тем же ключом, что и
     agents.js, и просим панель чата открыться (событие ловит
     workspace.js — см. aios-launch-skill) */
  function launchSkill(skillId, skillName) {
    try {
      localStorage.setItem('aios-workspace-skill-v1', JSON.stringify({
        skill_id: skillId,
        name: skillName,
        open: false
      }));
    } catch (e) { /* приватный режим */ }
    document.dispatchEvent(new CustomEvent('aios-launch-skill', {
      detail: { skill_id: skillId, name: skillName }
    }));
  }

  var ontoCta = document.getElementById('btnOntoConstructor');
  if (ontoCta) {
    ontoCta.addEventListener('click', function () {
      launchSkill('ontology-engineer', 'Конструктор онтологий');
    });
  }

  var ragCta = document.getElementById('btnRagConstructor');
  if (ragCta) {
    ragCta.addEventListener('click', function () {
      launchSkill('rag-builder', 'Конструктор RAG');
    });
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

  /* ============================================================
     Мои RAG-базы (после входа): список своих черновиков/баз,
     карточка с формой загрузки документов и публикацией.
     ============================================================ */

  var AUTH_KEY = 'aios-auth-v1';
  var API = window.AIOS_API;

  function getSession() {
    try {
      var raw = localStorage.getItem(AUTH_KEY);
      var s = raw ? JSON.parse(raw) : null;
      return s && s.token ? s : null;
    } catch (e) {
      return null;
    }
  }

  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  var myRagSection = document.getElementById('sectionMyRag');
  var myRagDetailEl = document.getElementById('myRagDetail');
  var myRagDetailName = document.getElementById('myRagDetailName');
  var myRagDetailDesc = document.getElementById('myRagDetailDesc');
  var myRagDetailMeta = document.getElementById('myRagDetailMeta');
  var myRagFileInput = document.getElementById('myRagFileInput');
  var myRagUploadStatus = document.getElementById('myRagUploadStatus');
  var myRagDocsEl = document.getElementById('myRagDocs');
  var btnMyRagBack = document.getElementById('btnMyRagBack');
  var btnMyRagPublish = document.getElementById('btnMyRagPublish');
  var myRagPublishMsg = document.getElementById('myRagPublishMsg');

  var docPollTimer = null;
  var currentRagId = null;
  var localSkipped = []; /* не добавленные файлы: только в памяти страницы, до обновления */

  var DOC_STATUS_LABEL = {
    queued: 'в очереди',
    processing: 'обрабатывается',
    ready: 'готов',
    error: 'ошибка'
  };

  function stopDocPolling() {
    if (docPollTimer) {
      clearInterval(docPollTimer);
      docPollTimer = null;
    }
  }

  function renderDocs(items) {
    if (!myRagDocsEl) return;
    var session = getSession();
    items = items.filter(function (d) { return !(d.error && d.error.indexOf('Не добавлено') === 0); }).concat(localSkipped);
    API.ragRenderDocs(myRagDocsEl, items, {
      token: session && session.token,
      ragId: currentRagId,
      refresh: function () { pollDocs(getSession(), currentRagId); },
      onError: function (m) { myRagUploadStatus.textContent = m; }
    });
  }

  function pollDocs(session, ragId) {
    if (!API.ragListDocuments) return;
    API.ragListDocuments(session.token, ragId)
      .then(function (data) {
        renderDocs((data && data.items) || []);
      })
      .catch(function () { /* сеть моргнула — подождём следующий тик */ });
  }

  function openMyRagDetail(session, ragId) {
    currentRagId = ragId;
    localSkipped = [];
    stopDocPolling();
    if (!API.ragGet) return;
    API.ragGet(session.token, ragId).then(function (card) {
      myRagDetailEl.hidden = false;
      myRagDetailName.textContent = card.name || ragId;
      myRagDetailDesc.textContent = card.description || '';
      myRagDetailMeta.textContent = 'Статус: ' + (card.status || 'draft') +
        (card.ontology_id ? ' · онтология: ' + card.ontology_id : '');
      myRagUploadStatus.textContent = '';
      myRagPublishMsg.textContent = '';
      renderDocs(card.documents || []);
      pollDocs(session, ragId);
      docPollTimer = setInterval(function () { pollDocs(session, ragId); }, 4000);
    }).catch(function (err) {
      myRagPublishMsg.textContent = 'Не удалось открыть базу: ' + (err && err.message ? err.message : 'ошибка сети');
    });
  }

  function loadMyRag(preselectRagId) {
    var session = getSession();
    if (!myRagSection) return;
    if (!session || !preselectRagId) {
      myRagSection.hidden = true;
      stopDocPolling();
      return;
    }
    myRagSection.hidden = false;
    openMyRagDetail(session, preselectRagId);
  }

  if (btnMyRagBack) {
    btnMyRagBack.addEventListener('click', function () {
      window.location.href = 'cabinet.html';
    });
  }

  if (myRagFileInput) {
    myRagFileInput.addEventListener('change', function () {
      var files = myRagFileInput.files ? Array.prototype.slice.call(myRagFileInput.files) : [];
      var session = getSession();
      if (!files.length || !session || !currentRagId || !API.ragUploadDocument) return;
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
        tooBig.forEach(function (f) {
          localSkipped.push({ filename: f.name, status: 'error', error: 'Не добавлено: превышен лимит ' + limitMb(f) + ' МБ' });
        });
        archSkipped.forEach(function (x) {
          var i = x.name.lastIndexOf('/');
          localSkipped.push({ filename: x.name.slice(i + 1), folder: i > 0 ? x.name.slice(0, i) : '', status: 'error', error: 'Не добавлено: ' + x.reason });
        });
      }
      if (!toUpload.length) {
        addLocal();
        pollDocs(session, currentRagId);
        myRagUploadStatus.textContent = bigText();
        myRagFileInput.value = '';
        return;
      }
      function uploadNext(i, okCount, errCount) {
        if (i >= toUpload.length) {
          var parts = [];
          if (okCount) parts.push(okCount + ' файл(ов) принято, идёт обработка');
          if (errCount) parts.push(errCount + ' с ошибкой загрузки');
          if (tooBig.length) parts.push(bigText());
          if (archSkipped.length) parts.push(archText());
          myRagUploadStatus.textContent = parts.join('. ') || 'Готово';
          addLocal();
          pollDocs(session, currentRagId);
          myRagFileInput.value = '';
          return;
        }
        var file = toUpload[i];
        myRagUploadStatus.textContent = 'Загрузка «' + file.name + '» (' + (i + 1) + ' из ' + toUpload.length + ')…';
        API.ragUploadDocument(session.token, currentRagId, file).then(function (res) {
          var isArch = res && res.archive;
          if (isArch && res.skipped) res.skipped.forEach(function (x) { archSkipped.push(x); });
          uploadNext(i + 1, okCount + (isArch ? (res.queued || 0) : 1), errCount);
        }).catch(function () {
          uploadNext(i + 1, okCount, errCount + 1);
        });
      }
      uploadNext(0, 0, 0);
    });
  }

  if (btnMyRagPublish) {
    btnMyRagPublish.addEventListener('click', function () {
      var session = getSession();
      if (!session || !currentRagId || !API.ragPublish) return;
      myRagPublishMsg.textContent = 'Публикация…';
      API.ragPublish(session.token, currentRagId).then(function () {
        myRagPublishMsg.textContent = 'Опубликовано в каталоге «Данные и знания».';
      }).catch(function (err) {
        myRagPublishMsg.textContent = 'Не удалось опубликовать: ' + (err && err.message ? err.message : 'ошибка');
      });
    });
  }

  var params = new URLSearchParams(window.location.search);
  var initialRagId = params.get('rag_id');

  window.addEventListener('aios-auth-change', function () { loadMyRag(initialRagId); });
  loadMyRag(initialRagId);
})();
