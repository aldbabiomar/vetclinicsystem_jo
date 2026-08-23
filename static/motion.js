/*
 * Tiny spring-based motion helper (no external animation library required —
 * vendored the same way as everything else in static/vendor since this app
 * runs offline on clinic WiFi). Implements a critically-damped spring
 * integrator so animations settle naturally and can be interrupted/redirected
 * mid-flight (unlike a fixed-duration CSS transition), per §4 of the fluid
 * interfaces framework this plan is built against.
 *
 * Two presets, matching the plan:
 *   VZSpring.presets.ui       — damping 1.0,  response 0.32s  (default UI motion)
 *   VZSpring.presets.momentum — damping 0.8,  response 0.32s  (flick/drag release)
 *
 * Respects prefers-reduced-motion: reduce automatically (§1.6) — animations
 * resolve instantly to their end state instead of running, no separate
 * per-call opt-out needed.
 */
(function (global) {
  const reduceMotion = () =>
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const presets = {
    ui: { damping: 1.0, response: 0.32 },
    momentum: { damping: 0.8, response: 0.32 },
  };

  // Critically/under-damped spring toward `to`, calling onUpdate(value) every
  // frame and onDone() once settled. Returns a cancel() function so a new
  // gesture/interaction can redirect motion already in flight.
  function animate(from, to, preset, onUpdate, onDone) {
    if (reduceMotion()) {
      onUpdate(to);
      if (onDone) onDone();
      return function cancel() {};
    }
    const { damping, response } = preset;
    const angularFreq = (2 * Math.PI) / response;
    let value = from;
    let velocity = 0;
    let raf = null;
    let last = performance.now();

    function step(now) {
      const dt = Math.min((now - last) / 1000, 0.032);
      last = now;
      const displacement = value - to;
      const springForce = -angularFreq * angularFreq * displacement;
      const dampingForce = -2 * damping * angularFreq * velocity;
      velocity += (springForce + dampingForce) * dt;
      value += velocity * dt;

      const settled = Math.abs(value - to) < 0.001 && Math.abs(velocity) < 0.001;
      if (settled) {
        onUpdate(to);
        if (onDone) onDone();
        return;
      }
      onUpdate(value);
      raf = requestAnimationFrame(step);
    }
    raf = requestAnimationFrame(step);
    return function cancel() {
      if (raf) cancelAnimationFrame(raf);
    };
  }

  // Convenience: spring an element's opacity/transform (scale + translateY)
  // from a "hidden" state to "shown" (or reverse), anchored to a trigger
  // element for transform-origin when provided (§7 spatial consistency).
  function present(el, show, opts) {
    opts = opts || {};
    const preset = presets[opts.preset || "ui"];
    const originEl = opts.originEl;
    if (originEl && el) {
      const originRect = originEl.getBoundingClientRect();
      const targetRect = el.getBoundingClientRect();
      const ox = originRect.left + originRect.width / 2 - (targetRect.left + targetRect.width / 2);
      const oy = originRect.top + originRect.height / 2 - (targetRect.top + targetRect.height / 2);
      el.style.transformOrigin = "calc(50% + " + ox + "px) calc(50% + " + oy + "px)";
    }
    if (el._vzCancel) el._vzCancel();
    if (show) {
      el.style.display = opts.display || "flex";
      el.style.pointerEvents = "auto";
      const startScale = 0.92,
        startOpacity = 0;
      el._vzCancel = animate(0, 1, preset, function (t) {
        const scale = startScale + (1 - startScale) * t;
        el.style.opacity = String(startOpacity + (1 - startOpacity) * t);
        el.style.transform = "scale(" + scale + ")";
      });
    } else {
      el.style.pointerEvents = "none";
      el._vzCancel = animate(1, 0, preset, function (t) {
        el.style.opacity = String(t);
        el.style.transform = "scale(" + (0.92 + 0.08 * t) + ")";
      }, function () {
        el.style.display = "none";
      });
    }
  }

  global.VZSpring = { animate: animate, present: present, presets: presets, reduceMotion: reduceMotion };
})(window);
