"""
Generates a printable, scannable internal barcode for inventory items that
don't already have a manufacturer barcode. Uses the EAN-13 format (12 digits
+ 1 check digit) so any standard barcode scanner/printer can read it; the
"20" prefix is the range officially reserved for in-store/internal use.
"""
import random


def _ean13_check_digit(digits12):
    total = 0
    for i, d in enumerate(digits12):
        n = int(d)
        total += n if i % 2 == 0 else n * 3
    return (10 - (total % 10)) % 10


def generate_barcode(db):
    for _ in range(50):
        body = "20" + "".join(str(random.randint(0, 9)) for _ in range(9))  # 11 digits
        code12 = body
        check = _ean13_check_digit(code12)
        candidate = code12 + str(check)
        existing = db.execute("SELECT 1 FROM inventory_list WHERE barcode=?", (candidate,)).fetchone()
        if not existing:
            return candidate
    raise RuntimeError("Could not generate a unique barcode — try again.")
