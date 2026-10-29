/* ============================================================
   «Модели офиса проректора» (models.html, блок #gridBuiltin).

   Прозрачные демо-модели для решений по инновационному портфелю вуза —
   считаются в браузере, без LLM и без песочницы:
     - trl-transition — шанс перехода проекта на следующий УГТ за 12 мес.
       (логистическая модель, вклад каждого фактора виден в разборе);
     - irl-score      — уровень инвестиционной готовности стартапа (IRL 1–9)
       по чек-листу и рекомендуемая мера поддержки;
     - license-npv    — ожидаемая NPV лицензирования РИД с учётом
       вероятности сделки, разгона продаж и затрат на поддержание патента.

   Коэффициенты экспертные (см. COEF ниже): откалибруйте их на истории
   своего портфеля. Модели доступны и другим страницам как
   window.AIOS_INNOVATION_MODELS (полигоны берут из них шанс перехода УГТ).
   ============================================================ */

(function () {
  function sigmoid(x) { return 1 / (1 + Math.exp(-x)); }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function n(v, d) { var x = parseFloat(String(v).replace(',', '.')); return isNaN(x) ? d : x; }
  function fmt(v, digits) {
    return Number(v).toLocaleString('ru-RU', { maximumFractionDigits: digits == null ? 1 : digits });
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* ---------- 1. Шанс перехода на следующий УГТ ---------- */

  var COEF = {
    // базовый логит по уровню: УГТ 3–5 — «долина смерти» вузовских технологий
    level: { 1: 0.1, 2: 0.0, 3: -0.4, 4: -0.8, 5: -0.9, 6: -0.5, 7: -0.6, 8: -0.9 },
    stale: -0.45,        // за каждый год на уровне сверх первого
    team: 0.08,          // за каждого участника (до 8)
    entrepreneur: 0.7,   // в команде есть предприниматель / CEO
    partner: 0.9,        // есть индустриальный партнёр / заказчик
    budget: 0.6,         // × ln(1 + бюджет, млн ₽/год)
    ip: 0.3,             // РИД охраняется (патент / ПО / ноу-хау)
    custdev: 0.4         // проведены интервью с клиентами / есть LOI
  };

  function trlTransition(x) {
    var level = clamp(Math.round(n(x.trl, 4)), 1, 8);
    var years = Math.max(0, n(x.years, 1));
    var team = clamp(n(x.team, 3), 0, 30);
    var budget = Math.max(0, n(x.budget, 2));
    var parts = [
      ['Базовый шанс для УГТ ' + level, COEF.level[level]],
      ['Срок на текущем уровне (' + fmt(years) + ' г.)', COEF.stale * Math.max(0, years - 1)],
      ['Команда (' + fmt(team, 0) + ' чел.)', COEF.team * Math.min(team, 8)],
      ['Предприниматель в команде', x.entrepreneur ? COEF.entrepreneur : 0],
      ['Индустриальный партнёр', x.partner ? COEF.partner : 0],
      ['Бюджет ' + fmt(budget) + ' млн ₽/год', COEF.budget * Math.log(1 + budget)],
      ['Охраняемый РИД', x.ip ? COEF.ip : 0],
      ['Подтверждён спрос (CustDev / LOI)', x.custdev ? COEF.custdev : 0]
    ];
    var logit = parts.reduce(function (s, p) { return s + p[1]; }, 0) - 1.2;
    return { p: sigmoid(logit), parts: parts, level: level, years: years };
  }

  /* ---------- 2. Инвестиционная готовность (IRL) ---------- */

  var IRL_ITEMS = [
    ['team', 'Сформирована команда (CEO + CTO)', 1],
    ['segment', 'Описан сегмент клиентов и проблема', 1],
    ['interviews', 'Проведено 20+ проблемных интервью', 1],
    ['mvp', 'Есть MVP или прототип', 1],
    ['pilot', 'Есть пилот / письма о намерениях (LOI)', 1],
    ['model', 'Посчитана бизнес-модель и юнит-экономика', 1],
    ['sales', 'Есть первые продажи', 1],
    ['repeat', 'Повторные продажи, CAC < LTV', 1],
    ['ip', 'Права на РИД оформлены на компанию (лицензия вуза)', 0.5],
    ['company', 'Зарегистрировано юрлицо', 0.5],
    ['docs', 'Готовы питч-дек, финмодель, cap table', 1]
  ];
  var IRL_MEASURES = [
    [2, 'Предакселератор вуза, «Студенческий стартап» / «УМНИК» Фонда содействия инновациям'],
    [4, 'Стартап-студия: трек CustDev и MVP; «Студенческий стартап» для студенческих команд'],
    [6, '«Старт» Фонда содействия инновациям, пилот с индустриальным партнёром'],
    [8, 'Посевные фонды, бизнес-ангелы, корпоративные акселераторы'],
    [9, 'Венчурные фонды, «Развитие» / «Коммерциализация» Фонда содействия инновациям']
  ];

  function irlScore(x) {
    // уровень = число последовательно выполненных шагов лестницы (как в шкале IRL),
    // юрлицо и права на РИД — поправки ±0,5
    var ladder = ['team', 'segment', 'interviews', 'mvp', 'pilot', 'model', 'sales', 'repeat', 'docs'];
    var level = 0;
    for (var i = 0; i < ladder.length; i++) {
      if (x[ladder[i]]) level = i + 1; else break;
    }
    var bonus = (x.ip ? 0.5 : 0) + (x.company ? 0.5 : 0);
    var gaps = IRL_ITEMS.filter(function (it) { return !x[it[0]]; }).map(function (it) { return it[1]; });
    var irl = clamp(Math.round(level + (level >= 5 ? bonus : 0)), 1, 9);
    var measure = IRL_MEASURES.filter(function (m) { return irl <= m[0]; })[0] || IRL_MEASURES[IRL_MEASURES.length - 1];
    return { irl: irl, measure: measure[1], gaps: gaps.slice(0, 4) };
  }

  /* ---------- 3. NPV лицензирования РИД ---------- */

  function licenseNpv(x) {
    var revenue = Math.max(0, n(x.revenue, 50));       // выручка лицензиата на зрелости, млн ₽/год
    var royalty = clamp(n(x.royalty, 3), 0, 50) / 100;
    var lag = clamp(Math.round(n(x.lag, 2)), 0, 10);   // лет до первых продаж
    var ramp = clamp(Math.round(n(x.ramp, 3)), 1, 10); // лет разгона продаж
    var term = clamp(Math.round(n(x.term, 10)), 1, 20);
    var prob = clamp(n(x.prob, 40), 0, 100) / 100;
    var rate = clamp(n(x.rate, 15), 0, 60) / 100;
    var lump = Math.max(0, n(x.lump, 0.5));            // паушальный платёж, млн ₽
    var upkeep = Math.max(0, n(x.upkeep, 60)) / 1000;  // поддержание патента, тыс. → млн ₽/год
    var flows = [];
    var npv = 0;
    for (var t = 0; t < term; t++) {
      var share = t < lag ? 0 : Math.min(1, (t - lag + 1) / ramp);
      var cash = prob * (revenue * share * royalty + (t === 0 ? lump : 0)) - upkeep;
      flows.push(cash);
      npv += cash / Math.pow(1 + rate, t + 1);
    }
    var total = flows.reduce(function (s, c) { return s + c; }, 0);
    return { npv: npv, flows: flows, total: total };
  }

  window.AIOS_INNOVATION_MODELS = { trlTransition: trlTransition, irlScore: irlScore, licenseNpv: licenseNpv };

  /* ---------- описания форм ---------- */

  var MODELS = [
    {
      id: 'trl-transition',
      name: 'Шанс перехода проекта на следующий УГТ',
      kind: 'портфель',
      out: 'Вероятность перехода за 12 месяцев',
      desc: 'Логистическая модель по факторам, которые эксперты стартап-студий называют решающими: срок «застоя», команда, партнёр, бюджет, спрос.',
      fields: [
        ['trl', 'Текущий УГТ (1–8)', 'number', 4],
        ['years', 'Лет на текущем уровне', 'number', 1.5],
        ['team', 'Размер команды, чел.', 'number', 4],
        ['budget', 'Бюджет на год, млн ₽', 'number', 3],
        ['entrepreneur', 'В команде есть предприниматель / CEO', 'bool', false],
        ['partner', 'Есть индустриальный партнёр', 'bool', true],
        ['ip', 'РИД охраняется', 'bool', true],
        ['custdev', 'Спрос подтверждён (интервью, LOI)', 'bool', false]
      ],
      render: function (x) {
        var r = trlTransition(x);
        var pct = Math.round(r.p * 100);
        var verdict = r.years >= 2 && pct < 35
          ? 'Кандидат на пересмотр: два года и больше на одном УГТ при низком шансе роста — обсудите закрытие, лицензирование или смену команды.'
          : pct >= 60 ? 'Проект в хорошей динамике: держите ресурсы и готовьте следующую контрольную точку.'
            : 'Средний шанс: усилите самый весомый недостающий фактор из разбора ниже.';
        return '<div class="imdl-result"><b>' + pct + ' %</b><span>шанс перейти на УГТ ' + (r.level + 1) + ' за 12 месяцев</span></div>' +
          '<p class="imdl-verdict">' + esc(verdict) + '</p>' + partsTable(r.parts);
      }
    },
    {
      id: 'irl-score',
      name: 'Инвестиционная готовность стартапа (IRL)',
      kind: 'стартапы',
      out: 'IRL 1–9 и следующая мера поддержки',
      desc: 'Лестница IRL из методики стартап-студии: уровень растёт, только если пройдены все предыдущие шаги. Подсказывает, куда направить команду.',
      fields: IRL_ITEMS.map(function (it, i) { return [it[0], it[1], 'bool', i < 3]; }),
      render: function (x) {
        var r = irlScore(x);
        return '<div class="imdl-result"><b>IRL ' + r.irl + '</b><span>уровень инвестиционной готовности</span></div>' +
          '<p class="imdl-verdict"><b>Куда направить:</b> ' + esc(r.measure) + '</p>' +
          (r.gaps.length ? '<p class="imdl-verdict"><b>Чего не хватает:</b> ' + esc(r.gaps.join('; ')) + '</p>' : '');
      }
    },
    {
      id: 'license-npv',
      name: 'NPV лицензирования РИД',
      kind: 'трансфер',
      out: 'Ожидаемая приведённая стоимость, млн ₽',
      desc: 'Сколько стоит лицензионная сделка для вуза: роялти с выручки лицензиата, паушальный платёж, вероятность успеха, пошлины на патент, дисконт.',
      fields: [
        ['revenue', 'Выручка лицензиата на зрелости, млн ₽/год', 'number', 80],
        ['royalty', 'Роялти, %', 'number', 3],
        ['lump', 'Паушальный платёж, млн ₽', 'number', 0.5],
        ['lag', 'Лет до первых продаж', 'number', 2],
        ['ramp', 'Лет разгона продаж до зрелости', 'number', 3],
        ['term', 'Срок лицензии, лет', 'number', 10],
        ['prob', 'Вероятность, что продукт выйдет на рынок, %', 'number', 40],
        ['upkeep', 'Поддержание патента, тыс. ₽/год', 'number', 60],
        ['rate', 'Ставка дисконтирования, %', 'number', 15]
      ],
      render: function (x) {
        var r = licenseNpv(x);
        var max = Math.max.apply(null, r.flows.map(function (f) { return Math.abs(f); }).concat([0.001]));
        var bars = r.flows.map(function (f, i) {
          var h = Math.round(Math.abs(f) / max * 56);
          return '<span class="imdl-bar' + (f < 0 ? ' imdl-bar--neg' : '') + '" style="height:' + Math.max(2, h) +
            'px" title="Год ' + (i + 1) + ': ' + fmt(f, 2) + ' млн ₽"></span>';
        }).join('');
        var verdict = r.npv > 0
          ? 'Сделка выгодна вузу при этих допущениях. Проверьте чувствительность к вероятности и сроку до продаж.'
          : 'При этих допущениях патент обходится дороже ожидаемого дохода: рассмотрите отчуждение, вклад в МТК или отказ от поддержания.';
        return '<div class="imdl-result"><b>' + fmt(r.npv, 2) + ' млн ₽</b><span>ожидаемая NPV (сумма потоков ' + fmt(r.total, 2) + ' млн ₽)</span></div>' +
          '<div class="imdl-bars" aria-label="Ожидаемый денежный поток по годам">' + bars + '</div>' +
          '<p class="imdl-verdict">' + esc(verdict) + '</p>';
      }
    }
  ];

  function partsTable(parts) {
    var rows = parts.filter(function (p) { return Math.abs(p[1]) > 1e-9; }).map(function (p) {
      var w = Math.min(100, Math.round(Math.abs(p[1]) / 1.5 * 100));
      return '<tr><td>' + esc(p[0]) + '</td><td class="imdl-contrib"><span class="imdl-contrib-bar' +
        (p[1] < 0 ? ' imdl-contrib-bar--neg' : '') + '" style="width:' + w + '%"></span>' +
        (p[1] > 0 ? '+' : '') + fmt(p[1], 2) + '</td></tr>';
    }).join('');
    return '<table class="imdl-parts"><thead><tr><th>Фактор</th><th>Вклад в логит</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  /* ---------- UI ---------- */

  var grid = document.getElementById('gridBuiltin');
  if (!grid) return;

  var ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19h16"/><path d="M6 16l4-5 3 3 5-7"/><circle cx="18" cy="7" r="1.4"/></svg>';

  MODELS.forEach(function (m) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'mdl-card imdl-card';
    card.innerHTML =
      '<span class="mdl-card-top"><span class="mdl-kind">' + ICON + esc(m.kind) + '</span>' +
      '<span class="mdl-status mdl-status--published">встроенная</span></span>' +
      '<span class="mdl-card-name">' + esc(m.name) + '</span>' +
      '<span class="mdl-card-out">' + esc(m.desc) + '</span>' +
      '<span class="mdl-card-foot">Результат: ' + esc(m.out) + '</span>';
    card.addEventListener('click', function () { open(m); });
    grid.appendChild(card);
  });

  var overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.hidden = true;
  overlay.innerHTML =
    '<div class="modal imdl-modal" role="dialog" aria-modal="true" aria-labelledby="imdlTitle">' +
      '<button class="modal-close" type="button" aria-label="Закрыть">✕</button>' +
      '<h2 id="imdlTitle"></h2><p class="mdl-import-note imdl-desc"></p>' +
      '<div class="imdl-body"><form class="mdl-form imdl-form"></form><div class="imdl-out" aria-live="polite"></div></div>' +
    '</div>';
  document.body.appendChild(overlay);
  var form = overlay.querySelector('.imdl-form');
  var out = overlay.querySelector('.imdl-out');
  var current = null;

  function close() { overlay.hidden = true; document.body.style.overflow = ''; }
  overlay.querySelector('.modal-close').addEventListener('click', close);
  overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !overlay.hidden) close(); });

  function values() {
    var x = {};
    current.fields.forEach(function (f) {
      var el = form.elements[f[0]];
      x[f[0]] = f[2] === 'bool' ? el.checked : el.value;
    });
    return x;
  }
  function recalc() { if (current) out.innerHTML = current.render(values()); }
  form.addEventListener('input', recalc);
  form.addEventListener('submit', function (e) { e.preventDefault(); recalc(); });

  function open(m) {
    current = m;
    overlay.querySelector('#imdlTitle').textContent = m.name;
    overlay.querySelector('.imdl-desc').textContent = m.desc;
    form.innerHTML = m.fields.map(function (f) {
      if (f[2] === 'bool') {
        return '<label class="imdl-check"><input type="checkbox" name="' + f[0] + '"' + (f[3] ? ' checked' : '') + '> ' + esc(f[1]) + '</label>';
      }
      return '<label class="mdl-field"><span>' + esc(f[1]) + '</span><input type="number" step="any" name="' + f[0] + '" value="' + f[3] + '"></label>';
    }).join('');
    recalc();
    overlay.hidden = false;
    document.body.style.overflow = 'hidden';
  }
})();
