/*
 * behaviors.js — the replacement for inline on* attributes.
 *
 * The Content-Security-Policy in add_security_headers() carries a per-request
 * nonce instead of 'unsafe-inline'. A nonce covers <script> blocks; it does
 * NOT cover inline event handlers, and a browser that sees a nonce ignores
 * 'unsafe-inline' entirely. So every on*= attribute had to go, and this file
 * is what they became.
 *
 * Two mechanisms, deliberately kept separate:
 *
 *   VZ.bind(key, type, fn)  — for handlers that were on markup Jinja renders.
 *       The element carries data-vzh="key". Several handlers on one element
 *       means several space-separated keys, matched with [data-vzh~="key"].
 *
 *   VZ.action(name, fn)     — for handlers on markup a page script BUILDS at
 *       runtime (the POS cart, the patient search dropdowns, the bill carts).
 *       Those cannot be bound once at load, because the elements do not exist
 *       yet and are replaced on every re-render. The element carries
 *       data-vz-act / data-vz-change / data-vz-input, and one delegated
 *       listener on document dispatches to the named function. Re-rendering
 *       the cart therefore needs no re-binding at all, which is why this is
 *       better than what it replaces rather than merely equivalent.
 *
 * Both preserve inline-handler semantics that the app actually relies on:
 * `this` is the element, `event` is the first argument, and returning false
 * calls preventDefault() — that last one is how "return confirm(...)" cancels
 * a form submit, and dropping it would have made every confirm dialog
 * decorative while looking like it worked.
 */
(function () {
  'use strict';

  function run(fn, el, event) {
    var result = fn.call(el, event);
    if (result === false) { event.preventDefault(); }
    return result;
  }

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  function bind(key, type, fn) {
    ready(function () {
      var nodes = document.querySelectorAll('[data-vzh~="' + key + '"]');
      for (var i = 0; i < nodes.length; i++) {
        (function (el) {
          el.addEventListener(type, function (event) { return run(fn, el, event); });
        })(nodes[i]);
      }
    });
  }

  var actions = Object.create(null);

  function action(name, fn) { actions[name] = fn; }

  function delegate(eventType, attribute) {
    document.addEventListener(eventType, function (event) {
      var start = event.target;
      if (!start || !start.closest) { return; }
      var el = start.closest('[' + attribute + ']');
      if (!el) { return; }
      var fn = actions[el.getAttribute(attribute)];
      if (fn) { run(fn, el, event); }
    });
  }

  delegate('click', 'data-vz-act');
  delegate('change', 'data-vz-change');
  delegate('input', 'data-vz-input');

  /* Whole-row navigation. Was onclick="window.location='...'" plus a matching
     onkeydown for Enter, repeated on every row of six list pages. A click on a
     control inside the row is left alone — the old attribute navigated even
     when you clicked the button inside it, which was a bug nobody had named. */
  function rowTarget(event) {
    var start = event.target;
    if (!start || !start.closest) { return null; }
    if (start.closest('a, button, input, select, textarea, label')) { return null; }
    return start.closest('[data-vz-href]');
  }

  document.addEventListener('click', function (event) {
    var el = rowTarget(event);
    if (el) { window.location = el.getAttribute('data-vz-href'); }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter') { return; }
    var el = event.target && event.target.closest
      ? event.target.closest('[data-vz-href]') : null;
    if (el) { window.location = el.getAttribute('data-vz-href'); }
  });

  window.VZ = window.VZ || {};
  window.VZ.bind = bind;
  window.VZ.action = action;
})();
