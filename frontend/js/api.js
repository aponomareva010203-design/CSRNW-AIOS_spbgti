/* ============================================================
   API-клиент AIOS.
   Единственная точка обращения фронтенда к данным.
   Сейчас базовый адрес — локальный FastAPI (/api). Чтобы позже
   перейти на выделенный бэкенд или Hugging Face Hub через сервер,
   достаточно изменить BASE_URL — интерфейс страниц не меняется.
   ============================================================ */

(function () {
  var BASE_URL = '/api';

  function getJSON(path) {
    return fetch(BASE_URL + path, { headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      });
  }

  /**
   * Каталог моделей.
   * @param {string} [source] — 'spbti' | 'huggingface'; без параметра — весь каталог.
   * @returns {Promise<{items: Array}>}
   */
  function getModels(source) {
    var query = source ? '?source=' + encodeURIComponent(source) : '';
    return getJSON('/models' + query);
  }

  window.AIOS_API = { getModels: getModels };
})();
