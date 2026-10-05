/* Сворачиваемые разделы левого меню («Дашборды», «Каталоги»).
   Подключается сразу после <aside>, чтобы применить сохранённое
   состояние до первой отрисовки — меню не мигает раскрытым.
   Раздел с активным пунктом всегда раскрывается при загрузке,
   иначе текущая страница оказалась бы спрятана. */
(function () {
  var KEY = 'aios-side-groups-v1';
  var state = {};
  try { state = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { state = {}; }

  document.querySelectorAll('.side-group--collapsible').forEach(function (group) {
    var name = group.getAttribute('data-side-group');
    var toggle = group.querySelector('.side-group-toggle');
    if (!name || !toggle) return;

    function apply(collapsed) {
      group.classList.toggle('side-group--collapsed', collapsed);
      toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    }

    apply(!!state[name] && !group.querySelector('.side-link.active'));

    toggle.addEventListener('click', function () {
      var collapsed = !group.classList.contains('side-group--collapsed');
      state[name] = collapsed;
      apply(collapsed);
      try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
    });
  });
})();
