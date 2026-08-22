/*
 * Generic "unsaved changes" guard for ordinary single-form pages (Patient
 * edit, Owner add/edit, Visit new/edit, Inpatient admission, Settings).
 *
 * This is a sibling to unsaved-changes.js, not a replacement for it:
 * unsaved-changes.js is purpose-built for the Inventory Catalog / Price
 * List pattern (many independent per-row forms saved individually via
 * fetch). Pages here have one or more ordinary <form> elements that submit
 * normally via POST — "saving" just means letting that submit happen, so
 * there's no per-row fetch/save-endpoint machinery needed.
 *
 * Usage: add the attribute `data-track-unsaved` to any <form> that should
 * be protected, then include this script near the end of the page. A page
 * can have more than one tracked form (each tracked independently); other,
 * untracked forms on the same page (e.g. a search box) are left alone but
 * will still trigger the confirmation if a tracked form elsewhere on the
 * page is dirty when they're submitted.
 *
 * Behavior:
 *   - Any edit to a tracked form's fields marks that form dirty.
 *   - Submitting a tracked form itself is never blocked — that submit *is*
 *     the save, so it always goes through and clears that form's dirty flag.
 *   - Clicking a link, or submitting any other form on the page, while a
 *     tracked form is dirty is intercepted with a Discard / Keep Editing
 *     confirmation (mirrors unsaved-changes.js, minus the "Save" option,
 *     since there's no way to save a tracked form except its own submit).
 *     Discarding restores every dirty field to its original value before
 *     the navigation/submission proceeds.
 *   - A browser-level beforeunload guard also covers tab close / refresh /
 *     typed URL / back-forward navigation.
 */
(function () {
  const dirtyForms = new Set();
  const originalValues = new Map(); // form -> Map(field -> value/checked)
  let pendingAction = null;
  let bypassNextSubmit = null;

  function trackedForms() {
    return Array.from(document.querySelectorAll("form[data-track-unsaved]"));
  }

  function fieldsFor(form) {
    return Array.from(form.elements).filter(
      (el) => el.name && el.name !== "csrf_token" && !el.disabled
    );
  }

  function snapshot(form) {
    const snap = new Map();
    fieldsFor(form).forEach((el) => {
      if (el.type === "checkbox" || el.type === "radio") snap.set(el, el.checked);
      else snap.set(el, el.value);
    });
    originalValues.set(form, snap);
  }

  function anyDirty() {
    return dirtyForms.size > 0;
  }

  function revert(form) {
    const snap = originalValues.get(form);
    if (!snap) return;
    snap.forEach((val, el) => {
      if (el.type === "checkbox" || el.type === "radio") el.checked = val;
      else el.value = val;
      // Re-fire change so any page-specific handler (e.g. the Follow-Up
      // section toggle on Visit edit) re-syncs its own display too.
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  function discardDirty() {
    dirtyForms.forEach(revert);
    dirtyForms.clear();
  }

  document.addEventListener("DOMContentLoaded", () => {
    trackedForms().forEach(snapshot);
  });

  function onFieldChange(e) {
    const el = e.target;
    if (!el || !el.form || !el.form.hasAttribute("data-track-unsaved")) return;
    if (!originalValues.has(el.form)) snapshot(el.form); // e.g. a form revealed after page load
    dirtyForms.add(el.form);
  }
  document.addEventListener("input", onFieldChange, true);
  document.addEventListener("change", onFieldChange, true);

  // ---- Modal ----
  function ensureModal() {
    if (document.getElementById("unsavedFormModalOverlay")) return;
    const wrap = document.createElement("div");
    wrap.innerHTML =
      '<div id="unsavedFormModalOverlay" class="modal-overlay" style="display:none;">' +
      '<div class="modal-box" style="max-width:420px;">' +
      '<div class="section-title" style="margin-top:0;">Unsaved Changes</div>' +
      '<p class="small muted" style="margin-bottom:6px;">You have unsaved changes on this page. Leave without saving?</p>' +
      '<div class="form-actions" style="justify-content:flex-end; margin-top:18px;">' +
      '<button class="btn small secondary" type="button" id="unsavedFormCancelBtn">Keep Editing</button>' +
      '<button class="btn small danger" type="button" id="unsavedFormDiscardBtn">Discard Changes</button>' +
      "</div></div></div>";
    document.body.appendChild(wrap.firstElementChild);
    document.getElementById("unsavedFormCancelBtn").addEventListener("click", hideModal);
    document.getElementById("unsavedFormDiscardBtn").addEventListener("click", proceed);
  }

  function showModal() {
    ensureModal();
    document.getElementById("unsavedFormModalOverlay").style.display = "flex";
  }

  function hideModal() {
    const el = document.getElementById("unsavedFormModalOverlay");
    if (el) el.style.display = "none";
    pendingAction = null;
  }

  function proceed() {
    const action = pendingAction;
    discardDirty();
    hideModal();
    if (!action) return;
    if (action.type === "nav") {
      window.location.href = action.href;
    } else if (action.type === "submit") {
      bypassNextSubmit = action.form;
      // requestSubmit() (not submit()) so the form's own submit event still
      // fires normally on this second, allowed pass — e.g. so HTML5
      // validation still runs as usual; only our interception is skipped.
      action.form.requestSubmit();
    }
  }

  // ---- Intercept link clicks while dirty ----
  document.addEventListener(
    "click",
    function (e) {
      if (!anyDirty()) return;
      const a = e.target.closest("a[href]");
      if (!a) return;
      if (a.target === "_blank" || a.hasAttribute("data-skip-dirty-check")) return;
      const href = a.getAttribute("href");
      if (!href || href.startsWith("#") || href.startsWith("javascript:")) return;
      e.preventDefault();
      e.stopPropagation();
      pendingAction = { type: "nav", href: a.href };
      showModal();
    },
    true
  );

  // ---- Intercept other form submissions while a tracked form is dirty ----
  document.addEventListener(
    "submit",
    function (e) {
      const form = e.target;
      if (form === bypassNextSubmit) {
        bypassNextSubmit = null;
        return;
      }
      if (form.hasAttribute("data-track-unsaved")) {
        // A tracked form's own submit is the "save" — never block it.
        dirtyForms.delete(form);
        return;
      }
      if (!anyDirty()) return;
      e.preventDefault();
      e.stopPropagation();
      pendingAction = { type: "submit", form: form };
      showModal();
    },
    true
  );

  // ---- Browser-level guard: refresh, close tab, typed URL, back/forward ----
  window.addEventListener("beforeunload", function (e) {
    if (anyDirty()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
})();
