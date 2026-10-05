/**
 * AIOS Технологического института — баннер согласия на файлы cookie.
 *
 * Подключается на каждую страницу платформы (frontend/*.html).
 * Хранит решение пользователя в localStorage (ключ aios_cookie_consent):
 *   { status: 'accepted' | 'essential_only', at: <ISO-дата>, version: 1 }
 *
 * Пока решения нет — баннер показывается при каждом визите. Строго
 * необходимые данные браузера (сессионный токен входа в localStorage)
 * баннер не блокирует — они не являются аналитикой/трекингом.
 *
 * Аналитических cookie на платформе сейчас нет. Если они появятся,
 * их инициализация должна быть обёрнута в функцию
 * window.__aiosLoadAnalytics и НЕ вызываться сама по себе — этот файл
 * вызовет её только после согласия пользователя (см. maybeLoadAnalytics).
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'aios_cookie_consent';
  var CONSENT_VERSION = 1;
  var POLICY_URL = 'privacy-policy.html#cookies';

  function readConsent() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || parsed.version !== CONSENT_VERSION) return null;
      return parsed;
    } catch (e) {
      return null;
    }
  }

  function writeConsent(status) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        status: status,
        at: new Date().toISOString(),
        version: CONSENT_VERSION
      }));
    } catch (e) {
      /* localStorage может быть недоступен (приватный режим и т.п.) —
         тогда просто не запоминаем выбор между визитами, это не критично. */
    }
  }

  function maybeLoadAnalytics() {
    /* Аналитики на платформе пока нет: хук пуст. Когда появятся
       аналитические сервисы — их запуск переедет в window.__aiosLoadAnalytics,
       и он будет вызываться только здесь, после согласия. */
    if (typeof window.__aiosLoadAnalytics === 'function') {
      try {
        window.__aiosLoadAnalytics();
      } catch (e) {
        /* аналитика не должна ронять страницу */
      }
    }
  }

  function injectStyles() {
    var style = document.createElement('style');
    style.textContent = [
      '#aios-cookie-banner{position:fixed;left:16px;right:16px;bottom:16px;z-index:200;',
      'display:flex;flex-wrap:wrap;align-items:center;gap:16px;',
      'background:#FFFFFF;color:#2C3128;border:1px solid #E4E0D4;',
      'border-radius:14px;padding:18px 20px;box-shadow:0 2px 4px rgba(44,49,40,.06),0 16px 40px rgba(93,108,80,.16);',
      'font-family:"Segoe UI","Helvetica Neue",Arial,"Noto Sans",sans-serif;',
      'font-size:13.5px;line-height:1.55;max-width:960px;margin:0 auto;}',

      '#aios-cookie-banner .acb-text{flex:1 1 380px;color:#6A7060;}',
      '#aios-cookie-banner .acb-text strong{color:#2C3128;font-weight:650;}',
      '#aios-cookie-banner .acb-text a{color:#5D6C50;text-decoration:underline;text-underline-offset:2px;}',
      '#aios-cookie-banner .acb-actions{display:flex;gap:10px;flex-wrap:wrap;flex:0 0 auto;margin-left:auto;}',
      '#aios-cookie-banner button{font-family:inherit;font-size:12.5px;font-weight:650;',
      'letter-spacing:.02em;border-radius:8px;padding:10px 18px;cursor:pointer;border:1px solid transparent;',
      'white-space:nowrap;transition:opacity .15s;}',
      '#aios-cookie-banner button:hover{opacity:.85;}',
      '#aios-cookie-banner .acb-accept{background:#5D6C50;color:#F4F6EF;}',
      '#aios-cookie-banner .acb-essential{background:transparent;color:#2C3128;border-color:#C9CFC0;}',

      '@media (max-width:640px){#aios-cookie-banner{left:8px;right:8px;bottom:8px;padding:16px;}',
      '#aios-cookie-banner .acb-actions{width:100%;margin-left:0;}',
      '#aios-cookie-banner .acb-actions button{flex:1;}}'
    ].join('');
    document.head.appendChild(style);
  }

  function renderBanner() {
    if (document.getElementById('aios-cookie-banner')) return;
    var el = document.createElement('div');
    el.id = 'aios-cookie-banner';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', 'Уведомление об использовании файлов cookie');
    el.innerHTML =
      '<div class="acb-text">' +
        'Платформа AIOS использует <strong>файлы cookie и данные браузера (localStorage)</strong> ' +
        'для сохранения входа и работы интерфейса — это строго необходимые данные. ' +
        'Аналитические cookie на платформе сейчас не используются; если появятся, ' +
        'они будут включаться только с вашего согласия. Подробнее — в ' +
        '<a href="' + POLICY_URL + '" target="_blank" rel="noopener">политике обработки персональных данных и файлов cookie</a>.' +
      '</div>' +
      '<div class="acb-actions">' +
        '<button type="button" class="acb-essential" id="acb-essential-btn">Только необходимые</button>' +
        '<button type="button" class="acb-accept" id="acb-accept-btn">Принять все</button>' +
      '</div>';
    document.body.appendChild(el);

    document.getElementById('acb-accept-btn').addEventListener('click', function () {
      writeConsent('accepted');
      el.remove();
      maybeLoadAnalytics();
    });
    document.getElementById('acb-essential-btn').addEventListener('click', function () {
      writeConsent('essential_only');
      el.remove();
    });
  }

  function init() {
    injectStyles();
    var consent = readConsent();
    if (!consent) {
      renderBanner();
      return;
    }
    // Решение уже было — баннер не показываем и никаких элементов
    // на странице не оставляем.
    if (consent.status === 'accepted') {
      maybeLoadAnalytics();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Публичный API: программно открыть баннер (резерв, интерфейсом не вызывается).
  window.aiosOpenCookieSettings = function () {
    var existingBanner = document.getElementById('aios-cookie-banner');
    if (existingBanner) return;
    renderBanner();
  };
})();
