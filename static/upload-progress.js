/* Auto-enhances every <form enctype="multipart/form-data"> that contains a
 * file input, anywhere in the app — attachment uploads today, and any new
 * upload form added later, with no per-page JS required.
 *
 * Adds, for each such form:
 *   - a "selected file: name (size)" readout the moment a file is chosen
 *   - an immediate client-side block + message if the file exceeds the
 *     server's MAX_CONTENT_LENGTH (kept in sync with app.py's MAX_UPLOAD_MB)
 *   - a real progress bar (via XHR upload.onprogress — fetch() can't do
 *     this) while the upload is in flight, so a large X-ray/PDF over a
 *     slow clinic WiFi connection doesn't look like the page just froze
 *   - a disabled submit button during upload, to prevent double-submits
 */
(function () {
  var MAX_UPLOAD_MB = 100; // must match app.config["MAX_CONTENT_LENGTH"] in app.py

  function formatSize(bytes) {
    if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    if (bytes >= 1024) return Math.round(bytes / 1024) + " KB";
    return bytes + " B";
  }

  function enhanceForm(form) {
    var fileInput = form.querySelector('input[type="file"]');
    if (!fileInput || form.dataset.uploadEnhanced) return;
    form.dataset.uploadEnhanced = "1";

    var submitBtn = form.querySelector('button[type="submit"], input[type="submit"]');

    // Status line: selected file name/size, or a size-limit warning.
    var status = document.createElement("div");
    status.className = "upload-status small muted";
    status.setAttribute("aria-live", "polite");
    fileInput.insertAdjacentElement("afterend", status);

    // Progress bar, hidden until an upload actually starts.
    var progressWrap = document.createElement("div");
    progressWrap.className = "upload-progress-wrap";
    progressWrap.style.display = "none";
    progressWrap.innerHTML =
      '<div class="upload-progress-bar"><div class="upload-progress-fill"></div></div>' +
      '<span class="upload-progress-pct small muted">0%</span>';
    status.insertAdjacentElement("afterend", progressWrap);
    var fill = progressWrap.querySelector(".upload-progress-fill");
    var pctLabel = progressWrap.querySelector(".upload-progress-pct");

    var tooLarge = false;

    fileInput.addEventListener("change", function () {
      tooLarge = false;
      status.classList.remove("upload-status-error");
      var f = fileInput.files && fileInput.files[0];
      if (!f) {
        status.textContent = "Max file size: " + MAX_UPLOAD_MB + " MB.";
        return;
      }
      if (f.size > MAX_UPLOAD_MB * 1024 * 1024) {
        tooLarge = true;
        status.classList.add("upload-status-error");
        status.textContent = f.name + " is " + formatSize(f.size) + " — that's over the " +
          MAX_UPLOAD_MB + " MB limit. Please choose a smaller file.";
      } else {
        status.textContent = "Selected: " + f.name + " (" + formatSize(f.size) + "). Max " + MAX_UPLOAD_MB + " MB.";
      }
    });
    // Initial hint before anything is picked.
    status.textContent = "Max file size: " + MAX_UPLOAD_MB + " MB.";

    form.addEventListener("submit", function (evt) {
      if (tooLarge) {
        evt.preventDefault();
        return;
      }
      var f = fileInput.files && fileInput.files[0];
      if (!f) return; // let normal/native validation (e.g. `required`) handle it

      evt.preventDefault();

      var xhr = new XMLHttpRequest();
      xhr.open("POST", form.action, true);

      xhr.upload.addEventListener("progress", function (e) {
        if (!e.lengthComputable) return;
        var pct = Math.round((e.loaded / e.total) * 100);
        fill.style.width = pct + "%";
        pctLabel.textContent = pct + "%";
      });

      xhr.addEventListener("loadstart", function () {
        progressWrap.style.display = "flex";
        status.textContent = "Uploading " + f.name + "…";
        if (submitBtn) { submitBtn.disabled = true; submitBtn.classList.add("disabled"); }
      });

      xhr.addEventListener("load", function () {
        if ((xhr.status >= 200 && xhr.status < 400) || xhr.status === 413) {
          // Both success and "too large" (413) land on a server redirect
          // with a flash message already queued in the session — follow
          // it either way so the message actually surfaces immediately
          // instead of sitting queued until the next unrelated page load.
          window.location.href = xhr.responseURL || form.action;
        } else {
          status.classList.add("upload-status-error");
          status.textContent = "Upload failed (server returned " + xhr.status + "). Please try again.";
          progressWrap.style.display = "none";
          if (submitBtn) { submitBtn.disabled = false; submitBtn.classList.remove("disabled"); }
        }
      });

      xhr.addEventListener("error", function () {
        status.classList.add("upload-status-error");
        status.textContent = "Upload failed — check your connection and try again.";
        progressWrap.style.display = "none";
        if (submitBtn) { submitBtn.disabled = false; submitBtn.classList.remove("disabled"); }
      });

      xhr.send(new FormData(form));
    });
  }

  function scan() {
    document.querySelectorAll('form[enctype="multipart/form-data"]').forEach(enhanceForm);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan);
  } else {
    scan();
  }

  // Re-scan if a form is injected later (e.g. an upload box toggled into
  // view via JS elsewhere in the app) — cheap MutationObserver, scoped to
  // additions only.
  var mo = new MutationObserver(function (mutations) {
    var needsScan = mutations.some(function (m) { return m.addedNodes.length > 0; });
    if (needsScan) scan();
  });
  mo.observe(document.body, { childList: true, subtree: true });
})();
