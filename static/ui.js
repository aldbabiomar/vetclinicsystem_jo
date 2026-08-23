/*
 * Shared UI behaviors (Experience Enhancement Plan, Part 1).
 * Every behavior here is purely presentational — none of it changes a
 * route, a validation rule, or what a submit actually does server-side.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------
  // 1.8 — Inline validation: when the browser blocks a submit because a
  // required/invalid field wasn't filled in, show the message right under
  // that field (and focus it) instead of only the native browser tooltip,
  // and clear it the moment the person starts fixing it. Validation rules
  // themselves (required, min, max, pattern, type) are untouched — this
  // only changes how the existing rule's message is surfaced.
  // ---------------------------------------------------------------
  function fieldErrorEl(field) {
    let err = field.parentElement.querySelector(".field-error");
    if (!err) {
      err = document.createElement("div");
      err.className = "field-error";
      field.insertAdjacentElement("afterend", err);
    }
    return err;
  }
  document.addEventListener("invalid", function (e) {
    const field = e.target;
    if (!(field instanceof HTMLElement) || !field.closest) return;
    e.preventDefault(); // suppress the native tooltip, show ours instead
    field.classList.add("field-invalid");
    const err = fieldErrorEl(field);
    err.textContent = field.validationMessage;
    field.addEventListener("input", function clear() {
      field.classList.remove("field-invalid");
      err.remove();
      field.removeEventListener("input", clear);
    });
    if (typeof field.focus === "function") field.focus();
    if (typeof field.scrollIntoView === "function") field.scrollIntoView({ block: "center", behavior: window.VZSpring.reduceMotion() ? "auto" : "smooth" });
  }, true);

  // ---------------------------------------------------------------
  // 1.9 — Styled confirm dialog, replacing native confirm()
  // ---------------------------------------------------------------
  function ensureDialog() {
    let el = document.getElementById("vz-confirm-overlay");
    if (el) return el;
    const wrap = document.createElement("div");
    wrap.innerHTML =
      '<div id="vz-confirm-overlay" class="modal-overlay" style="display:none;">' +
      '<div class="modal-box vz-confirm-box" role="alertdialog" aria-modal="true" aria-labelledby="vz-confirm-msg">' +
      '<p id="vz-confirm-msg" class="vz-confirm-msg"></p>' +
      '<div class="form-actions" style="justify-content:flex-end; margin-top:18px;">' +
      '<button type="button" class="btn small secondary" id="vz-confirm-cancel">Cancel</button>' +
      '<button type="button" class="btn small danger" id="vz-confirm-ok">Confirm</button>' +
      "</div></div></div>";
    document.body.appendChild(wrap.firstElementChild);
    return document.getElementById("vz-confirm-overlay");
  }

  // Returns a Promise<boolean> — same "are you sure" moment as native
  // confirm(), just styled like the rest of the product (§16.2).
  function vzConfirm(message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      const overlay = ensureDialog();
      overlay.querySelector("#vz-confirm-msg").textContent = message;
      const okBtn = overlay.querySelector("#vz-confirm-ok");
      const cancelBtn = overlay.querySelector("#vz-confirm-cancel");
      okBtn.textContent = opts.okLabel || "Confirm";
      cancelBtn.textContent = opts.cancelLabel || "Cancel";

      function cleanup(result) {
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        document.removeEventListener("keydown", onKey);
        window.VZSpring.present(overlay, false, { originEl: opts.originEl });
        resolve(result);
      }
      function onOk() { cleanup(true); }
      function onCancel() { cleanup(false); }
      function onKey(e) {
        if (e.key === "Escape") cleanup(false);
      }
      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      document.addEventListener("keydown", onKey);
      window.VZSpring.present(overlay, true, { originEl: opts.originEl, preset: "ui" });
      cancelBtn.focus();
    });
  }
  window.VZDialog = { confirm: vzConfirm };

  // Any <form data-confirm="…"> gets the same "are you sure" behavior as
  // before, just via the styled dialog instead of native confirm(). Same
  // copy (data-confirm carries the exact original message), same block
  // until answered.
  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.hasAttribute("data-confirm") || form.dataset.confirmed === "1") return;
    e.preventDefault();
    const trigger = document.activeElement;
    vzConfirm(form.getAttribute("data-confirm"), { originEl: trigger }).then(function (ok) {
      if (ok) {
        form.dataset.confirmed = "1";
        if (form.requestSubmit) form.requestSubmit();
        else form.submit();
        delete form.dataset.confirmed;
      }
    });
  });

  // ---------------------------------------------------------------
  // 1.7 — Optimistic "Saving…" state the instant a form is submitted,
  // not when the response arrives. Skips forms mid-confirmation (they get
  // the state once actually submitted) and anything opting out.
  // ---------------------------------------------------------------
  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.noSavingState === "1") return;
    if (form.hasAttribute("data-confirm") && form.dataset.confirmed !== "1") return; // waits for confirm
    const btns = form.querySelectorAll('button[type="submit"], input[type="submit"]');
    btns.forEach(function (btn) {
      if (btn.disabled) return;
      const label = btn.tagName === "INPUT" ? "value" : "textContent";
      btn.dataset.originalLabel = btn[label];
      btn[label] = btn.dataset.savingLabel || "Saving…";
      btn.disabled = true;
      btn.classList.add("is-saving");
    });
  }, true);

  // ---------------------------------------------------------------
  // 1.6 — Row press feedback + 10px movement threshold + focus + aria-label
  // for every tabindex="0" role="link" table row. Rows carry data-row-href
  // instead of inline onclick, so a press-and-drag scroll on a touchscreen
  // doesn't also trigger the row underneath (§10).
  // ---------------------------------------------------------------
  function initRowNav() {
    document.querySelectorAll("tr[data-row-href]").forEach(function (tr) {
      if (tr.dataset.rowNavBound === "1") return;
      tr.dataset.rowNavBound = "1";
      let startX = 0,
        startY = 0,
        dragged = false;
      tr.addEventListener("pointerdown", function (e) {
        startX = e.clientX;
        startY = e.clientY;
        dragged = false;
      });
      tr.addEventListener("pointermove", function (e) {
        if (Math.abs(e.clientX - startX) > 10 || Math.abs(e.clientY - startY) > 10) dragged = true;
      });
      tr.addEventListener("pointerup", function (e) {
        if (dragged) return;
        if (e.target.closest("a, button, input, select, textarea")) return;
        window.location = tr.dataset.rowHref;
      });
      tr.addEventListener("keydown", function (e) {
        if (e.key === "Enter") window.location = tr.dataset.rowHref;
      });
    });
  }

  // ---------------------------------------------------------------
  // 1.4 — Sidebar group collapsing, remembers last open/closed state per
  // person via localStorage. Every link stays exactly where it is — this
  // only adds a fold.
  // ---------------------------------------------------------------
  function initCollapsibleGroups() {
    document.querySelectorAll(".nav-group[data-collapsible]").forEach(function (group) {
      const key = "vetclinicsystemjo-navgroup-" + group.dataset.collapsible;
      const header = group.querySelector(".nav-group-toggle");
      if (!header) return;
      const collapsed = localStorage.getItem(key) === "closed";
      group.classList.toggle("collapsed", collapsed);
      header.setAttribute("aria-expanded", String(!collapsed));
      header.addEventListener("click", function () {
        const isCollapsed = group.classList.toggle("collapsed");
        header.setAttribute("aria-expanded", String(!isCollapsed));
        try {
          localStorage.setItem(key, isCollapsed ? "closed" : "open");
        } catch (e) {}
      });
    });
  }

  // ---------------------------------------------------------------
  // 1.5 — Scroll-edge fade under sticky table headers, instead of a hard
  // cut, once a table has scrolled.
  // ---------------------------------------------------------------
  function initScrollFade() {
    document.querySelectorAll(".table-wrap").forEach(function (wrap) {
      if (wrap.dataset.scrollFadeBound === "1") return;
      wrap.dataset.scrollFadeBound = "1";
      const update = function () {
        wrap.classList.toggle("is-scrolled", wrap.scrollTop > 2);
      };
      wrap.addEventListener("scroll", update, { passive: true });
      update();
    });
  }

  // ---------------------------------------------------------------
  // 1.3 — Modal spring open/close + click-anchored transform-origin, for
  // every .modal-overlay in the app. A modal is opened via
  // data-open-modal="#id" on the trigger, closed via data-close-modal or a
  // backdrop click.
  // ---------------------------------------------------------------
  function initModals() {
    document.querySelectorAll("[data-open-modal]").forEach(function (trigger) {
      if (trigger.dataset.modalBound === "1") return;
      trigger.dataset.modalBound = "1";
      trigger.addEventListener("click", function () {
        const target = document.querySelector(trigger.getAttribute("data-open-modal"));
        if (target) window.VZSpring.present(target, true, { originEl: trigger });
      });
    });
    document.querySelectorAll("[data-close-modal]").forEach(function (btn) {
      if (btn.dataset.modalBound === "1") return;
      btn.dataset.modalBound = "1";
      btn.addEventListener("click", function () {
        const overlay = btn.closest(".modal-overlay");
        if (overlay) window.VZSpring.present(overlay, false, {});
      });
    });
    document.querySelectorAll(".modal-overlay").forEach(function (overlay) {
      if (overlay.dataset.backdropBound === "1") return;
      overlay.dataset.backdropBound = "1";
      overlay.addEventListener("click", function (e) {
        if (e.target === overlay) window.VZSpring.present(overlay, false, {});
      });
    });
  }

  // ---------------------------------------------------------------
  // Mobile sidebar drawer: swipe-to-close, added alongside the existing
  // hamburger/backdrop-tap close. Enter/exit stays along the same path
  // (slides from the left both ways).
  // ---------------------------------------------------------------
  function initSidebarSwipe() {
    const sidebar = document.querySelector(".sidebar");
    if (!sidebar) return;
    let startX = null;
    sidebar.addEventListener("touchstart", function (e) {
      startX = e.touches[0].clientX;
    }, { passive: true });
    sidebar.addEventListener("touchmove", function (e) {
      if (startX === null) return;
      const dx = e.touches[0].clientX - startX;
      if (dx < 0) sidebar.style.transform = "translateX(" + dx + "px)";
    }, { passive: true });
    sidebar.addEventListener("touchend", function (e) {
      if (startX === null) return;
      const dx = e.changedTouches[0].clientX - startX;
      sidebar.style.transform = "";
      if (dx < -60 && typeof window.closeSidebar === "function") window.closeSidebar();
      startX = null;
    });
  }

  // ---------------------------------------------------------------
  // htmx wiring for the partial-update layer (1.1). The server route, URL,
  // and returned data are unchanged; htmx just extracts the same fragment
  // out of the same full-page response instead of navigating.
  // ---------------------------------------------------------------
  if (window.htmx) {
    document.body.addEventListener("htmx:configRequest", function (e) {
      var meta = document.querySelector('meta[name="csrf-token"]');
      if (meta) e.detail.headers["X-CSRFToken"] = meta.content;
    });
    document.body.addEventListener("htmx:afterSwap", function () {
      initRowNav();
      initScrollFade();
      initModals();
    });
    document.body.addEventListener("htmx:beforeRequest", function (e) {
      const el = e.detail.elt;
      if (el && el.tagName === "FORM") return; // form has its own Saving state
      const trigger = e.detail.requestConfig && e.detail.requestConfig.elt;
      if (trigger && trigger.classList) trigger.classList.add("is-loading");
    });
    document.body.addEventListener("htmx:afterRequest", function (e) {
      const el = e.detail.elt;
      if (el && el.classList) el.classList.remove("is-loading");
      if (!e.detail.successful) {
        window.VZToast.show("Couldn't reach the server — please check your connection and try again.", "error");
      }
    });
    document.body.addEventListener("htmx:sendError", function () {
      window.VZToast.show("Couldn't reach the server — please check your connection and try again.", "error");
    });
  }

  // ---------------------------------------------------------------
  // Wayfinding: "← Back to [where you came from]" using the referring
  // page's own URL (so a filtered/sorted/paginated list state is preserved
  // exactly, not reset to the list's default view). Falls back to hidden
  // if there's no usable same-origin referrer (e.g. opened in a new tab).
  // ---------------------------------------------------------------
  function initBackLinks() {
    document.querySelectorAll(".js-back-link").forEach(function (link) {
      if (link.dataset.backBound === "1") return;
      link.dataset.backBound = "1";
      let ref;
      try {
        ref = document.referrer ? new URL(document.referrer) : null;
      } catch (e) {
        ref = null;
      }
      if (!ref || ref.origin !== window.location.origin || ref.href === window.location.href) return;
      link.href = ref.href;
      const label = link.querySelector(".js-back-link-label");
      if (label && link.dataset.backLabel) label.textContent = link.dataset.backLabel;
      link.style.display = "";
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initRowNav();
    initCollapsibleGroups();
    initScrollFade();
    initModals();
    initSidebarSwipe();
    initBackLinks();
  });
})();
