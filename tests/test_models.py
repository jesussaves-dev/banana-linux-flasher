# -*- coding: utf-8 -*-
"""
Tests for handheld models configuration and GitHub release resolution.
"""

from __future__ import annotations

import unittest
from unittest import mock

import flash_banana as fb


class TestModelConfiguration(unittest.TestCase):
    def test_retroid_pocket_classic_model_registered(self):
        self.assertIn("Retroid Pocket Classic", fb.MODELS)
        self.assertEqual(
            fb.MODELS["Retroid Pocket Classic"],
            "banana-retroid-pocket-classic.img.gz",
        )

    def test_model_repos_mapping(self):
        self.assertEqual(
            fb.MODEL_REPOS["Retroid Pocket Classic"],
            "jesussaves-dev/Banana-Pocket",
        )
        self.assertEqual(
            fb.MODEL_REPOS["Anbernic RG DS Plus"],
            "JorreFog/BananaLinux",
        )
        self.assertEqual(
            fb.MODEL_REPOS["Anbernic RG DS"],
            "JorreFog/BananaLinux",
        )

    def test_repo_for_model(self):
        self.assertEqual(
            fb.repo_for_model("Retroid Pocket Classic"),
            "jesussaves-dev/Banana-Pocket",
        )
        self.assertEqual(
            fb.repo_for_model("Anbernic RG DS Plus"),
            "JorreFog/BananaLinux",
        )
        self.assertEqual(
            fb.repo_for_model("Nonexistent Model"),
            fb.DEFAULT_REPO,
        )

    def test_repo_for_asset(self):
        self.assertEqual(
            fb.repo_for_asset("banana-retroid-pocket-classic.img.gz"),
            "jesussaves-dev/Banana-Pocket",
        )
        self.assertEqual(
            fb.repo_for_asset("banana-rgds-plus.img.gz"),
            "JorreFog/BananaLinux",
        )
        self.assertEqual(
            fb.repo_for_asset("unknown-asset.img.gz"),
            fb.DEFAULT_REPO,
        )

    def test_model_tokens(self):
        tokens = fb._model_tokens("banana-retroid-pocket-classic.img.gz")
        self.assertEqual(tokens, ["retroid", "pocket", "classic"])

        tokens_plus = fb._model_tokens("banana-rgds-plus.img.gz")
        self.assertEqual(tokens_plus, ["rgds", "plus"])


class TestReleaseResolution(unittest.TestCase):
    @mock.patch.object(fb, "_fetch_json")
    def test_resolve_banana_pocket_release_with_digest(self, mock_fetch):
        # First call is /releases/latest which returns 404 for pre-release-only repos
        # Second call is /releases?per_page=30 which returns the release list
        fake_releases = [
            {
                "tag_name": "v0.7.0",
                "draft": False,
                "prerelease": True,
                "assets": [
                    {
                        "name": "banana-retroid-pocket-classic-0.7.0.update.tar",
                        "size": 237936640,
                        "browser_download_url": "https://example.com/update.tar",
                        "digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
                    },
                    {
                        "name": "banana-retroid-pocket-classic.img.gz",
                        "size": 196413256,
                        "browser_download_url": "https://example.com/banana-retroid-pocket-classic.img.gz",
                        "digest": "sha256:93ec8ec1486776eb1dac079633bdb3e81870f742855f1789bd1e8192f0ea1fff",
                    },
                ],
            }
        ]

        def side_effect(url):
            if "latest" in url:
                raise fb.FlasherError("404 Not Found")
            return fake_releases

        mock_fetch.side_effect = side_effect

        resolved = fb.resolve_release_asset("banana-retroid-pocket-classic.img.gz")
        self.assertEqual(resolved["name"], "banana-retroid-pocket-classic.img.gz")
        self.assertEqual(resolved["tag"], "v0.7.0")
        self.assertEqual(resolved["size"], 196413256)
        self.assertEqual(resolved["repo"], "jesussaves-dev/Banana-Pocket")
        self.assertEqual(
            resolved["checksum"],
            "93ec8ec1486776eb1dac079633bdb3e81870f742855f1789bd1e8192f0ea1fff",
        )

    @mock.patch.object(fb, "_fetch_checksum")
    @mock.patch.object(fb, "_fetch_json")
    def test_resolve_bananalinux_release_with_sums_file(self, mock_fetch, mock_sums):
        fake_releases = [
            {
                "tag_name": "v0.5.0",
                "draft": False,
                "prerelease": True,
                "assets": [
                    {
                        "name": "banana-rgds-plus.img.gz",
                        "size": 158607263,
                        "browser_download_url": "https://example.com/banana-rgds-plus.img.gz",
                    },
                    {
                        "name": "SHA256SUMS-rgds-plus.txt",
                        "size": 100,
                        "browser_download_url": "https://example.com/SHA256SUMS-rgds-plus.txt",
                    },
                ],
            }
        ]

        def side_effect(url):
            if "latest" in url:
                raise fb.FlasherError("404 Not Found")
            return fake_releases

        mock_fetch.side_effect = side_effect
        mock_sums.return_value = "900177637ebcd466acf9c72ede0e5cbc2123fa3a4b8eb86cbe8c9bc33f097422"

        resolved = fb.resolve_release_asset("banana-rgds-plus.img.gz")
        self.assertEqual(resolved["name"], "banana-rgds-plus.img.gz")
        self.assertEqual(resolved["tag"], "v0.5.0")
        self.assertEqual(resolved["repo"], "JorreFog/BananaLinux")
        self.assertEqual(
            resolved["checksum"],
            "900177637ebcd466acf9c72ede0e5cbc2123fa3a4b8eb86cbe8c9bc33f097422",
        )

    @mock.patch.object(fb, "_fetch_json")
    def test_resolve_missing_asset_raises_flasher_error(self, mock_fetch):
        mock_fetch.return_value = [
            {"tag_name": "v1.0", "draft": False, "assets": [{"name": "other-asset.zip"}]}
        ]
        with self.assertRaises(fb.FlasherError) as ctx:
            fb.resolve_release_asset("missing-asset.img.gz")
        self.assertIn("missing-asset.img.gz", str(ctx.exception))
        self.assertIn("v1.0", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
