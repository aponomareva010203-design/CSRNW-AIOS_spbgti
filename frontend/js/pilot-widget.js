/* ============================================================
   Виджет «Статус внедрения AIOS» на главной (index.html).
   Заполняет прогресс-бар из тех же данных, что и таблица
   «Статус внедрения AIOS» (GET /api/pilot/projects,
   публичный эндпоинт) — виджет обновляется сам при изменении
   статусов на странице пилота.

   Этап 1 «Онбординг» считается пройденным проектом, если
   карточка активна (попадает в таблицу пилота). Остальные
   этапы — средний прогресс артефакта по всем проектам
   (статус 0–3 → 0…100%: «не начато» не добавляет, «гипотеза
   сформулирована» — треть, «в процессе создания» — две трети,
   «завершено» — весь вклад), поэтому бар растёт и при частичном
   прогрессе команд, а не только по завершённым артефактам:
     Данные и знания → rag, Агенты → agents, Модели → models,
     Полигоны → polygon, Социальная сеть агентов → social.
   Тултип сегмента раскрывает детализацию по статусам.
   ============================================================ */

(function () {
  var track = document.querySelector('.progress-track');
  if (!track) return;

  /* порядок сегментов трека = порядок стадий в карточке */
  var STAGES = ['onboarding', 'rag', 'agents', 'models', 'polygon', 'social'];

  var segs = track.querySelectorAll('.progress-seg');
  if (segs.length !== STAGES.length) return;

  function setFill(seg, fraction, tip) {
    var fill = seg.querySelector('.progress-seg-fill');
    if (!fill) {
      fill = document.createElement('div');
      fill.className = 'progress-seg-fill';
      seg.appendChild(fill);
    }
    var pct = Math.round(fraction * 100);
    fill.style.width = pct + '%';
    if (tip) seg.setAttribute('title', tip);
    else seg.removeAttribute('title');
  }

  window.AIOS_API.pilotList().then(function (data) {
    var rows = (data && data.items) || [];
    if (!rows.length) return;
    var total = rows.length;

    /* средний прогресс этапа: сумма статусов / (число проектов × 3);
       counts — сколько проектов на каждом статусе, для тултипа */
    function stageStat(art) {
      var sum = 0;
      var counts = [0, 0, 0, 0];
      rows.forEach(function (p) {
        var a = p.artifacts && p.artifacts[art];
        var st = a ? Number(a.status) : 0;
        if (!(st >= 0 && st <= 3)) st = 0;
        sum += st;
        counts[st] += 1;
      });
      return { fraction: sum / (total * 3), counts: counts };
    }

    /* Онбординг: карточки в таблице — подтверждённый онбординг */
    setFill(segs[0], 1, 'Онбординг пройден: ' + total + ' из ' + total + ' проектов');

    for (var i = 1; i < STAGES.length; i++) {
      var stat = stageStat(STAGES[i]);
      var tip = 'Средний прогресс этапа: ' + Math.round(stat.fraction * 100) + '%' +
        ' — «завершено» у ' + stat.counts[3] + ' из ' + total +
        ', «в процессе создания» у ' + stat.counts[2] +
        ', «гипотеза сформулирована» у ' + stat.counts[1];
      setFill(segs[i], stat.fraction, tip);
    }
  }).catch(function () { /* виджет остаётся пустым — таблица покажет ошибку на странице пилота */ });
})();
