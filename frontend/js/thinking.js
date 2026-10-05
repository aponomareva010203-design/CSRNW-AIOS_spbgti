/* Индикатор «думает»: наполняющаяся колба + подпись.
   Общий для рабочей области (workspace.js) и страницы агента
   (agent_page.js). Показывается, пока от модели не пришло ни одного
   токена: колба в палитре платформы (зелёный + золото) и подпись.
   Подпись не статичная: слово подбирается по формулировке вопроса
   («рассчитай…» → «Рассчитываю..»), затем мягко сменяется по кругу. */
(function () {
  'use strict';

  var ROTATE_MS = 3400;

  var GENERIC = ['Думаю..', 'Осмысляю..', 'Сопоставляю..', 'Прикидываю..', 'Взвешиваю..'];

  /* Фразы по теме вопроса: регэксп проверяет текст сообщения, первое
     слово — самая точная формулировка, остальные — вариации той же темы */
  var THEMES = [
    { re: /хим|реагент|реакци|молекул|синтез|раствор|кислот|спирт|полимер|веществ|катализ|колб|смес|экстрак|дистилл|вязкост|среда/i,
      words: ['Химичу..', 'Смешиваю реагенты..', 'Сверяю методику..'] },
    { re: /рассчит|вычисл|посчит|подсчит|формул|уравнен|скольк|баланс|масс|концентрац|тепл|давлен|числ/i,
      words: ['Рассчитываю..', 'Вычисляю..', 'Сверяю порядок величины..'] },
    { re: /онтолог|сущност|таксоном|классификац|граф|паспорт|термин/i,
      words: ['Онтологизирую..', 'Раскладываю по сущностям..', 'Связываю понятия..'] },
    { re: /прочит|перечит|документ|файл|стать|баз|найд|поиск|источник|литератур|прилож|реферат/i,
      words: ['Перечитываю..', 'Листаю источники..', 'Сверяюсь с первоисточником..'] },
    { re: /сравн|лучше|разниц|отлича|versus|\bvs\b/i,
      words: ['Сравниваю..', 'Взвешиваю варианты..'] },
    { re: /план|этап|стратег|последовательн|roadmap|график/i,
      words: ['Планирую..', 'Раскладываю по этапам..'] },
    { re: /напиши|сочин|текст|письм|отч[её]т|записк|слоган|формулир/i,
      words: ['Формулирую..', 'Подбираю слова..'] },
    { re: /провер|код|баг|ошибк|отлад|тест|почин|не работает|падает/i,
      words: ['Проверяю..', 'Локализую..', 'Ищу причину..'] }
  ];

  function poolFor(userText) {
    var pool = ['Думаю..'];
    var text = String(userText || '');
    THEMES.forEach(function (t) {
      if (t.re.test(text)) {
        t.words.forEach(function (w) {
          if (pool.indexOf(w) === -1) pool.push(w);
        });
      }
    });
    GENERIC.forEach(function (w) {
      if (pool.length >= 6) return;
      if (pool.indexOf(w) === -1) pool.push(w);
    });
    return pool;
  }

  var seq = 0;

  function create(userText) {
    var pool = poolFor(userText);
    var idx = 0;
    var clipId = 'think-clip-' + (++seq);

    var root = document.createElement('div');
    root.className = 'think';
    root.setAttribute('role', 'status');
    root.setAttribute('aria-label', 'Думаю');
    root.innerHTML =
      '<svg class="think-flask" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
        '<defs><clipPath id="' + clipId + '">' +
          '<path d="M10 3.8 h4 v4.4 l4 8.1 a2 2 0 0 1 -1.8 3 H7.8 a2 2 0 0 1 -1.8 -3 L10 8.2 Z"/>' +
        '</clipPath></defs>' +
        '<g clip-path="url(#' + clipId + ')">' +
          '<rect class="think-liquid" x="4" y="24" width="16" height="22"/>' +
          '<circle class="think-bubble think-bubble--1" cx="10.6" cy="17.6" r=".9"/>' +
          '<circle class="think-bubble think-bubble--2" cx="13.2" cy="18.4" r=".6"/>' +
          '<circle class="think-bubble think-bubble--3" cx="12" cy="16.8" r=".5"/>' +
        '</g>' +
        '<path class="think-glass" d="M9.4 3 h5.2 v4.8 l4.3 8.6 a2.5 2.5 0 0 1 -2.2 3.7 H7.3 a2.5 2.5 0 0 1 -2.2 -3.7 L9.4 7.8 Z"/>' +
        '<path class="think-glass" d="M8.5 3 h7"/>' +
      '</svg>' +
      '<span class="think-label" aria-hidden="true"></span>';

    var label = root.querySelector('.think-label');
    label.textContent = pool[0];

    var timer = setInterval(function () {
      idx = (idx + 1) % pool.length;
      label.textContent = pool[idx];
    }, ROTATE_MS);

    return {
      el: root,
      stop: function () { clearInterval(timer); }
    };
  }

  window.AiosThinking = { create: create };
})();
