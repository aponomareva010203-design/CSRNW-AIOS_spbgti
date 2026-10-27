/* ============================================================
   Шаблоны агентов и конструктор MD-файла (страница «Агенты»).

   Шаблоны — карточки data/skills.json с source: "template"; их
   тела — data/skills/{id}.md единой структуры:
     frontmatter (name, description, triggers) +
     ## Назначение / ## Роль / ## Навыки (skills) /
     ## Входные данные / ## Выходные данные / ## Ограничения /
     ## Сценарий работы (необязательно).

   Конструктор разбирает выбранный шаблон на поля формы, а по
   кнопке «Скачать .md» собирает файл той же структуры обратно —
   его можно положить в data/skills/ и добавить карточку в
   data/skills.json, чтобы агент появился в каталоге.
   ============================================================ */

(function () {
  var grid = document.getElementById('tplGrid');
  var form = document.getElementById('tplForm');
  if (!grid || !form) return;

  var preview = document.getElementById('tplPreview');
  var baseSelect = document.getElementById('tplBase');
  var status = document.getElementById('tplStatus');

  /* Разделы MD-файла: заголовок → поле формы; list — пункты списком */
  var SECTIONS = [
    { field: 'purpose', title: 'Назначение' },
    { field: 'role', title: 'Роль' },
    { field: 'skills', title: 'Навыки (skills)', list: true },
    { field: 'inputs', title: 'Входные данные', list: true },
    { field: 'outputs', title: 'Выходные данные', list: true },
    { field: 'limits', title: 'Ограничения', list: true },
    { field: 'flow', title: 'Сценарий работы', list: true, ordered: true, optional: true }
  ];

  var templates = [];

  function setStatus(text, isError) {
    status.textContent = text || '';
    status.classList.toggle('is-error', !!isError);
  }

  /* ---------- разбор MD-шаблона ---------- */

  function parseFrontmatter(text) {
    var m = /^---\s*\n([\s\S]*?)\n---\s*\n?/.exec(text);
    var meta = { name: '', description: '', triggers: [] };
    if (!m) return { meta: meta, body: text };
    var key = null;
    m[1].split('\n').forEach(function (line) {
      var kv = /^([A-Za-z_]+):\s*(.*)$/.exec(line);
      if (kv) {
        key = kv[1];
        if (kv[2]) meta[key] = kv[2].trim();
        else if (key === 'triggers') meta.triggers = [];
        return;
      }
      var item = /^\s*-\s+(.*)$/.exec(line);
      if (item && key === 'triggers') meta.triggers.push(item[1].trim());
    });
    return { meta: meta, body: text.slice(m[0].length) };
  }

  /* Текст раздела → строки для формы: маркеры и нумерация снимаются */
  function sectionToField(text, isList) {
    var lines = text.trim().split('\n');
    if (!isList) return lines.join('\n').trim();
    var out = [];
    lines.forEach(function (line) {
      var t = line.trim();
      if (!t) return;
      var item = /^(?:[-*]|\d+\.)\s+(.*)$/.exec(t);
      if (item) out.push(item[1]);
      else if (out.length && /^\s+/.test(line)) out[out.length - 1] += ' ' + t;
      else out.push(t);
    });
    return out.join('\n');
  }

  function parseTemplate(text) {
    var fm = parseFrontmatter(text);
    var values = {
      name: fm.meta.name || '',
      description: fm.meta.description || '',
      triggers: (fm.meta.triggers || []).join(', ')
    };
    var parts = fm.body.split(/^##\s+/m).slice(1);
    parts.forEach(function (part) {
      var nl = part.indexOf('\n');
      var heading = (nl < 0 ? part : part.slice(0, nl)).trim();
      var content = nl < 0 ? '' : part.slice(nl + 1);
      var sec = SECTIONS.find(function (s) {
        return heading.toLowerCase().indexOf(s.title.split(' (')[0].toLowerCase()) === 0;
      });
      if (sec) values[sec.field] = sectionToField(content, sec.list);
    });
    return values;
  }

  /* ---------- сборка MD-файла ---------- */

  function slug(name) {
    var map = { а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'sch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya' };
    var s = String(name || '').toLowerCase().split('').map(function (c) {
      return map[c] !== undefined ? map[c] : c;
    }).join('').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    return s || 'my-agent';
  }

  function yamlValue(v) {
    var s = String(v || '').replace(/\s+/g, ' ').trim();
    return /[:#\[\]{}&*!|>'"%@`]/.test(s) ? '"' + s.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"' : s;
  }

  function values() {
    var out = {};
    Array.prototype.forEach.call(form.elements, function (f) {
      if (f.name) out[f.name] = f.value || '';
    });
    return out;
  }

  function buildMarkdown(v) {
    var name = v.name.trim() || 'Новый агент';
    var triggers = v.triggers.split(',').map(function (t) { return t.trim(); }).filter(Boolean);
    var lines = [
      '---',
      'name: ' + yamlValue(name),
      'description: ' + yamlValue(v.description),
      'stage: agents',
      'version: 1',
      'status: draft'
    ];
    if (triggers.length) {
      lines.push('triggers:');
      triggers.forEach(function (t) { lines.push('  - ' + yamlValue(t)); });
    }
    lines.push('---', '', '# Агент «' + name + '»', '');
    SECTIONS.forEach(function (sec) {
      var text = (v[sec.field] || '').trim();
      if (!text && sec.optional) return;
      lines.push('## ' + sec.title, '');
      if (!text) {
        lines.push('_Заполните раздел._');
      } else if (sec.list) {
        text.split('\n').map(function (l) { return l.trim(); }).filter(Boolean).forEach(function (l, i) {
          lines.push((sec.ordered ? (i + 1) + '. ' : '- ') + l);
        });
      } else {
        lines.push(text);
      }
      lines.push('');
    });
    return lines.join('\n');
  }

  function refresh() {
    preview.textContent = buildMarkdown(values());
  }

  function fill(v) {
    Array.prototype.forEach.call(form.elements, function (f) {
      if (f.name) f.value = v[f.name] || '';
    });
    refresh();
  }

  /* ---------- загрузка шаблонов ---------- */

  function loadTemplate(t) {
    setStatus('Загружаю шаблон «' + t.name + '»…');
    return fetch(t.file_url)
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.text();
      })
      .then(function (text) {
        fill(parseTemplate(text));
        baseSelect.value = t.id;
        setStatus('Шаблон «' + t.name + '» загружен — впишите особенности своего вуза.');
      })
      .catch(function () {
        setStatus('Не удалось загрузить шаблон. Проверьте, что платформа запущена через сервер.', true);
      });
  }

  function renderCards() {
    grid.innerHTML = '';
    templates.forEach(function (t) {
      var card = document.createElement('div');
      card.className = 'tpl-card';
      var h = document.createElement('h3');
      h.textContent = t.name;
      var p = document.createElement('p');
      p.textContent = t.description_ru || '';
      var actions = document.createElement('div');
      actions.className = 'tpl-card-actions';
      var dl = document.createElement('a');
      dl.className = 'btn-primary mm-btn';
      dl.href = t.file_url;
      dl.setAttribute('download', t.file_url.split('/').pop());
      dl.textContent = 'Скачать .md';
      var edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'btn-primary mm-btn';
      edit.textContent = 'Открыть в конструкторе';
      edit.addEventListener('click', function () {
        loadTemplate(t).then(function () {
          document.getElementById('builder').scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      });
      actions.appendChild(dl);
      actions.appendChild(edit);
      card.appendChild(h);
      card.appendChild(p);
      card.appendChild(actions);
      grid.appendChild(card);

      var opt = document.createElement('option');
      opt.value = t.id;
      opt.textContent = t.name;
      baseSelect.appendChild(opt);
    });
  }

  fetch('/data/skills.json', { headers: { 'Accept': 'application/json' } })
    .then(function (res) { return res.json(); })
    .then(function (data) {
      templates = ((data && data.items) || []).filter(function (a) { return a.source === 'template'; });
      renderCards();
      if (templates.length) loadTemplate(templates[0]);
    })
    .catch(function () {
      grid.textContent = 'Не удалось загрузить каталог шаблонов.';
    });

  /* ---------- события формы ---------- */

  form.addEventListener('input', refresh);

  baseSelect.addEventListener('change', function () {
    var t = templates.find(function (x) { return x.id === baseSelect.value; });
    if (t) loadTemplate(t);
    else { fill({}); setStatus('Пустой шаблон: заполните поля.'); }
  });

  document.getElementById('tplDownload').addEventListener('click', function () {
    var v = values();
    if (!v.name.trim()) {
      setStatus('Укажите название агента — из него формируется имя файла.', true);
      form.elements.name.focus();
      return;
    }
    var blob = new Blob([buildMarkdown(v)], { type: 'text/markdown;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = slug(v.name) + '.md';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    setStatus('Файл ' + a.download + ' сохранён. Положите его в data/skills/ и добавьте карточку в data/skills.json (см. README).');
  });

  document.getElementById('tplCopy').addEventListener('click', function () {
    var text = buildMarkdown(values());
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { setStatus('Текст агента скопирован.'); },
        function () { setStatus('Не удалось скопировать — выделите текст справа вручную.', true); });
    } else {
      setStatus('Буфер обмена недоступен — выделите текст справа вручную.', true);
    }
  });

  /* Доработка в диалоге: тот же путь, что «Запустить в рабочем чате»
     (workspace.js слушает aios-launch-skill); без входа чат попросит войти */
  document.getElementById('tplChat').addEventListener('click', function () {
    document.dispatchEvent(new CustomEvent('aios-launch-skill', {
      detail: {
        skill_id: 'agent-builder',
        name: 'Конструктор агентов',
        prompt: 'Помоги доработать агента для управления инновациями в нашем университете. Вот мой черновик:\n\n' + buildMarkdown(values())
      }
    }));
  });

  refresh();
})();
