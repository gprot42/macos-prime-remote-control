"""Titles that must not appear as Included with Prime."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


def _load_catalog():
    path = Path(__file__).resolve().parent / "prime-catalog.py"
    spec = importlib.util.spec_from_file_location("prime_catalog_mod", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prime_catalog_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


C = _load_catalog()


class KnownNonPrimeTitleTests(unittest.TestCase):
    def test_vampire_diaries_variants(self):
        for title in (
            "The Vampire Diaries",
            "The Vampire Diaries - Season 1",
            "The Vampire Diaries: The Complete First Season",
        ):
            self.assertTrue(
                C.is_known_non_prime_membership_title(title),
                title,
            )

    def test_does_not_match_other_vampire_titles(self):
        for title in (
            "Vampire Academy",
            "Interview With the Vampire",
            "Buffy the Vampire Slayer",
            "Fallout",
        ):
            self.assertFalse(
                C.is_known_non_prime_membership_title(title),
                title,
            )

    def test_demote_clears_prime_flags(self):
        item = C.PrimeTitle(
            title="The Vampire Diaries",
            content_id="0GNJAIZL73W92U15HYFFT3Q58M",
            included_with_prime=True,
            prime_catalog=True,
            availability="Available with Prime subscription",
            focus_message="Watch with a 7 day free Prime trial, auto renews at CHF 9.99/month",
        )
        C.demote_known_non_prime_membership(item)
        self.assertFalse(item.included_with_prime)
        self.assertFalse(item.prime_catalog)
        self.assertEqual(item.availability, "Get an add-on subscription or buy")
        self.assertEqual(item.access_label(), "Rent/Buy")


if __name__ == "__main__":
    unittest.main()

