/*
 * Shared toast notification component (Part 1.2 of the Experience
 * Enhancement Plan). Replaces the top-of-<main> .flash banner: same
 * messages, same success/error/warning/status states — just anchored where
 * the person is looking, auto-dismissing, and not scrolled out of view on a
 * long form.
 *
 * Usage: VZToast.show("Saved.", "success")
 * States: "success" | "error" | "warn" | "status" (status = in-progress,
 * doesn't auto-dismiss until replaced/dismissed — used for long-running
 * actions per 1.9).
 */
(function (global) {
  function ensureContainer() {
    let c = document.getElementById("vz-toast-stack");
    if (!c) {
      c = document.createElement("div");
      c.id = "vz-toast-stack";
      c.className = "vz-toast-stack";
      c.setAttribute("aria-live", "polite");
      c.setAttribute("aria-atomic", "false");
      document.body.appendChild(c);
    }
    return c;
  }

  function iconFor(kind) {
    if (kind === "success") return "✓";
    if (kind === "error") return "!";
    if (kind === "warn") return "!";
    if (kind === "status") return "…";
    return "";
  }

  function show(message, kind, opts) {
    kind = kind || "status";
    opts = opts || {};
    const stack = ensureContainer();
    const toast = document.createElement("div");
    toast.className = "vz-toast vz-toast-" + kind;
    toast.setAttribute("role", kind === "error" ? "alert" : "status");
    toast.innerHTML =
      '<span class="vz-toast-icon">' + iconFor(kind) + "</span>" +
      '<span class="vz-toast-msg"></span>' +
      (opts.dismissible !== false ? '<button type="button" class="vz-toast-close" aria-label="Dismiss">&times;</button>' : "");
    toast.querySelector(".vz-toast-msg").textContent = message;
    stack.appendChild(toast);

    function remove() {
      if (!toast.parentNode) return;
      toast.classList.add("vz-toast-out");
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 180);
    }
    const closeBtn = toast.querySelector(".vz-toast-close");
    if (closeBtn) closeBtn.addEventListener("click", remove);

    requestAnimationFrame(function () {
      toast.classList.add("vz-toast-in");
    });

    let timer = null;
    const duration = opts.duration || (kind === "error" ? 6000 : 4000);
    if (kind !== "status") {
      timer = setTimeout(remove, duration);
    }
    return {
      dismiss: remove,
      update: function (newMessage, newKind) {
        if (newKind) {
          toast.className = "vz-toast vz-toast-" + newKind + " vz-toast-in";
          toast.querySelector(".vz-toast-icon").textContent = iconFor(newKind);
        }
        toast.querySelector(".vz-toast-msg").textContent = newMessage;
        if (newKind && newKind !== "status") {
          if (timer) clearTimeout(timer);
          timer = setTimeout(remove, duration);
        }
      },
    };
  }

  // Convert any server-rendered .flash banners (still emitted by Flask's
  // flash()) into toasts on load, instead of a static top-of-page banner —
  // same copy, same success/error state, delivered where a person filling
  // out a long form will actually see it.
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("main .flash, .auth-flash-wrap .flash").forEach(function (el) {
      const kind = el.classList.contains("error") ? "error" : el.classList.contains("success") ? "success" : "status";
      show(el.textContent.trim(), kind);
      el.remove();
    });
  });

  global.VZToast = { show: show };
})(window);
