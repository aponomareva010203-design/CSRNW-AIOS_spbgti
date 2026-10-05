/* ============================================================
   Каталог компонентов ответа агента (frontend/js/agent_views.js)

   Агент не описывает интерфейс — он выбирает компонент из этого
   каталога и наполняет его данными. Фронтенд владеет отрисовкой:
   агент не может испортить вёрстку, вставить скрипт или увести
   стиль в сторону. Это паттерн Static Generative UI (AG-UI /
   CopilotKit, 2026): свобода в данных, контроль в представлении.

   Соглашение — блок кода с языком aios-view, продолжение уже
   существующих на платформе идиом aios-choices и SVG-блока:

       ```aios-view
       {"component":"calc_trace", …}
       ```

   Один каталог отрисовывается в трёх местах: в чате (workspace.js),
   на странице агента (agent_page.js) и в сохранённой записи. Поэтому
   модуль ничего не знает ни о чате, ни о странице — только строит HTML.

   Подключение:
       <script src="/js/agent_views.js?v=20260924a"></script>
       AiosViews.enhance(container);   // после вставки текста в DOM

   Безопасность: любое значение проходит esc(); ни один компонент не
   вставляет HTML из данных. Исключение одно — svg в компоненте chart,
   и он чистится тем же способом, что уже применяется в workspace.js.
   ============================================================ */

(function (global) {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function sanitizeSvg(raw) {
    return String(raw)
      .replace(/<script[\s\S]*?<\/script>/gi, '')
      .replace(/\son\w+\s*=\s*"[^"]*"/gi, '')
      .replace(/\son\w+\s*=\s*'[^']*'/gi, '')
      .replace(/\bxlink:href\s*=\s*"(?!#)[^"]*"/gi, '')
      .replace(/\bhref\s*=\s*"(?!#)[^"]*"/gi, '');
  }

  function num(v) {
    if (typeof v === 'number') return v;
    var f = parseFloat(String(v).replace(',', '.'));
    return isNaN(f) ? null : f;
  }

  /* Статусы пришли от модели — приводим к трём известным, чтобы в CSS
     не появлялись классы из данных. */
  var STATUS = {
    ok: 'ok', warn: 'warn', fail: 'fail', good: 'ok', bad: 'fail', warning: 'warn',
    pass: 'ok', passed: 'ok', success: 'ok', done: 'ok', yes: 'ok',
    'соответствует': 'ok', 'да': 'ok', 'годится': 'ok',
    error: 'fail', failed: 'fail', no: 'fail', 'не соответствует': 'fail', 'нет': 'fail',
    pending: 'warn', check: 'warn', 'требует проверки': 'warn'
  };
  function status(s) { return STATUS[String(s || '').toLowerCase().trim()] || 'neutral'; }

  /* Первое непустое из нескольких возможных имён поля.

     Модель наполняет компонент по описанию, а не по схеме: вместо text
     приходит label, вместо items — list, вместо sources — references.
     Раньше такой ответ отрисовывался пустой рамкой — на странице агента
     висел чек-лист из пустых строк с галочками, и понять, что произошло,
     было нельзя. Разбирать синонимы дешевле, чем требовать от модели
     точного попадания в схему на каждом ответе. */
  function pick(obj, names, fallback) {
    if (obj == null) return fallback;
    if (typeof obj !== 'object') return obj;          /* пришла строка вместо объекта */
    for (var i = 0; i < names.length; i++) {
      var v = obj[names[i]];
      if (v !== undefined && v !== null && v !== '') return v;
    }
    return fallback;
  }

  /* Список, каким бы именем его ни назвали, и без пустых элементов. */
  function list(d, names) {
    var raw = pick(d, names, []);
    if (!Array.isArray(raw)) raw = raw ? [raw] : [];
    return raw.filter(function (x) {
      return x !== null && x !== undefined && x !== '';
    });
  }

  function head(title, subtitle) {
    if (!title && !subtitle) return '';
    return '<div class="av-head">' +
      (title ? '<h4 class="av-title">' + esc(title) + '</h4>' : '') +
      (subtitle ? '<p class="av-sub">' + esc(subtitle) + '</p>' : '') +
      '</div>';
  }

  /* ---------------------------------------------------------------
     1. metric_cards — крупные числа с единицами и отклонением
     {title, items:[{label, value, unit, delta, status, note}]}     */

  function metricCards(d) {
    var items = (d.items || []).slice(0, 8).map(function (m) {
      var st = status(m.status);
      return '<div class="av-metric av-metric--' + st + '">' +
        '<div class="av-metric-value">' + esc(m.value) +
          (m.unit ? '<span class="av-metric-unit">' + esc(m.unit) + '</span>' : '') +
        '</div>' +
        '<div class="av-metric-label">' + esc(m.label) + '</div>' +
        (m.delta ? '<div class="av-metric-delta">' + esc(m.delta) + '</div>' : '') +
        (m.note ? '<div class="av-metric-note">' + esc(m.note) + '</div>' : '') +
        '</div>';
    }).join('');
    return head(d.title, d.subtitle) + '<div class="av-metrics">' + items + '</div>';
  }

  /* ---------------------------------------------------------------
     2. data_table — таблица с единицами, подсветкой лучшей строки
        и выгрузкой в CSV
     {title, columns:[{key,label,unit,align}], rows:[{…}],
      highlight_row: 0, footnote}                                    */

  function dataTable(d) {
    var data = list(d, ['rows', 'items', 'data', 'records']);
    var cols = list(d, ['columns', 'cols', 'headers']).map(function (c) {
      return typeof c === 'string' ? { key: c, label: c } : c;
    });
    if (!cols.length && data.length && typeof data[0] === 'object') {
      cols = Object.keys(data[0]).map(function (k) { return { key: k, label: k }; });
    }
    if (!cols.length) return '';        /* не из чего строить таблицу */
    d = { title: d.title, subtitle: d.subtitle, rows: data,
          highlight_row: d.highlight_row, footnote: d.footnote };
    var thead = cols.map(function (c) {
      return '<th>' + esc(c.label || c.key) +
        (c.unit ? '<span class="av-unit">' + esc(c.unit) + '</span>' : '') + '</th>';
    }).join('');

    var rows = (d.rows || []).slice(0, 200).map(function (r, i) {
      var cls = (i === d.highlight_row) ? ' class="av-row--best"' : '';
      var cells = cols.map(function (c) {
        var v = r[c.key];
        var isNum = num(v) !== null && String(v).trim() !== '';
        return '<td' + (isNum ? ' class="av-num"' : '') + '>' + esc(v == null ? '' : v) + '</td>';
      }).join('');
      return '<tr' + cls + '>' + cells + '</tr>';
    }).join('');

    var csv = [cols.map(function (c) { return c.label || c.key; }).join(';')]
      .concat((d.rows || []).map(function (r) {
        return cols.map(function (c) { return String(r[c.key] == null ? '' : r[c.key]); }).join(';');
      })).join('\n');

    return head(d.title, d.subtitle) +
      '<div class="av-table-wrap"><table class="av-table">' +
      '<thead><tr>' + thead + '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
      (d.footnote ? '<p class="av-note">' + esc(d.footnote) + '</p>' : '') +
      '<button type="button" class="av-csv" data-csv="' + esc(csv) + '" ' +
      'data-name="' + esc(d.title || 'таблица') + '">Скачать CSV</button>';
  }

  /* ---------------------------------------------------------------
     3. chart — график; либо готовый svg, либо данные для CSS-столбиков
     {title, svg} | {title, style:'bar'|'line', labels:[], series:[{name,values}]} */

  function chart(d) {
    if (d.svg) {
      return head(d.title, d.subtitle) +
        '<div class="av-chart">' + sanitizeSvg(d.svg) + '</div>';
    }
    var series = d.series || [{ name: '', values: d.values || [] }];
    var all = series.reduce(function (a, s) { return a.concat(s.values || []); }, []).map(num)
      .filter(function (v) { return v !== null; });
    var max = Math.max.apply(null, all.concat([1]));
    var bars = (d.labels || []).map(function (label, i) {
      var stack = series.map(function (s, si) {
        var v = num((s.values || [])[i]) || 0;
        return '<span class="av-bar av-bar--' + (si % 4) + '" ' +
          'style="height:' + Math.max(3, Math.round(v / max * 100)) + '%" ' +
          'title="' + esc((s.name ? s.name + ': ' : '') + v) + '"></span>';
      }).join('');
      return '<div class="av-bar-col"><div class="av-bar-stack">' + stack + '</div>' +
        '<span class="av-bar-label">' + esc(label) + '</span></div>';
    }).join('');
    var legend = series.length > 1
      ? '<div class="av-legend">' + series.map(function (s, si) {
          return '<span class="av-legend-item"><i class="av-dot av-dot--' + (si % 4) + '"></i>' +
            esc(s.name) + '</span>';
        }).join('') + '</div>'
      : '';
    return head(d.title, d.subtitle) + '<div class="av-bars">' + bars + '</div>' + legend;
  }

  /* ---------------------------------------------------------------
     4. comparison — варианты рядом, с оценкой и рекомендацией
     {title, options:[{name, score, pros:[], cons:[], values:{…},
                       recommended:bool}], criteria:[…]}            */

  function comparison(d) {
    var cards = (d.options || []).slice(0, 5).map(function (o) {
      var stars = '';
      var score = num(o.score);
      if (score !== null) {
        var full = Math.round(Math.max(0, Math.min(5, score)));
        stars = '<div class="av-score" aria-label="оценка ' + full + ' из 5">' +
          new Array(full + 1).join('★') + new Array(6 - full).join('☆') + '</div>';
      }
      var values = Object.keys(o.values || {}).map(function (k) {
        return '<div class="av-kv"><span>' + esc(k) + '</span><b>' + esc(o.values[k]) + '</b></div>';
      }).join('');
      var pros = (o.pros || []).map(function (p) {
        return '<li class="av-pro">' + esc(p) + '</li>';
      }).join('');
      var cons = (o.cons || []).map(function (c) {
        return '<li class="av-con">' + esc(c) + '</li>';
      }).join('');
      return '<div class="av-option' + (o.recommended ? ' av-option--best' : '') + '">' +
        (o.recommended ? '<span class="av-flag">рекомендуется</span>' : '') +
        '<h5>' + esc(o.name) + '</h5>' + stars + values +
        ((pros || cons) ? '<ul class="av-prcs">' + pros + cons + '</ul>' : '') +
        '</div>';
    }).join('');
    return head(d.title, d.subtitle) + '<div class="av-options">' + cards + '</div>' +
      (d.recommendation ? '<p class="av-reco">' + esc(d.recommendation) + '</p>' : '');
  }

  /* ---------------------------------------------------------------
     5. calc_trace — ГЛАВНЫЙ компонент платформы: показывает расчёт
        так, чтобы инженер мог его проверить и принять
     {title, inputs:{…}, formula, code, result:{value,unit},
      steps:[{label,value}], verdict:{status,text}, source}          */

  function calcTrace(d) {
    var inputs = Object.keys(d.inputs || {}).map(function (k) {
      return '<div class="av-kv"><span>' + esc(k) + '</span><b>' + esc(d.inputs[k]) + '</b></div>';
    }).join('');
    var steps = (d.steps || []).map(function (s) {
      return '<div class="av-step"><span>' + esc(s.label) + '</span>' +
        '<b>' + esc(s.value) + '</b></div>';
    }).join('');
    var res = d.result || {};
    var verdict = d.verdict
      ? '<div class="av-verdict av-verdict--' + status(d.verdict.status) + '">' +
        esc(d.verdict.text) + '</div>'
      : '';
    var code = d.code
      ? '<details class="av-code"><summary>Показать расчёт</summary>' +
        '<pre>' + esc(d.code) + '</pre></details>'
      : '';
    return '<div class="av-calc">' +
      head(d.title, d.subtitle) +
      (inputs ? '<div class="av-calc-inputs"><span class="av-calc-cap">Исходные данные</span>' +
        inputs + '</div>' : '') +
      (d.formula ? '<div class="av-formula">' + esc(d.formula) + '</div>' : '') +
      (steps ? '<div class="av-calc-steps">' + steps + '</div>' : '') +
      (res.value != null
        ? '<div class="av-result"><span class="av-result-value">' + esc(res.value) + '</span>' +
          (res.unit ? '<span class="av-result-unit">' + esc(res.unit) + '</span>' : '') +
          (res.label ? '<span class="av-result-label">' + esc(res.label) + '</span>' : '') +
          '</div>'
        : '') +
      verdict + code +
      (d.source ? '<p class="av-note">Метод: ' + esc(d.source) + '</p>' : '') +
      '</div>';
  }

  /* ---------------------------------------------------------------
     6. spec_sheet — паспорт: параметр → значение → источник
     {title, groups:[{name, rows:[{param, value, unit, source, status}]}]} */

  function specSheet(d) {
    var specs = (d.groups || [{ name: '', rows: d.rows || [] }]);
    var groups = specs.map(function (g) {
      var rows = (g.rows || []).map(function (r) {
        return '<tr class="av-spec-row--' + status(r.status) + '">' +
          '<td>' + esc(r.param) + '</td>' +
          '<td class="av-num"><b>' + esc(r.value) + '</b>' +
            (r.unit ? ' <span class="av-unit">' + esc(r.unit) + '</span>' : '') + '</td>' +
          '<td class="av-src">' + esc(r.source || '') + '</td></tr>';
      }).join('');
      return (g.name ? '<h5 class="av-group">' + esc(g.name) + '</h5>' : '') +
        '<table class="av-table av-table--spec"><tbody>' + rows + '</tbody></table>';
    }).join('');
    /* CSV как у таблиц: паспорт часто уходит в переписку с заказчиком */
    var csv = ['группа;параметр;значение;единица;источник;статус'];
    specs.forEach(function (g) {
      (g.rows || []).forEach(function (r) {
        csv.push([g.name || '', r.param || '', r.value == null ? '' : r.value,
                  r.unit || '', r.source || '', r.status || ''].join(';'));
      });
    });
    return head(d.title, d.subtitle) + '<div class="av-spec">' + groups + '</div>' +
      '<button type="button" class="av-csv" data-csv="' + esc(csv.join('\n')) + '" ' +
      'data-name="' + esc(d.title || 'паспорт') + '">Скачать CSV</button>';
  }

  /* ---------------------------------------------------------------
     7. checklist — шаги со статусом; поля done можно отмечать
     {title, items:[{text, done, status, note}]}                     */

  function checklist(d) {
    var items = list(d, ['items', 'list', 'checks', 'steps', 'points']).map(function (it) {
      var text = pick(it, ['text', 'label', 'title', 'item', 'name', 'question'], '');
      if (!String(text).trim()) return '';        /* пустая строка с галочкой бесполезна */
      var st = status(pick(it, ['status', 'state'], ''));
      var done = !!pick(it, ['done', 'checked', 'completed'], false);
      var note = pick(it, ['note', 'comment', 'detail', 'hint'], '');
      return '<li class="av-check av-check--' + st + (done ? ' av-check--done' : '') + '">' +
        '<span class="av-check-box">' + (done ? '✓' : '') + '</span>' +
        '<span class="av-check-text">' + esc(text) + '</span>' +
        (note ? '<span class="av-check-note">' + esc(note) + '</span>' : '') +
        '</li>';
    }).join('');
    if (!items) return '';        /* нечего показывать — не рисуем пустую рамку */
    return head(d.title, d.subtitle) + '<ul class="av-checklist">' + items + '</ul>';
  }

  /* ---------------------------------------------------------------
     8. timeline — план или процесс со стадиями
     {title, steps:[{label, detail, status, meta}]}                  */

  function timeline(d) {
    var steps = (d.steps || []).map(function (s) {
      return '<li class="av-tl-step av-tl-step--' + status(s.status) + '">' +
        '<div class="av-tl-dot"></div>' +
        '<div class="av-tl-body"><b>' + esc(s.label) + '</b>' +
        (s.meta ? '<span class="av-tl-meta">' + esc(s.meta) + '</span>' : '') +
        (s.detail ? '<p>' + esc(s.detail) + '</p>' : '') + '</div></li>';
    }).join('');
    return head(d.title, d.subtitle) + '<ul class="av-timeline">' + steps + '</ul>';
  }

  /* ---------------------------------------------------------------
     9. source_list — источники со ссылками и относимостью
     {title, sources:[{title, authors, year, journal, citations,
                       doi, url, relevance, note}]}                  */

  function sourceList(d) {
    var items = (d.sources || []).slice(0, 25).map(function (s) {
      var url = s.url || (s.doi ? 'https://doi.org/' + String(s.doi).replace(/^https?:\/\/doi\.org\//, '') : '');
      var safeUrl = /^https?:\/\//i.test(url) ? url : '';
      var title = safeUrl
        ? '<a href="' + esc(safeUrl) + '" target="_blank" rel="noopener noreferrer">' + esc(s.title) + '</a>'
        : esc(s.title);
      var meta = [s.authors && [].concat(s.authors).slice(0, 3).join(', '), s.journal, s.year]
        .filter(Boolean).join(' · ');
      return '<li class="av-source">' +
        '<div class="av-source-title">' + title + '</div>' +
        (meta ? '<div class="av-source-meta">' + esc(meta) +
          (s.citations != null ? ' · цитирований: ' + esc(s.citations) : '') + '</div>' : '') +
        (s.note ? '<div class="av-source-note">' + esc(s.note) + '</div>' : '') +
        '</li>';
    }).join('');
    return head(d.title, d.subtitle) + '<ol class="av-sources">' + items + '</ol>';
  }

  /* ---------------------------------------------------------------
     10. file_card — готовый файл на скачивание
     {title, filename, size, url, kind, note}                        */

  function fileCard(d) {
    var ICONS = { docx: '📄', xlsx: '📊', pdf: '📕', csv: '📋', png: '🖼', svg: '🖼' };
    /* Модель не видит полей компонента и нередко вкладывает их в объект file
       ({file: {name, size_kb, url}}) или отдаёт один attachment_id. Раньше
       такая карточка рисовалась без адреса и без кнопки «Скачать»: файл
       создан, а взять его нельзя. Принимаем все эти формы. */
    var f = (d.file && typeof d.file === 'object') ? d.file : {};
    var url = String(d.url || f.url || d.href || f.href || d.download_url || f.download_url || '');
    var attId = String(d.attachment_id || f.attachment_id || f.id || '');
    if (!url && /^att-[\w-]+$/.test(attId)) url = '/api/chat/attachments/' + attId;
    if (url.indexOf(window.location.origin + '/api/') === 0) url = url.slice(window.location.origin.length);
    var sizeKb = d.size_kb != null ? d.size_kb : f.size_kb;
    d = {
      title: d.title,
      filename: d.filename || f.filename || f.name || d.name || '',
      size: d.size || f.size || (sizeKb != null && sizeKb !== '' ? String(sizeKb).replace('.', ',') + ' КБ' : ''),
      url: url,
      kind: d.kind || f.kind,
      note: d.note
    };
    var kind = String(d.kind || (d.filename || '').split('.').pop() || '').toLowerCase();
    var safeUrl = /^\/api\//.test(d.url || '') ? d.url : '';
    /* PNG (рисунки структур chem_depict, графики расчётов) показываем
       прямо в карточке. Сама картинка приходит не через src, а через
       fetch с Bearer-токеном — см. hydrateFileMedia: эндпоинт вложений
       без заголовка авторизации отвечает 401, <img src> его не передаст. */
    var preview = (kind === 'png' && safeUrl)
      ? '<img class="av-file-preview" data-src="' + esc(safeUrl) + '" alt="' +
        esc(d.filename || d.title || 'изображение') + '">'
      : '';
    return '<div class="av-file">' +
      preview +
      '<span class="av-file-icon">' + (ICONS[kind] || '📎') + '</span>' +
      '<div class="av-file-body"><b>' + esc(d.filename || d.title || 'файл') + '</b>' +
      (d.size ? '<span class="av-file-size">' + esc(d.size) + '</span>' : '') +
      (d.note ? '<p class="av-note">' + esc(d.note) + '</p>' : '') + '</div>' +
      (safeUrl ? '<a class="av-file-dl" href="' + esc(safeUrl) + '" download ' +
        'data-filename="' + esc(d.filename || 'файл') + '">Скачать</a>' : '') +
      '</div>';
  }

  /* Картинки-превью и скачивание файлов требуют Bearer-токена: вложения
     отдаются только владельцу. Обычные <img src>/<a href> заголовок не
     передают, поэтому наполняем их через fetch → Blob URL (как bindCsv).
     Модуль по-прежнему не знает про чат: токен читается из localStorage
     тем же способом, что и в api.js. */
  function fileAuthToken() {
    try {
      var raw = localStorage.getItem('aios-auth-v1');
      var s = raw ? JSON.parse(raw) : null;
      return s && s.token ? s.token : null;
    } catch (e) {
      return null;
    }
  }

  function fetchAuthorized(url) {
    var headers = {};
    var token = fileAuthToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    return fetch(url, { headers: headers });
  }

  /* Кэш blob-URL вложений: чат перерисовывает сообщение при каждом
     delta-куске SSE, и без кэша одна и та же картинка качалась бы
     заново на каждый кусок. Отказ в кэш не прячем — следующий рендер
     попробует снова (токен мог появиться после входа). */
  var ATT_MEDIA_CACHE = {};

  function authorizedMedia(url) {
    if (!ATT_MEDIA_CACHE[url]) {
      ATT_MEDIA_CACHE[url] = fetchAuthorized(url).then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        /* настоящее имя файла сообщает сервер (Content-Disposition): ссылку в
           ответе модель называет как угодно */
        var name = dispositionName(res.headers.get('Content-Disposition'));
        return res.blob().then(function (blob) {
          return { blob: blob, url: URL.createObjectURL(blob), name: name };
        });
      });
      ATT_MEDIA_CACHE[url].catch(function () { delete ATT_MEDIA_CACHE[url]; });
    }
    return ATT_MEDIA_CACHE[url];
  }

  var MIME_EXT = {
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/pdf': '.pdf',
    'text/csv': '.csv',
    'text/markdown': '.md',
    'image/png': '.png'
  };
  var MAX_DOWNLOAD_NAME = 150;

  /* Имя из заголовка Content-Disposition (filename*=UTF-8''… или filename="…") */
  function dispositionName(header) {
    var h = String(header || '');
    var m = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(h);
    if (m) {
      try { return decodeURIComponent(m[1].trim()); } catch (e) { /* ниже — обычная форма */ }
    }
    m = /filename\s*=\s*"?([^";]+)"?/.exec(h);
    return m ? m[1].trim() : '';
  }

  /* Имя скачиваемого файла. Раньше имя из текста ссылки обрезалось до 80
     символов вместе с расширением («….docx» → «….d»), и файл не открывался.
     Теперь расширение восстанавливается по типу файла и никогда не режется:
     сокращается только основа имени. */
  function downloadName(text, blobType) {
    var name = String(text || '').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, '_').trim();
    var ext = MIME_EXT[String(blobType || '').split(';')[0].trim().toLowerCase()] || '';
    if (!ext) {
      var m = /(\.[A-Za-z0-9]{1,5})$/.exec(name);
      ext = m ? m[1] : '';
    }
    if (ext && name.toLowerCase().slice(-ext.length) === ext.toLowerCase()) name = name.slice(0, -ext.length);
    name = name.replace(/[. ]+$/, '');
    return (name || 'файл').slice(0, MAX_DOWNLOAD_NAME - ext.length) + ext;
  }

  function saveBlobAs(blobUrl, filename, blobType) {
    var save = document.createElement('a');
    save.href = blobUrl;
    save.download = downloadName(filename, blobType);
    save.click();
  }

  /* Модель нередко отдаёт адрес вложения «голым» текстом — в скобках
     после слов «ссылка …», списком полей в код-блоке, без markdown и
     без file_card. Превью и скачивание цепляются только к настоящим
     <a>, поэтому сначала превращаем такие адреса в ссылки: дальше их
     подхватят блоки 2-3 hydrateFileMedia (клик скачает файл, а перед
     ссылкой встанет сама картинка). Идентификатор — строго att-hex,
     как в ATTACHMENT_ID_RE на сервере, иначе regex цеплял бы хвосты
     пунктуации. */
  function autolinkAttachmentUrls(root) {
    var ATT_URL_RE = /\/api\/chat\/attachments\/(att-[a-f0-9]{8,12})/g;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    var nodes = [];
    while (walker.nextNode()) {
      var n = walker.currentNode;
      if (n.parentNode && n.parentNode.nodeName !== 'A' &&
          n.nodeValue && n.nodeValue.indexOf('/api/chat/attachments/') !== -1) {
        nodes.push(n);
      }
    }
    nodes.forEach(function (n) {
      var text = n.nodeValue;
      var frag = document.createDocumentFragment();
      var last = 0, m;
      ATT_URL_RE.lastIndex = 0;
      while ((m = ATT_URL_RE.exec(text))) {
        if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        var a = document.createElement('a');
        a.href = '/api/chat/attachments/' + m[1];
        a.textContent = m[0];
        frag.appendChild(a);
        last = m.index + m[0].length;
      }
      if (!last) return;        /* пустое совпадение быть не может, но вдруг */
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      n.parentNode.replaceChild(frag, n);
    });
  }

  function hydrateFileMedia(root) {
    /* 0) «голые» текстовые ссылки на вложения → настоящие <a> (см. выше);
       блоки 2-3 ниже сработают по ним в этом же вызове */
    autolinkAttachmentUrls(root);

    /* 1) превью внутри file_card (data-src без src — см. fileCard) */
    Array.prototype.forEach.call(
      root.querySelectorAll('img.av-file-preview[data-src]'),
      function (img) {
        if (img.dataset.done) return;
        img.dataset.done = '1';
        authorizedMedia(img.dataset.src).then(function (m) {
          img.src = m.url;
          img.classList.add('av-file-preview--loaded');
        }).catch(function () {
          /* не скачалось (нет входа, вложение удалено) — остаётся карточка
             с именем и кнопкой, картинка убирается */
          if (img.parentNode) img.parentNode.removeChild(img);
        });
      });

    /* 2) кнопка «Скачать» в file_card */
    Array.prototype.forEach.call(
      root.querySelectorAll('a.av-file-dl[href^="/api/"]'),
      function (a) {
        if (a.dataset.bound) return;
        a.dataset.bound = '1';
        a.addEventListener('click', function (ev) {
          ev.preventDefault();
          authorizedMedia(a.getAttribute('href')).then(function (m) {
            saveBlobAs(m.url, m.name || a.dataset.filename || 'файл', m.blob.type);
          }).catch(function () {
            window.location.href = a.getAttribute('href');
          });
        });
      });

    /* 3) обычные markdown-ссылки на вложения: модель оформляет файл то
       file_card, то просто [имя](/api/chat/attachments/…) — оба варианта
       должны открываться. Клик качает blob с токеном; если вложение
       оказалось картинкой — добавляем превью перед ссылкой (у URL нет
       расширения, тип узнаём по Content-Type ответа). */
    Array.prototype.forEach.call(
      root.querySelectorAll('a[href^="/api/chat/attachments/"]'),
      function (a) {
        /* «Скачать» внутри file_card уже обеспечена превью блоком 1;
           чипы вложений пользователя компактные по дизайну — их не трогаем */
        if (a.classList.contains('av-file-dl') ||
            a.classList.contains('ws-attach-chip')) return;
        var href = a.getAttribute('href');
        if (!a.dataset.avMedia) {
          a.dataset.avMedia = '1';
          a.addEventListener('click', function (ev) {
            ev.preventDefault();
            authorizedMedia(href).then(function (m) {
              saveBlobAs(m.url, m.name || (a.textContent || 'файл').trim(), m.blob.type);
            }).catch(function () {
              window.open(href, '_blank');
            });
          });
        }
        /* Флаг ставится СИНХРОННО, до асинхронной вставки: гидрация
           вызывается из нескольких точек рендера, и без синхронного
           флага каждая успевала бы запланировать свою картинку —
           дублирование, которое мы наблюдали. */
        if (!a.dataset.avPreview) {
          a.dataset.avPreview = '1';
          authorizedMedia(href).then(function (m) {
            if (!m.blob.type || String(m.blob.type).indexOf('image/') !== 0) return;
            var img = document.createElement('img');
            img.className = 'av-file-preview av-file-preview--loaded';
            img.alt = (a.textContent || 'изображение').trim();
            img.src = m.url;
            if (a.parentNode) a.parentNode.insertBefore(img, a);
          }).catch(function () {
            a.dataset.avPreview = '';
          });
        }
      });
  }

  /* ---------------------------------------------------------------
     11. verdict — заключение агента по объекту: сводный статус,
         построчная проверка критериев, что исправить, на чём основано

     Второй по важности компонент после calc_trace и единственный, который
     отвечает на вопрос инженера целиком. «Площадь 92,4 м²» — это число;
     «по площади проходит, по перепаду давления нет, вот чем закрыть» —
     это решение. Разница между расчётчиком и инженером ровно здесь, и
     страница агента должна её показывать.

     {title, status:'ok'|'warn'|'fail', headline, summary,
      checks:[{label, value, status, status_label, note}],
      fix:{title, text, action},   ← action становится кнопкой: её текст
                                      уходит агенту обычным вопросом
      sources:[{title, note, date, url}]}                            */

  var VERDICT_HEADLINE = { ok: 'Соответствует', warn: 'Есть замечания', fail: 'Не соответствует' };
  var CHECK_LABEL = { ok: 'Соответствует', warn: 'Требует подтверждения', fail: 'Не соответствует' };

  function verdict(d) {
    var st = status(d.status);
    var banner = '<div class="av-vd-banner av-vd-banner--' + st + '">' +
      '<span class="av-vd-sign" aria-hidden="true"></span>' +
      '<div><b>' + esc(d.headline || VERDICT_HEADLINE[st] || 'Заключение') + '</b>' +
      (d.summary ? '<p>' + esc(d.summary) + '</p>' : '') + '</div></div>';

    var checks = (d.checks || []).slice(0, 12).map(function (c) {
      var cst = status(c.status);
      return '<div class="av-vd-check av-vd-check--' + cst + '">' +
        '<span class="av-vd-dot" aria-hidden="true"></span>' +
        '<div class="av-vd-check-body"><b>' + esc(c.label) + '</b>' +
        (c.value ? '<span class="av-vd-value">' + esc(c.value) + '</span>' : '') +
        (c.note ? '<span class="av-vd-note">' + esc(c.note) + '</span>' : '') + '</div>' +
        '<span class="av-vd-chip av-vd-chip--' + cst + '">' +
        esc(c.status_label || CHECK_LABEL[cst] || '—') + '</span></div>';
    }).join('');

    var fix = '';
    if (d.fix && (d.fix.text || d.fix.action)) {
      fix = '<div class="av-vd-fix">' +
        '<b>' + esc(d.fix.title || 'Что нужно исправить') + '</b>' +
        (d.fix.text ? '<p>' + esc(d.fix.text) + '</p>' : '') +
        (d.fix.action
          ? '<button type="button" class="av-ask" data-ask="' + esc(d.fix.action) + '">' +
            esc(d.fix.label || 'Исправить') + '</button>'
          : '') + '</div>';
    }

    var sources = (d.sources || []).slice(0, 8).map(function (s) {
      var safeUrl = /^https?:\/\//i.test(s.url || '') ? s.url : '';
      var name = safeUrl
        ? '<a href="' + esc(safeUrl) + '" target="_blank" rel="noopener noreferrer">' + esc(s.title) + '</a>'
        : esc(s.title);
      return '<li class="av-vd-src"><div><b>' + name + '</b>' +
        (s.note ? '<span>' + esc(s.note) + '</span>' : '') + '</div>' +
        (s.date ? '<time>' + esc(s.date) + '</time>' : '') + '</li>';
    }).join('');

    return head(d.title || 'Результат проверки', d.subtitle) + banner +
      (checks ? '<div class="av-vd-checks">' + checks + '</div>' : '') + fix +
      (sources ? '<div class="av-vd-sources"><span class="av-calc-cap">Источники</span>' +
        '<ul>' + sources + '</ul></div>' : '');
  }

  /* Результат модели коллектива (инструменты run_model, optimize_model,
     train_model): рисуется интерфейсом прямо из ответа инструмента, а не
     из текста агента — число, погрешность и версия не зависят от того,
     как модель их пересказала. */
  var DOMAIN_TEXT = {
    inside: 'в области опытов',
    edge: 'на границе опытов — прогноз менее надёжен',
    outside: 'вне опытов — прогноз недостоверен, нужен опыт'
  };

  function mrNum(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    var a = Math.abs(Number(v));
    if (a !== 0 && (a >= 1e6 || a < 1e-3)) return Number(v).toExponential(2).replace('.', ',');
    return Number(v).toLocaleString('ru-RU', { maximumFractionDigits: a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3 });
  }

  function modelResult(d) {
    var m = d.model || {};
    var outs = d.outputs_meta || [];
    var ins = d.inputs_meta || [];
    var url = /^model\.html\?id=mdl-[a-f0-9]{10}$/.test(m.url || '') ? m.url : '';
    var h = '<div class="av-mr-head"><span class="av-mr-name">' + esc(m.name || 'Модель') + '</span>' +
      '<span class="av-mr-ver">версия ' + esc(m.version || '') + '</span></div>';
    var items = d.items || [];
    if (d.mode === 'train' && d.summary) {
      var s = d.summary;
      h += '<div class="av-mr-values">' + outs.map(function (o) {
        var so = (s.outputs || {})[o.name] || {};
        return '<div class="av-mr-value"><span>' + esc(o.title || o.name) + ': типичная ошибка</span><b>±' +
          mrNum(so.mae) + ' <small>' + esc(o.unit || '') + '</small></b></div>';
      }).join('') + (typeof s.skill === 'number' ? '<div class="av-mr-value"><span>точнее угадывания средним</span><b>' +
        Math.round(s.skill * 100) + ' %</b></div>' : '') + '</div>' +
        '<div class="av-mr-domain ' + (s.passed === false ? 'av-mr-domain--outside' : 'av-mr-domain--inside') + '">' +
        (s.passed === true ? 'порог приёмки пройден' : s.passed === false ? 'порог приёмки не пройден' : 'порог приёмки не задан') +
        (s.n ? ' · ' + esc(s.n) + ' опытов' : '') + '</div>';
    } else if (d.mode === 'kinetics' && d.kinetics) {
      var k = d.kinetics;
      var st = (k.domain || {}).status || 'inside';
      h += '<div class="av-mr-values"><div class="av-mr-value"><span>степень превращения в конце</span><b>' + mrNum(k.final) + '</b></div>' +
        (k.reach || []).map(function (r) {
          return '<div class="av-mr-value"><span>до ' + mrNum(r.target) + '</span><b>' + (r.time === null ? '—' : mrNum(r.time)) + '</b></div>';
        }).join('') + '</div><div class="av-mr-domain av-mr-domain--' + esc(st) + '">' + esc(DOMAIN_TEXT[st] || st) + '</div>';
    } else if (items.length === 1 && d.mode === 'predict') {
      var it = items[0];
      var st1 = (it.domain || {}).status || 'inside';
      h += '<div class="av-mr-values">' + outs.map(function (o) {
        var r = (it.outputs || {})[o.name] || {};
        return '<div class="av-mr-value"><span>' + esc(o.title || o.name) + '</span><b>' + mrNum(r.value) +
          (r.error !== null && r.error !== undefined ? ' <small>± ' + mrNum(r.error) + '</small>' : '') +
          ' <small>' + esc(o.unit || '') + '</small></b></div>';
      }).join('') + '</div><div class="av-mr-domain av-mr-domain--' + esc(st1) + '">' + esc(DOMAIN_TEXT[st1] || st1) +
        ((it.domain || {}).notes || []).map(function (n) { return ' · ' + esc(n); }).join('') + '</div>';
    } else if (items.length) {
      h += '<table class="av-mr-table"><thead><tr>' + ins.map(function (i) { return '<th>' + esc(i.title || i.name) + '</th>'; }).join('') +
        outs.map(function (o) { return '<th>' + esc(o.title || o.name) + (o.unit ? ', ' + esc(o.unit) : '') + '</th>'; }).join('') +
        '<th>область</th></tr></thead><tbody>' + items.slice(0, 12).map(function (it2) {
          var st2 = (it2.domain || {}).status || 'inside';
          return '<tr>' + ins.map(function (i) { var v = (it2.inputs || {})[i.name]; return '<td>' + (typeof v === 'number' ? mrNum(v) : esc(v)) + '</td>'; }).join('') +
            outs.map(function (o) { var r = (it2.outputs || {})[o.name] || {}; return '<td><b>' + mrNum(r.value) + '</b> ± ' + mrNum(r.error) + '</td>'; }).join('') +
            '<td class="av-mr-domain--' + esc(st2) + '">' + esc(st2 === 'inside' ? 'в опытах' : st2 === 'edge' ? 'на границе' : 'вне опытов') + '</td></tr>';
        }).join('') + '</tbody></table>';
    }
    if (url) h += '<a class="av-mr-link" href="' + esc(url) + '" target="_blank" rel="noopener">Открыть модель →</a>';
    return h;
  }

  /* --------------------------------------------------------------- */

  var COMPONENTS = {
    metric_cards: metricCards,
    data_table: dataTable,
    chart: chart,
    comparison: comparison,
    calc_trace: calcTrace,
    verdict: verdict,
    spec_sheet: specSheet,
    checklist: checklist,
    timeline: timeline,
    source_list: sourceList,
    file_card: fileCard,
    model_result: modelResult
  };

  function render(spec) {
    var fn = COMPONENTS[spec && spec.component];
    if (!fn) return null;
    try {
      return '<div class="av av--' + spec.component + '">' + fn(spec) + '</div>';
    } catch (e) {
      return null;  /* кривые данные не должны ронять весь ответ */
    }
  }

  /* Находит блоки ```aios-view в уже отрисованном тексте и подменяет
     их компонентами. Работает так же, как enhanceChartBlocks в
     workspace.js, чтобы не заводить второй механизм. */
  function enhance(root) {
    if (!root) return;
    var blocks = root.querySelectorAll('pre > code.language-aios-view, pre > code');
    Array.prototype.forEach.call(blocks, function (code) {
      var raw = code.textContent || '';
      var looksLikeView = /"component"\s*:/.test(raw) &&
        (code.className.indexOf('aios-view') >= 0 || raw.trim().charAt(0) === '{');
      if (!looksLikeView) return;
      var spec;
      try { spec = JSON.parse(raw); } catch (e) { return; }
      var html = render(spec);
      if (!html) return;
      var holder = document.createElement('div');
      holder.innerHTML = html;
      var pre = code.parentNode;
      pre.parentNode.replaceChild(holder.firstChild, pre);
    });
    bindCsv(root);
    bindAsk(root);
    hydrateFileMedia(root);
  }

  /* Кнопка внутри компонента («Исправить», «Пересчитать с запасом») не
     знает, где её нажали: в чате, на странице агента или в записи. Поэтому
     она не вызывает ничего напрямую, а поднимает событие aios:ask с текстом
     вопроса — страница-хозяин решает, что с ним делать. Модуль остаётся
     без единой ссылки на чат, как и задумано. */
  function bindAsk(root) {
    Array.prototype.forEach.call(root.querySelectorAll('.av-ask'), function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', function () {
        var text = btn.dataset.ask || '';
        if (!text) return;
        var ev;
        try {
          ev = new CustomEvent('aios:ask', { detail: { text: text }, bubbles: true });
        } catch (e) {            /* старые браузеры */
          ev = document.createEvent('CustomEvent');
          ev.initCustomEvent('aios:ask', true, false, { text: text });
        }
        btn.dispatchEvent(ev);
      });
    });
  }

  function bindCsv(root) {
    Array.prototype.forEach.call(root.querySelectorAll('.av-csv'), function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', function () {
        /* BOM — иначе Excel открывает кириллицу кракозябрами */
        var blob = new Blob(['﻿' + btn.dataset.csv], { type: 'text/csv;charset=utf-8' });
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = (btn.dataset.name || 'таблица') + '.csv';
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
      });
    });
  }

  global.AiosViews = { render: render, enhance: enhance, hydrateFileMedia: hydrateFileMedia,
                       components: Object.keys(COMPONENTS) };
})(window);
