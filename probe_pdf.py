"""Diagnostic: show pdfplumber table and word output for a pay stub PDF.

Usage:
    python probe_pdf.py path/to/stub.pdf
"""
import sys
import pdfplumber

def main():
    if len(sys.argv) < 2:
        print("Usage: python probe_pdf.py <stub.pdf>")
        sys.exit(1)

    path = sys.argv[1]
    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            print(f"\n{'='*72}")
            print(f"PAGE {page_num}  (width={page.width:.0f}, height={page.height:.0f})")
            print(f"{'='*72}")

            # ---- 1. Tables ------------------------------------------------
            tables = page.extract_tables()
            print(f"\n--- extract_tables(): found {len(tables)} table(s) ---")
            for t_idx, table in enumerate(tables):
                print(f"\n  Table {t_idx} ({len(table)} rows x {len(table[0]) if table else 0} cols):")
                for r_idx, row in enumerate(table):
                    cells = [repr(c or "") for c in row]
                    print(f"    row {r_idx:>2}: {' | '.join(cells)}")

            # ---- 2. Words with coordinates (first 60) --------------------
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            print(f"\n--- extract_words(): {len(words)} words total (first 80 shown) ---")
            print(f"  {'x0':>6} {'top':>6}  text")
            for w in words[:80]:
                print(f"  {w['x0']:>6.1f} {w['top']:>6.1f}  {w['text']}")

            # ---- 3. Raw text (current approach) --------------------------
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            print(f"\n--- extract_text() (current approach) ---")
            for i, line in enumerate(text.splitlines()):
                print(f"  {i:>3}: {line}")

if __name__ == "__main__":
    main()
