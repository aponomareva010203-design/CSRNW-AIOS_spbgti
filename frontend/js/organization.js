/* ============================================================
   Админ-панель → «Об организации».

   Два элемента раздела (данные — data/organization/*.json):
     1. «Информация об организации» — форма «Основная информация»
        (название, разворачиваемый список aliases, описание,
        направления деятельности) и «Показатели деятельности».
        Правки сохраняются в исходные profile.json / metrics.json
        (PUT /api/admin/organization/*); дата последнего обновления
        проставляется сервером автоматически (ДД.ММ.ГГГГ).
     2. «Основные документы» — три категории (Стратегия, Программа
        развития, Другие документы): загрузка файлов, короткое
        .md-описание для ассистента, скачивание по клику, удаление.

   Формы рендерятся по описанию полей из JSON (id/title/type/unit),
   поэтому будущие типы организаций не потребуют правок здесь.
   ============================================================ */

(function () {
  var wrap = document.getElementById('orgWrap');
  if (!wrap) return;

  var API = window.AIOS_API;
  var AUTH_KEY = 'aios-auth-v1';

  var msg = document.getElementById('orgMsg');
  var status = document.getElementById('orgStatus');
  var infoCard = document.getElementById('orgInfoCard');
  var docsCard = document.getElementById('orgDocsCard');
  var typeEl = document.getElementById('orgType');
  var profileForm = document.getElementById('orgProfileForm');
  var profileFields = document.getElementById('orgProfileFields');
  var profileUpdated = document.getElementById('orgProfileUpdated');
  var metricsForm = document.getElementById('orgMetricsForm');
  var metricsFields = document.getElementById('orgMetricsFields');
  var metricsUpdated = document.getElementById('orgMetricsUpdated');
  var docsGroups = document.getElementById('orgDocsGroups');

  var categories = {};   /* id → название категории (приходит с API) */
  var catOrder = [];     /* порядок групп в UI */
  var documents = [];    /* текущий список документов */

  function getSession() {
    try {
      var raw = localStorage.getItem(AUTH_KEY);
      var s = raw ? JSON.parse(raw) : null;
      return s && s.token ? s : null;
    } catch (e) {
      return null;
    }
  }

  var session = getSession();
  if (!session || session.role !== 'Администратор' || !API || !API.adminGetOrganization) return;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function showMsg(text) {
    if (!msg) return;
    msg.textContent = text;
    msg.hidden = !text; /* пустой текст не должен оставлять красную полосу-заглушку */
  }

  function showStatus(text) {
    if (!status) return;
    status.textContent = text || '';
    status.hidden = !text;
  }

  function fmtSize(bytes) {
    bytes = Number(bytes) || 0;
    if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1).replace('.', ',') + ' МБ';
    if (bytes >= 1024) return Math.round(bytes / 1024) + ' КБ';
    return bytes + ' Б';
  }

  /* ---------- элемент 1: профиль и показатели ---------- */

  function aliasRow(value) {
    return '<div class="org-alias-row">' +
      '<input type="text" value="' + esc(value) + '" placeholder="другое название организации">' +
      '<button class="org-alias-del" type="button" title="Убрать название">×</button>' +
      '</div>';
  }

  function renderProfileFields(profile) {
    if (!profileFields) return;
    var html = '';
    (profile.fields || []).forEach(function (f) {
      if (f.type === 'string_list') {
        html += '<div class="org-field org-field--aliases">' +
          '<span class="org-field-title">' + esc(f.title) + '</span>' +
          '<div class="org-aliases" data-field="' + esc(f.id) + '">' +
          (f.value || []).map(aliasRow).join('') +
          '</div>' +
          '<button class="adm-btn org-alias-add" type="button">+ Добавить название</button>' +
          '</div>';
      } else if (f.type === 'text') {
        html += '<label class="org-field"><span class="org-field-title">' + esc(f.title) + '</span>' +
          '<textarea name="' + esc(f.id) + '" rows="5" data-type="text">' + esc(f.value) + '</textarea></label>';
      } else {
        html += '<label class="org-field"><span class="org-field-title">' + esc(f.title) + '</span>' +
          '<input type="text" name="' + esc(f.id) + '" value="' + esc(f.value) + '" data-type="string"></label>';
      }
    });
    profileFields.innerHTML = html;
  }

  function renderMetricsFields(metrics) {
    if (!metricsFields) return;
    var html = '';
    (metrics.items || []).forEach(function (m) {
      var val = m.value == null ? '' : String(m.value).replace('.', ',');
      var unit = m.unit ? ', ' + m.unit : '';
      html += '<label class="org-field org-field--metric"><span class="org-field-title">' +
        esc(m.title) + unit + '</span>' +
        '<input type="text" inputmode="decimal" name="' + esc(m.id) + '" value="' + esc(val) +
        '" placeholder="—" data-type="number"></label>';
    });
    metricsFields.innerHTML = html;
  }

  function updatedText(data) {
    return data && data.updated_at ? 'Обновлено: ' + data.updated_at : '';
  }

  function collectSectionValues(container, fieldsKey) {
    /* значения полей формы → {field_id: value}; aliases собирается по строкам */
    var values = {};
    if (!container) return values;
    container.querySelectorAll('[name]').forEach(function (input) {
      values[input.name] = input.value;
    });
    container.querySelectorAll('.org-aliases').forEach(function (box) {
      var items = [];
      box.querySelectorAll('input').forEach(function (input) {
        if (input.value.trim()) items.push(input.value.trim());
      });
      values[box.getAttribute('data-field')] = items;
    });
    return values;
  }

  function saveSection(form, container, apiCall, updatedEl) {
    if (!form || !API) return;
    showStatus('');
    var saveBtn = form.querySelector('.org-save');
    if (saveBtn) saveBtn.disabled = true;
    var values = collectSectionValues(container);
    apiCall(session.token, values, form.elements.source.value.trim())
      .then(function (data) {
        if (updatedEl) updatedEl.textContent = updatedText(data);
        showStatus('✓ Раздел сохранён в исходный .json');
      })
      .catch(function (err) {
        showStatus('');
        showMsg(err && err.message ? err.message : 'Не удалось сохранить раздел');
      })
      .then(function () { if (saveBtn) saveBtn.disabled = false; });
  }

  if (profileForm) {
    profileForm.addEventListener('submit', function (e) {
      e.preventDefault();
      saveSection(profileForm, profileFields, API.adminSaveOrgProfile, profileUpdated);
    });
  }

  if (metricsForm) {
    metricsForm.addEventListener('submit', function (e) {
      e.preventDefault();
      saveSection(metricsForm, metricsFields, API.adminSaveOrgMetrics, metricsUpdated);
    });
  }

  /* aliases: добавить/убрать строку (делегирование по блоку профиля) */
  if (profileFields) {
    profileFields.addEventListener('click', function (e) {
      var addBtn = e.target.closest ? e.target.closest('.org-alias-add') : null;
      if (addBtn) {
        var field = addBtn.closest('.org-field');
        var box = field ? field.querySelector('.org-aliases') : null;
        if (box) {
          var row = document.createElement('div');
          row.className = 'org-alias-row';
          row.innerHTML = '<input type="text" placeholder="другое название организации">' +
            '<button class="org-alias-del" type="button" title="Убрать название">×</button>';
          box.appendChild(row);
          row.querySelector('input').focus();
        }
        return;
      }
      var delBtn = e.target.closest ? e.target.closest('.org-alias-del') : null;
      if (delBtn) {
        var rowEl = delBtn.closest('.org-alias-row');
        if (rowEl) rowEl.parentNode.removeChild(rowEl);
      }
    });
  }

  /* ---------- элемент 2: основные документы ---------- */

  function iconLabel(filename) {
    var ext = String(filename || '').split('.').pop().toUpperCase();
    return ext || 'FILE';
  }

  function docCard(d) {
    var ext = String(d.filename || '').split('.').pop().toLowerCase();
    var meta = [d.filename, fmtSize(d.size), d.uploaded_at].filter(Boolean).join(' · ');
    var summaryHtml = d.summary
      ? '<div class="org-doc-summary"><span class="org-doc-summary-title">Короткое .md-описание (читает ассистент)</span><pre>' + esc(d.summary) + '</pre></div>'
      : '<div class="org-doc-summary org-doc-summary--empty">Короткое .md-описание не заполнено</div>';
    return '<div class="org-doc" data-id="' + esc(d.id) + '">' +
      '<div class="org-doc-row">' +
      '<a class="org-doc-main" href="' + esc(d.file_url) + '" download="' + esc(d.filename) + '" title="Скачать ' + esc(d.filename) + '">' +
      '<span class="org-doc-icon" data-ext="' + esc(ext) + '">' + esc(iconLabel(d.filename)) + '</span>' +
      '<span class="org-doc-info"><span class="org-doc-title">' + esc(d.title) + '</span>' +
      '<span class="org-doc-meta">' + esc(meta) + '</span></span>' +
      '</a>' +
      '<div class="org-doc-side">' +
      '<button class="adm-btn" type="button" data-act="edit">Изменить</button>' +
      '<button class="adm-btn adm-btn--danger" type="button" data-act="del">Удалить</button>' +
      '</div>' +
      '</div>' +
      summaryHtml +
      '</div>';
  }

  function uploadForm(catId) {
    return '<form class="org-upload-form" hidden>' +
      '<label><span>Название</span>' +
      '<input type="text" name="title" maxlength="300" placeholder="например: Стратегия развития до 2030 года"></label>' +
      '<label><span>Короткое описание (.md) — основное содержание документа, его первым прочитает ассистент</span>' +
      '<textarea name="summary" rows="4" placeholder="## Основное содержание&#10;Ключевые цели и положения документа…"></textarea></label>' +
      '<label><span>Файл (pdf, doc, docx, xlsx, pptx, txt…)</span>' +
      '<input type="file" name="file" required accept=".pdf,.doc,.docx,.rtf,.odt,.xls,.xlsx,.csv,.ppt,.pptx,.txt,.md"></label>' +
      '<div class="org-actions">' +
      '<button class="btn-primary org-save" type="submit">Загрузить</button>' +
      '<button class="adm-btn org-upload-cancel" type="button">Отмена</button>' +
      '</div>' +
      '</form>';
  }

  function renderDocuments() {
    if (!docsGroups) return;
    var html = '';
    catOrder.forEach(function (catId) {
      var items = documents.filter(function (d) { return d.category === catId; });
      var listHtml = items.length
        ? items.map(docCard).join('')
        : '<p class="org-doc-empty">В категории пока нет документов</p>';
      html += '<div class="org-doc-group" data-cat="' + esc(catId) + '">' +
        '<div class="org-doc-group-head">' +
        '<h4 class="org-h4">' + esc(categories[catId] || catId) +
        '<span class="org-doc-count">' + items.length + '</span></h4>' +
        '<button class="adm-btn org-upload-toggle" type="button">Загрузить документ</button>' +
        '</div>' +
        '<div class="org-doc-list">' + listHtml + '</div>' +
        uploadForm(catId) +
        '</div>';
    });
    docsGroups.innerHTML = html;
  }

  function editForm(d) {
    var options = catOrder.map(function (catId) {
      var sel = catId === d.category ? ' selected' : '';
      return '<option value="' + esc(catId) + '"' + sel + '>' + esc(categories[catId] || catId) + '</option>';
    }).join('');
    return '<form class="org-edit-form">' +
      '<label><span>Название</span><input type="text" name="title" maxlength="300" value="' + esc(d.title) + '" required></label>' +
      '<label><span>Категория</span><select name="category">' + options + '</select></label>' +
      '<label><span>Короткое описание (.md) — основное содержание документа, его первым прочитает ассистент</span>' +
      '<textarea name="summary" rows="4">' + esc(d.summary || '') + '</textarea></label>' +
      '<div class="org-actions">' +
      '<button class="btn-primary" type="submit">Сохранить</button>' +
      '<button class="adm-btn org-edit-cancel" type="button">Отмена</button>' +
      '</div>' +
      '</form>';
  }

  function findDoc(id) {
    return documents.filter(function (d) { return d.id === id; })[0] || null;
  }

  if (docsGroups) {
    docsGroups.addEventListener('click', function (e) {
      var toggle = e.target.closest ? e.target.closest('.org-upload-toggle') : null;
      if (toggle) {
        var group = toggle.closest('.org-doc-group');
        var form = group ? group.querySelector('.org-upload-form') : null;
        if (form) {
          form.hidden = !form.hidden;
          if (!form.hidden) form.elements.title.focus();
        }
        return;
      }

      var cancel = e.target.closest ? e.target.closest('.org-upload-cancel') : null;
      if (cancel) {
        var g = cancel.closest('.org-doc-group');
        var f = g ? g.querySelector('.org-upload-form') : null;
        if (f) { f.reset(); f.hidden = true; }
        return;
      }

      var editCancel = e.target.closest ? e.target.closest('.org-edit-cancel') : null;
      if (editCancel) {
        renderDocuments(); /* возвращаем обычный вид карточки */
        return;
      }

      var btn = e.target.closest ? e.target.closest('button[data-act]') : null;
      if (!btn) return;
      var card = btn.closest('.org-doc');
      var docId = card ? card.getAttribute('data-id') : '';
      var doc = findDoc(docId);
      if (!doc) return;

      if (btn.getAttribute('data-act') === 'del') {
        if (!window.confirm('Удалить документ «' + (doc.title || doc.filename) + '»? Файл будет удалён из organization/objects.')) return;
        showStatus('');
        API.adminDeleteOrgDocument(session.token, docId)
          .then(load)
          .catch(function (err) { showMsg(err && err.message ? err.message : 'Не удалось удалить документ'); });
      } else if (btn.getAttribute('data-act') === 'edit') {
        var summaryBox = card.querySelector('.org-doc-summary');
        if (summaryBox) {
          summaryBox.innerHTML = editForm(doc);
          summaryBox.classList.add('org-doc-summary--edit');
          summaryBox.hidden = false;
        }
      }
    });

    /* отправка форм внутри блока документов: загрузка и правка */
    docsGroups.addEventListener('submit', function (e) {
      var uploadFormEl = e.target.closest ? e.target.closest('.org-upload-form') : null;
      if (uploadFormEl) {
        e.preventDefault();
        var group = uploadFormEl.closest('.org-doc-group');
        var catId = group ? group.getAttribute('data-cat') : '';
        var file = uploadFormEl.elements.file.files[0];
        if (!file) { showMsg('Выберите файл'); return; }
        showStatus('Загрузка файла…');
        var upBtn = uploadFormEl.querySelector('.org-save');
        if (upBtn) upBtn.disabled = true;
        API.adminUploadOrgDocument(
          session.token, catId,
          uploadFormEl.elements.title.value.trim(),
          uploadFormEl.elements.summary.value,
          file
        ).then(load).catch(function (err) {
          showStatus('');
          showMsg(err && err.message ? err.message : 'Не удалось загрузить документ');
        }).then(function () { if (upBtn) upBtn.disabled = false; });
        return;
      }

      var editFormEl = e.target.closest ? e.target.closest('.org-edit-form') : null;
      if (editFormEl) {
        e.preventDefault();
        var cardEl = editFormEl.closest('.org-doc');
        var id = cardEl ? cardEl.getAttribute('data-id') : '';
        showStatus('');
        API.adminUpdateOrgDocument(session.token, id, {
          title: editFormEl.elements.title.value.trim(),
          category: editFormEl.elements.category.value,
          summary: editFormEl.elements.summary.value
        }).then(load).catch(function (err) {
          showStatus('');
          showMsg(err && err.message ? err.message : 'Не удалось сохранить описание');
        });
      }
    });
  }

  /* ---------- инициализация ---------- */

  function load() {
    showMsg('');
    showStatus('');
    API.adminGetOrganization(session.token)
      .then(function (data) {
        categories = data.categories || {};
        catOrder = Object.keys(categories);
        documents = data.documents || [];

        if (typeEl) typeEl.textContent = 'тип: ' + ((data.profile && data.profile.type) || '—');
        renderProfileFields(data.profile || { fields: [] });
        renderMetricsFields(data.metrics || { items: [] });
        if (profileForm && data.profile) profileForm.elements.source.value = data.profile.source || '';
        if (metricsForm && data.metrics) metricsForm.elements.source.value = data.metrics.source || '';
        if (profileUpdated) profileUpdated.textContent = updatedText(data.profile);
        if (metricsUpdated) metricsUpdated.textContent = updatedText(data.metrics);
        renderDocuments();

        if (infoCard) infoCard.hidden = false;
        if (docsCard) docsCard.hidden = false;
        wrap.hidden = false;
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          try { localStorage.removeItem(AUTH_KEY); } catch (e) {}
          showMsg('Сессия истекла. Войдите заново под учётной записью администратора.');
        } else if (err && err.status === 403) {
          showMsg('Доступ только для администраторов.');
        } else {
          showMsg('Не удалось загрузить данные организации: ' + (err && err.message ? err.message : 'ошибка сети'));
        }
      });
  }

  load();
})();

