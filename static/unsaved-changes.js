/*
 * Shared "unsaved changes" tracking for pages with per-row inline-edit forms
 * (Inventory Catalog, Price List). Each row is its own <form id="edit-...">
 * whose fields live in the table row via the form="edit-..." attribute —
 * this script auto-discovers every such form on the page, so no per-page
 * configuration is needed beyond including this file and adding:
 *   - a <button id="saveChangesBtn"> in the page header
 *   - data-edit-form="edit-<id>" on each editable <tr>
 *
 * Behavior:
 *   - Any edit to a tracked row's fields marks it "dirty" and highlights the
 *     row, and enables the top Save Changes button. The field's original
 *     value is captured the moment it first becomes dirty.
 *   - Clicking the top Save Changes button saves every dirty row (via
 *     fetch, reusing each row's existing save endpoint) and reloads —
 *     only once every save has actually succeeded.
 *   - Clicking any link, or submitting any other form on the page (search,
 *     +New Item, Remove/Deactivate, pagination, etc.) while changes are
 *     unsaved is intercepted with a confirmation modal offering Save,
 *     Discard, or Keep Editing.
 *       - Save & Continue only navigates once every dirty row has actually
 *         saved successfully; if any save fails, it stays on the page and
 *         says so, instead of silently losing the edit or leaving with no
 *         explanation.
 *       - Discard Changes restores every dirty field to the value it had
 *         before editing (not just forgetting that it was edited) and then
 *         completes the navigation/submission that was clicked.
 *   - A browser-level beforeunload guard also covers tab close / refresh /
 *     typed URL / back-forward navigation (with the browser's own generic
 *     warning, since custom text isn't permitted there by modern browsers).
 */
(function () {
  const dirtyForms = new Set();
  const dirtyNames = new Map();
  const originalValues = new Map(); // formId -> Map(fieldEl -> original value/checked)
  let pendingAction = null;
  let bypassNextSubmit = null;
  let reverting = false;

  function editForms() {
    return Array.from(document.querySelectorAll('form[id^="edit-"]'));
  }

  function trackedFormIds() {
    return new Set(editForms().map((f) => f.id));
  }

  function fieldsFor(formId) {
    return Array.from(document.querySelectorAll('[form="' + formId + '"]')).filter(
      (el) => el.name && el.name !== "csrf_token"
    );
  }

  function rowNameFor(formId) {
    const nameField = document.querySelector('[form="' + formId + '"][name="name"]');
    return nameField && nameField.value ? nameField.value : formId.replace(/^edit-/, "");
  }

  function rowElFor(formId) {
    return document.querySelector('tr[data-edit-form="' + formId + '"]');
  }

  function updateSaveButton() {
    const btn = document.getElementById("saveChangesBtn");
    if (!btn) return;
    btn.disabled = dirtyForms.size === 0;
    btn.textContent = dirtyForms.size > 0 ? "Save Changes (" + dirtyForms.size + ")" : "Save Changes";
  }

  function snapshotForm(formId) {
    const snap = new Map();
    fieldsFor(formId).forEach((el) => {
      if (el.type === "checkbox" || el.type === "radio") snap.set(el, el.checked);
      else snap.set(el, el.value);
    });
    originalValues.set(formId, snap);
  }

  function revertForm(formId) {
    const snap = originalValues.get(formId);
    if (!snap) return;
    reverting = true;
    snap.forEach((val, el) => {
      if (el.type === "checkbox" || el.type === "radio") el.checked = val;
      else el.value = val;
      // Re-fire change so any page-specific handler (e.g. the distributor
      // label swap on Inventory Catalog) re-syncs its own display too.
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
    reverting = false;
  }

  function markDirty(formId) {
    if (dirtyForms.has(formId)) return;
    if (!originalValues.has(formId)) snapshotForm(formId);
    dirtyForms.add(formId);
    dirtyNames.set(formId, rowNameFor(formId));
    const tr = rowElFor(formId);
    if (tr) tr.classList.add("row-dirty");
    updateSaveButton();
  }

  function forgetForm(formId) {
    dirtyForms.delete(formId);
    dirtyNames.delete(formId);
    originalValues.delete(formId);
    const tr = rowElFor(formId);
    if (tr) tr.classList.remove("row-dirty");
  }

  function discardDirty() {
    Array.from(dirtyForms).forEach((formId) => {
      revertForm(formId);
      forgetForm(formId);
    });
    updateSaveButton();
  }

  function onFieldChange(e) {
    if (reverting) return;
    const el = e.target;
    if (!el || !el.form) return;
    if (trackedFormIds().has(el.form.id)) markDirty(el.form.id);
  }
  document.addEventListener("input", onFieldChange, true);
  document.addEventListener("change", onFieldChange, true);

  function bulkUrl() {
    const btn = document.getElementById("saveChangesBtn");
    return btn ? btn.dataset.bulkUrl : null;
  }

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  // Saves every dirty row and returns true only if every single one
  // actually succeeded. Rows that fail stay marked dirty so nothing is
  // silently lost and the user can see exactly what still needs saving.
  //
  // Prefers a single batched request (one DB transaction, one financial
  // recompute at most, instead of one of each per row — this is what makes
  // saving many rows at once fast instead of laggy). Falls back to the old
  // one-request-per-row behavior only if the page hasn't configured a
  // bulk endpoint via #saveChangesBtn[data-bulk-url].
  async function saveDirty() {
    const url = bulkUrl();
    const ids = Array.from(dirtyForms);
    if (url) {
      const items = ids.map((formId) => {
        const rowId = formId.replace(/^edit-/, "");
        const fields = {};
        fieldsFor(formId).forEach((el) => {
          if (el.type === "checkbox" || el.type === "radio") fields[el.name] = el.checked ? "on" : "";
          else fields[el.name] = el.value;
        });
        return { id: rowId, fields: fields };
      });
      let data = null;
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
          body: JSON.stringify({ items: items }),
          credentials: "same-origin",
        });
        if (res.ok) data = await res.json().catch(() => null);
      } catch (err) {
        data = null;
      }
      if (!data) {
        updateSaveButton();
        return false; // network/server error — leave everything marked dirty to retry
      }
      const failedIds = new Set(Object.keys(data.errors || {}));
      ids.forEach((formId) => {
        const rowId = formId.replace(/^edit-/, "");
        if (!failedIds.has(rowId)) forgetForm(formId);
      });
      updateSaveButton();
      return !!data.ok;
    }

    // Fallback: no bulk endpoint configured — save each row individually.
    const results = await Promise.all(
      ids.map(async (id) => {
        const form = document.getElementById(id);
        if (!form) return { id, ok: true };
        try {
          const res = await fetch(form.action, {
            method: "POST",
            body: new FormData(form),
            credentials: "same-origin",
          });
          return { id, ok: res.ok };
        } catch (err) {
          return { id, ok: false };
        }
      })
    );
    let allOk = true;
    results.forEach(({ id, ok }) => {
      if (ok) forgetForm(id);
      else allOk = false;
    });
    updateSaveButton();
    return allOk;
  }

  // ---- Top "Save Changes" button ----
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("saveChangesBtn");
    if (btn) {
      btn.addEventListener("click", async () => {
        if (dirtyForms.size === 0) return;
        btn.disabled = true;
        btn.textContent = "Saving…";
        const ok = await saveDirty();
        if (!ok) {
          updateSaveButton();
          alert(
            "Some changes couldn't be saved — please check your connection and try again. " +
            "The items that failed are still highlighted."
          );
          return;
        }
        window.location.reload();
      });
    }
    updateSaveButton();
  });

  // ---- Modal ----
  function ensureModal() {
    if (document.getElementById("unsavedModalOverlay")) return;
    const wrap = document.createElement("div");
    wrap.innerHTML =
      '<div id="unsavedModalOverlay" class="modal-overlay" style="display:none;">' +
      '<div class="modal-box" style="max-width:460px;">' +
      '<div class="section-title" style="margin-top:0;">Unsaved Changes</div>' +
      '<p class="small muted" id="unsavedModalMsg" style="margin-bottom:6px;"></p>' +
      '<div class="form-actions" style="justify-content:flex-end; margin-top:18px;">' +
      '<button class="btn small secondary" type="button" id="unsavedCancelBtn">Keep Editing</button>' +
      '<button class="btn small danger" type="button" id="unsavedDiscardBtn">Discard Changes</button>' +
      '<button class="btn small" type="button" id="unsavedSaveBtn">Save &amp; Continue</button>' +
      "</div></div></div>";
    document.body.appendChild(wrap.firstElementChild);
    document.getElementById("unsavedCancelBtn").addEventListener("click", hideModal);
    document.getElementById("unsavedDiscardBtn").addEventListener("click", () => proceed(false));
    document.getElementById("unsavedSaveBtn").addEventListener("click", () => proceed(true));
  }

  function showModal() {
    ensureModal();
    const names = Array.from(dirtyNames.values());
    const list =
      names.length <= 4 ? names.join(", ") : names.slice(0, 4).join(", ") + ", and " + (names.length - 4) + " more";
    document.getElementById("unsavedModalMsg").textContent =
      "You have unsaved changes on " +
      dirtyForms.size +
      (dirtyForms.size === 1 ? " item" : " items") +
      " (" +
      list +
      "). Save them before leaving, or discard them?";
    document.getElementById("unsavedModalOverlay").style.display = "flex";
    // Reset button states/labels in case a previous attempt left them mid-save.
    const saveBtn = document.getElementById("unsavedSaveBtn");
    const discardBtn = document.getElementById("unsavedDiscardBtn");
    const cancelBtn = document.getElementById("unsavedCancelBtn");
    [saveBtn, discardBtn, cancelBtn].forEach((b) => { b.disabled = false; });
    saveBtn.textContent = "Save & Continue";
  }

  function hideModal() {
    const el = document.getElementById("unsavedModalOverlay");
    if (el) el.style.display = "none";
    pendingAction = null;
  }

  async function proceed(save) {
    const action = pendingAction;
    if (!action) {
      hideModal();
      return;
    }
    if (save) {
      const saveBtn = document.getElementById("unsavedSaveBtn");
      const discardBtn = document.getElementById("unsavedDiscardBtn");
      const cancelBtn = document.getElementById("unsavedCancelBtn");
      [saveBtn, discardBtn, cancelBtn].forEach((b) => { b.disabled = true; });
      saveBtn.textContent = "Saving…";
      const ok = await saveDirty();
      if (!ok) {
        [saveBtn, discardBtn, cancelBtn].forEach((b) => { b.disabled = false; });
        saveBtn.textContent = "Save & Continue";
        alert(
          "Some changes couldn't be saved — please check your connection and try again. " +
          "You're still on this page and nothing else has been lost."
        );
        return; // stay put; don't navigate away from a failed save
      }
    } else {
      discardDirty();
    }
    hideModal();
    if (action.type === "nav") {
      window.location.href = action.href;
    } else if (action.type === "submit") {
      bypassNextSubmit = action.form;
      // requestSubmit() (not submit()) so the form's own submit event still
      // fires normally on this second, allowed pass — e.g. so a "Remove"
      // row's own confirm() dialog and any HTML5 validation still run as
      // usual; only OUR interception is skipped for this one resubmission.
      action.form.requestSubmit();
    }
  }

  // ---- Intercept link clicks while dirty ----
  document.addEventListener(
    "click",
    function (e) {
      if (dirtyForms.size === 0) return;
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

  // ---- Intercept other form submissions while dirty (search, +New Item, Remove, pagination forms, etc.) ----
  document.addEventListener(
    "submit",
    function (e) {
      const form = e.target;
      if (form === bypassNextSubmit) {
        bypassNextSubmit = null;
        return;
      }
      if (dirtyForms.size === 0) return;
      if (trackedFormIds().has(form.id)) return; // one of our own row-edit forms — let it submit normally
      e.preventDefault();
      e.stopPropagation();
      pendingAction = { type: "submit", form: form };
      showModal();
    },
    true
  );

  // ---- Browser-level guard: refresh, close tab, typed URL, back/forward ----
  window.addEventListener("beforeunload", function (e) {
    if (dirtyForms.size > 0) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
})();
