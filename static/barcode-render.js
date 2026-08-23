// Shared by barcode_label.html and barcode_bulk_print.html — one place
// for the format-detection + checksum logic so a future fix to it can't
// land in only one of the two label pages.
//
// Auto-generated internal codes are always 13-digit EAN-13 with a correct
// check digit, but this field can also hold a manufacturer's own barcode
// (scanned, typed in, or carried over from imported data) — those come in
// whatever format the manufacturer used. A code that's the *right length*
// for EAN-13/UPC-A/EAN-8 doesn't necessarily have a *valid* check digit
// for it (a fat-fingered manual entry, a legacy record, test data,
// anything) — and JsBarcode strictly validates that checksum and throws
// rather than rendering something wrong. Checking length alone isn't
// enough; only pick a checksummed format when the checksum actually
// checks out, so a numeric-but-invalid code still renders (as CODE128,
// which has no checksum requirement) instead of failing outright.
function vzBarcodeFormat(code) {
  function eanCheckDigit(bodyDigits) {
    // Standard EAN check digit: rightmost body digit gets weight 3,
    // alternating from there. Works for both EAN-13 (12-digit body) and
    // EAN-8 (7-digit body).
    let total = 0;
    const reversed = bodyDigits.split('').reverse();
    for (let i = 0; i < reversed.length; i++) {
      total += parseInt(reversed[i], 10) * (i % 2 === 0 ? 3 : 1);
    }
    return (10 - (total % 10)) % 10;
  }
  function upcCheckDigit(body11) {
    let oddSum = 0, evenSum = 0;
    for (let i = 0; i < 11; i++) {
      const d = parseInt(body11[i], 10);
      if (i % 2 === 0) oddSum += d; else evenSum += d;
    }
    return (10 - ((oddSum * 3 + evenSum) % 10)) % 10;
  }
  if (/^\d{13}$/.test(code) && eanCheckDigit(code.slice(0, 12)) === parseInt(code[12], 10)) return 'EAN13';
  if (/^\d{12}$/.test(code) && upcCheckDigit(code.slice(0, 11)) === parseInt(code[11], 10)) return 'UPC';
  if (/^\d{8}$/.test(code) && eanCheckDigit(code.slice(0, 7)) === parseInt(code[7], 10)) return 'EAN8';
  return 'CODE128';
}

function vzRenderBarcode(target, code, extraOptions) {
  const format = vzBarcodeFormat(code);
  JsBarcode(target, code, Object.assign({ format: format, width: 2, height: 60, fontSize: 14 }, extraOptions || {}));
  return format;
}
