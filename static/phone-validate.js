/*
 * Client-side mirror of app.py's normalize_phone() — same branch structure,
 * same PHONE_COUNTRY_CODE/PHONE_LOCAL_LENGTH constants, so a number this
 * accepts is exactly a number the server will also accept (and vice versa).
 * Kept as an exact port rather than a single regex because normalize_phone()
 * has multiple accept branches (+E.164, 00-prefixed, local-with-trunk-0,
 * bare local) that don't collapse into one pattern without either rejecting
 * something the server allows or accepting something it doesn't.
 *
 * Flags a bad number on blur (before the person ever tries to submit) using
 * the same .field-invalid/.field-error DOM shape and CSS ui.js's "invalid"
 * listener uses elsewhere, so it looks identical to every other field's
 * validation styling — but shown proactively rather than only at submit
 * time, and without stealing focus back the way a submit-triggered
 * reportValidity() would. setCustomValidity() is also set, so the browser's
 * native submit-time constraint check still blocks the actual submit as a
 * backstop (ui.js's listener handles that path if it's ever hit). The
 * server remains the authority either way — this only saves the round-trip
 * for the common case of a mistyped phone number.
 */
(function () {
  "use strict";

  var COUNTRY_CODE = "962";
  var LOCAL_LENGTH = 9;
  var MESSAGE = "That phone number doesn't look valid — check the digits and try again.";

  function isValidPhone(raw) {
    if (raw == null) return true;
    raw = String(raw).trim();
    if (!raw) return true; // blank is fine — phone is optional
    var digits = raw.replace(/\D/g, "");
    if (!digits) return false;
    if (raw.charAt(0) === "+") {
      return /^\+[1-9]\d{7,14}$/.test("+" + digits);
    }
    if (digits.indexOf("00") === 0) {
      return /^\+[1-9]\d{7,14}$/.test("+" + digits.slice(2));
    }
    var local;
    if (digits.charAt(0) === "0") {
      local = digits.slice(1);
    } else if (digits.indexOf(COUNTRY_CODE) === 0 && digits.length === COUNTRY_CODE.length + LOCAL_LENGTH) {
      local = digits.slice(COUNTRY_CODE.length);
    } else {
      local = digits;
    }
    return local.length === LOCAL_LENGTH;
  }

  function fieldErrorEl(field) {
    var err = field.parentElement.querySelector(".field-error");
    if (!err) {
      err = document.createElement("div");
      err.className = "field-error";
      field.insertAdjacentElement("afterend", err);
    }
    return err;
  }

  function clearError(field) {
    field.classList.remove("field-invalid");
    var err = field.parentElement.querySelector(".field-error");
    if (err) err.remove();
  }

  function check(field, showIfInvalid) {
    var valid = isValidPhone(field.value);
    field.setCustomValidity(valid ? "" : MESSAGE);
    if (valid) {
      clearError(field);
    } else if (showIfInvalid) {
      field.classList.add("field-invalid");
      fieldErrorEl(field).textContent = MESSAGE;
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var fields = document.querySelectorAll("[data-phone-field]");
    for (var i = 0; i < fields.length; i++) {
      (function (field) {
        check(field, false);
        field.addEventListener("blur", function () { check(field, true); });
        field.addEventListener("input", function () { check(field, false); });
      })(fields[i]);
    }
  });
})();
