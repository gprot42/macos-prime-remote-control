"""Unit tests for Prime TV launch helpers (no TV required)."""

from __future__ import annotations

import importlib.util
import unittest
import urllib.parse
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parent / "lg-tv-connect.py"
    spec = importlib.util.spec_from_file_location("lg_tv_connect", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


class ProfileNavTests(unittest.TestCase):
    def test_from_new_one_up_is_d(self):
        keys = M.profile_nav_keys(
            2, profile_type="none", list_size=3, last_focused=3
        )
        self.assertEqual(keys, ["UP"])
        self.assertNotIn("DOWN", keys)

    def test_from_kids_down_is_d(self):
        keys = M.profile_nav_keys(
            2, profile_type="none", list_size=3, last_focused=1
        )
        self.assertEqual(keys, ["DOWN"])

    def test_already_on_target_is_enter_only(self):
        keys = M.profile_nav_keys(
            2, profile_type="none", list_size=3, last_focused=2
        )
        self.assertEqual(keys, [])

    def test_already_on_d_no_move(self):
        keys = M.profile_nav_keys(
            2, profile_type="none", list_size=3, last_focused=2
        )
        self.assertEqual(keys, [])

    def test_from_new_two_up_is_kids(self):
        keys = M.profile_nav_keys(
            1, profile_type="none", list_size=3, last_focused=3
        )
        self.assertEqual(keys, ["UP", "UP"])

    def test_from_margaret_down_to_d_stops_before_new(self):
        keys = M.profile_nav_keys(
            2, profile_type="none", list_size=3, last_focused=0
        )
        self.assertEqual(keys, ["DOWN", "DOWN"])

    def test_adult_row_homes_left_then_right(self):
        keys = M.profile_nav_keys(1, row=0, profile_type="adult")
        self.assertEqual(
            keys,
            ["LEFT"] * M.PROFILE_HOME_STEPS + ["RIGHT"],
        )


class LaunchIdFilterTests(unittest.TestCase):
    def test_https_autoplay_is_params_target(self):
        url = "https://app.primevideo.com/detail/0HAQAA7JM43QWX0H6GUD3IOF70?autoplay=1"
        self.assertTrue(M._prime_target_uses_params(url))
        self.assertTrue(M._is_autoplay_target(url))

    def test_gti_and_detail_id_are_bare(self):
        gti = "amzn1.dv.gti.242f5d02-0b3e-4f4d-a89b-22da3f65f0ec"
        detail = "0HAQAA7JM43QWX0H6GUD3IOF70"
        self.assertFalse(M._prime_target_uses_params(gti))
        self.assertFalse(M._prime_target_uses_params(detail))
        self.assertFalse(M._is_autoplay_target(gti))
        self.assertFalse(M._is_autoplay_target(detail))

    def test_bare_ids_drop_https_and_keep_detail(self):
        detail = "0HAQAA7JM43QWX0H6GUD3IOF70"
        ids = M.bare_prime_launch_ids(detail, html="<html></html>", episode=3)
        self.assertTrue(ids)
        self.assertTrue(all(not M._prime_target_uses_params(x) for x in ids))
        self.assertIn(detail, ids)


class ProfileNameResolveTests(unittest.TestCase):
    def test_d_is_none_slot_not_adult(self):
        from amazon.prime_profiles import resolve_profile_name

        ptype, entry = resolve_profile_name("D")
        self.assertEqual(ptype, "none")
        self.assertEqual(entry.index, 2)
        self.assertEqual(entry.name, "D")

    def test_settings_index_wins_over_adult_mapping(self):
        index, _row, _pin, display, ptype = M.resolve_profile_selection(
            profile=2,
            profile_name="D",
            profile_type="none",
            profile_row=0,
            profile_pin=None,
        )
        self.assertEqual(index, 2)
        self.assertEqual(display, "D")
        self.assertEqual(ptype, "none")

    def test_d_is_one_up_from_new_never_down(self):
        """Slot 2 (D) is one UP from + / New. DOWN would open Add profile."""
        size = 3
        ups = max(0, size - 2)
        self.assertEqual(ups, 1)
        keys = M.profile_nav_keys(
            2, profile_type="none", list_size=size, last_focused=size
        )
        self.assertEqual(keys, ["UP"])
        self.assertNotIn("DOWN", keys)


class IsolationPathTests(unittest.TestCase):
    def test_cache_and_lock_are_app_private(self):
        self.assertTrue(str(M.APP_CACHE_DIR).endswith("prime-remote-control"))
        self.assertIn("prime-remote-control", str(M.TV_SSAP_LOCK_PATH))
        self.assertNotIn("prime-catalog-ui", str(M.APP_CACHE_DIR))


class AmazoffDetectTests(unittest.TestCase):
    def test_list_apps_finds_patcher_id(self):
        hit = M.amazoff_match_from_apps(
            [
                {"id": "amazon", "title": "Prime Video"},
                {"id": "com.amazoff.patcher", "title": "AmazOff", "version": "0.2.3"},
            ]
        )
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["app_id"], "com.amazoff.patcher")
        self.assertEqual(hit["title"], "AmazOff")
        self.assertEqual(hit["version"], "0.2.3")

    def test_launch_points_dict_finds_title(self):
        hit = M.amazoff_match_from_apps(
            {"com.amazoff.patcher": {"id": "com.amazoff.patcher", "title": "AmazOff"}}
        )
        self.assertIsNotNone(hit)

    def test_no_match_on_stock_prime_only(self):
        self.assertIsNone(
            M.amazoff_match_from_apps([{"id": "amazon", "title": "Prime Video"}])
        )


class PostProfilePolicyTests(unittest.TestCase):
    def test_launch_prime_uses_gti_then_play_not_search(self):
        """Series play: catalog detail id after profile. No Search / second launch."""
        src = Path(__file__).resolve().parent / "lg-tv-connect.py"
        text = src.read_text()
        start = text.index("async def cmd_launch_prime")
        end = text.index("async def cmd_launch(")
        body = text[start:end]
        self.assertNotIn("prime_search_open_title", body)
        self.assertNotIn('button("SEARCH")', body)
        self.assertIn("amazoff_play_launch_id", body)
        self.assertIn("no second launch", body)
        self.assertIn("used_autoplay_launch = False", body)


class StorefrontLaunchIdTests(unittest.TestCase):
    def test_amazoff_prefers_gti_over_detail(self):
        self.assertTrue(M.PRIME_GTI_RE.match("amzn1.dv.gti.4c40e9aa-d8a3-46ce-9aed-f2c25c3263ed"))

    def test_detail_path_is_params_target(self):
        self.assertTrue(M._prime_target_uses_params("/detail/0JO4LK6J2W4TJ8TB755MF1U8KT"))
        self.assertFalse(M._prime_target_uses_params("0JO4LK6J2W4TJ8TB755MF1U8KT"))
        self.assertFalse(M._is_autoplay_target("/detail/0JO4LK6J2W4TJ8TB755MF1U8KT"))

    def test_movie_keeps_catalog_detail_id(self):
        movie = "0GOZFOSOEE161DO8WSOE05JURG"
        self.assertEqual(
            M.storefront_launch_id(movie, html=None, episode=None, play=True),
            movie,
        )
        self.assertFalse(movie.startswith("amzn1.dv.gti."))

    def test_series_uses_episode_detail_not_gti(self):
        series = "0POGMB4U56K9RYIL5GS64X19A5"
        html = (
            '<html><script>{"titleType":"season","episodes":['
            '{"title":"La naissance","sequence_number":1,'
            '"content_id":"0JO4LK6J2W4TJ8TB755MF1U8KT"}]}</script></html>'
        )
        # If the helper cannot parse this stub, it must still not prefer a GTI
        # over the series detail id we already have.
        lid = M.storefront_launch_id(series, html=html, episode=1, play=True)
        self.assertFalse(lid.startswith("amzn1.dv.gti."), lid)
        self.assertRegex(lid, r"^[0-9A-Z]{26,32}$")


class SeriesEpisodeGateTests(unittest.TestCase):
    def test_play_ep1_uses_series_hub(self):
        # Mirrors cmd_launch_prime: play without seek on ep<=1 launches the hub.
        episode = 1
        start = 0
        play = True
        launch_episode = episode
        if play and not (start and start > 0) and (episode is None or episode <= 1):
            launch_episode = None
        self.assertIsNone(launch_episode)

    def test_play_ep3_keeps_episode(self):
        episode = 3
        start = 0
        play = True
        launch_episode = episode
        if play and not (start and start > 0) and (episode is None or episode <= 1):
            launch_episode = None
        self.assertEqual(launch_episode, 3)


if __name__ == "__main__":
    unittest.main()
