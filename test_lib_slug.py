"""Unit tests for lib/slug.py — slug normalization and generation."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.slug import normalize_name, generate_slug, SLUG_MODES


def chk(name, ok, detail=""):
    status = "✅" if ok else "❌"
    print(f"  {status} {name} {detail}")
    if not ok:
        global FAILURES
        FAILURES += 1
        FAILURES_LIST.append(name)


FAILURES = 0
FAILURES_LIST = []


def test_normalize():
    """Test normalize_name with various inputs."""

    # Accents
    chk("é → e", normalize_name("Café") == "cafe", f"got {normalize_name('Café')}")
    chk("è → e", normalize_name("Rêve") == "reve")
    chk("ñ → n", normalize_name("Español") == "espanol")

    # Special Latin chars
    chk("Ł → L(lowercase l)", normalize_name("Łódź") == "lodz")
    chk("Ø → o", normalize_name("Søren") == "soren")
    chk("ß → ss", normalize_name("Straße") == "strasse")

    # Multiple accents mixed
    chk("Mixed accents", normalize_name("São Tomé") == "sao-tome")

    # Special chars
    chk("ampersand → empty", normalize_name("Rock & Roll") == "rock-roll")
    chk("quotes stripped", normalize_name("It's Fine") == "its-fine")
    chk("parentheses stripped", normalize_name("Hello (World)") == "hello-world")

    # Spaces
    chk("spaces → hyphens", normalize_name("Hello World") == "hello-world")
    chk("multiple spaces collapse", normalize_name("Hello   World") == "hello-world")

    # Case
    chk("UPPERCASE → lowercase", normalize_name("UPPERCASE") == "uppercase")
    chk("MixedCase", normalize_name("MixedCase") == "mixedcase")

    # Edge cases
    chk("empty → room", normalize_name("") == "room")
    chk("only special chars → room", normalize_name("!!!@@@###") == "room")
    chk("unicode emoji stripped", normalize_name("🎵 Music") == "music")
    chk("leading/trailing hyphens stripped",
        normalize_name("-hello-") == "hello")
    chk("multiple hyphens collapsed",
        normalize_name("hello---world") == "hello-world")


def test_generate():
    """Test generate_slug with all 3 modes."""

    # hex8 mode
    s = generate_slug("Test", "hex8")
    chk("hex8 is 8 chars", len(s) == 8, s)
    chk("hex8 is alphanumeric", all(c in "0123456789abcdef" for c in s), s)

    # name4 mode (default)
    s = generate_slug("Hello World")
    chk("name4 contains name", s.startswith("hello-world"), s)
    chk("name4 has suffix", "-" in s, s)
    chk("name4 last part is 4 hex", len(s.split("-")[-1]) == 4, s)

    # name mode
    s = generate_slug("UniqueName", "name")
    chk("name is normalized", s == "uniquename", f"got {s}")

    # name mode with accents
    s = generate_slug("Déjà Vu", "name")
    chk("name with accents", s == "deja-vu", f"got {s}")

    # SLUG_MODES tuple
    chk("SLUG_MODES has 3 modes", len(SLUG_MODES) == 3)
    chk("SLUG_MODES contains hex8", "hex8" in SLUG_MODES)
    chk("SLUG_MODES contains name4", "name4" in SLUG_MODES)
    chk("SLUG_MODES contains name", "name" in SLUG_MODES)


def main():
    print(f"\n{'='*60}")
    print("  Slug Unit Tests")
    print(f"{'='*60}")
    test_normalize()
    test_generate()
    print(f"\n{'='*60}")
    if FAILURES == 0:
        print("  ✅ All slug tests passed")
    else:
        print(f"  ❌ {FAILURES} test(s) FAILED:")
        for f in FAILURES_LIST:
            print(f"     - {f}")
    print(f"{'='*60}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
