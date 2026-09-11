/* ============================================================
   Виджет «Статус пилотного проекта» на главной (index.html).
   Заполняет прогресс-бар из тех же данных, что и таблица
   «Статус пилотного проекта» (GET /api/pilot/projects,
   публичный эндпоинт) — виджет обновляется сам при изменении
   статусов на странице пилота.

   Этап 1 «Онбординг» считается пройденным проектом, если
   карточка активна (попадает в таблицу пилота). Остальные
   этапы — доли проектов, у которых соответствующий артефакт
   имеет статус «завершено» (3):
     Данные и знания → rag, Модели → models, Агенты → agents,
     Полигоны → polygon, Социальная сеть агентов → social.
   ============================================================ */

(function () {
  var track = document.querySelector('.progress-track');
  if (!track) return;

  /* порядок сегментов трека = порядок стадий в карточке */
  var STAGES = ['onboarding', 'rag', 'models', 'agents', 'polygon', 'social'];

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

    function doneCount(art) {
      var n = 0;
      rows.forEach(function (p) {
        var a = p.artifacts && p.artifacts[art];
        if (a && Number(a.status) === 3) n += 1;
      });
      return n;
    }

    /* Онбординг: карточки в таблице — подтверждённый онбординг */
    setFill(segs[0], 1, 'Онбординг пройден: ' + total + ' из ' + total + ' проектов');

    for (var i = 1; i < STAGES.length; i++) {
      var art = STAGES[i];
      var n = doneCount(art);
      setFill(segs[i], n / total, n + ' из ' + total + ' проектов — «завершено»');
    }
  }).catch(function () { /* виджет остаётся пустым — таблица покажет ошибку на странице пилота */ });
})();
