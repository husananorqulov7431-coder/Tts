import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp

from processor import ProcessingError, _atempo_chain, _clean_caption, _download_video


class ProcessorHelpersTest(unittest.TestCase):
    def test_caption_cleanup_removes_markup_and_fixes_line_breaks(self) -> None:
        self.assertEqual(_clean_caption(r"<i>Salom</i>\\N dunyo"), "Salom dunyo")

    def test_atempo_chain_handles_normal_speed(self) -> None:
        self.assertEqual(_atempo_chain(1.5), "atempo=1.50000")

    def test_atempo_chain_splits_large_factors(self) -> None:
        self.assertEqual(_atempo_chain(4.5), "atempo=2.0,atempo=2.0,atempo=1.12500")

    def test_youtube_bot_challenge_retries_with_tv_client_without_cookies(self) -> None:
        calls = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options
                calls.append(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download=True):
                if len(calls) == 1:
                    raise yt_dlp.utils.DownloadError(
                        "Sign in to confirm you're not a bot"
                    )
                output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
                output.write_bytes(b"test-video")
                return {"id": "test", "ext": "mp4"}

            def prepare_filename(self, _info):
                return Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))

        result_exists = False
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"YOUTUBE_COOKIES_FILE": ""}):
                with patch("processor.yt_dlp.YoutubeDL", FakeYoutubeDL):
                    result = _download_video("https://youtu.be/example", Path(directory))
                    result_exists = result.exists()

        self.assertTrue(result_exists)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("cookiefile", calls[0])
        self.assertEqual(
            calls[1]["extractor_args"]["youtube"]["player_client"], ["tv"]
        )

    def test_all_youtube_profiles_fail_with_clear_error(self) -> None:
        calls = []

        class AlwaysFailYoutubeDL:
            def __init__(self, options):
                calls.append(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download=True):
                raise yt_dlp.utils.DownloadError(
                    "Sign in to confirm you're not a bot"
                )

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"YOUTUBE_COOKIES_FILE": ""}):
                with patch("processor.yt_dlp.YoutubeDL", AlwaysFailYoutubeDL):
                    with self.assertRaisesRegex(ProcessingError, "bot-tekshiruv"):
                        _download_video("https://youtu.be/example", Path(directory))

        self.assertEqual(len(calls), 7)
        self.assertEqual(
            calls[1]["extractor_args"]["youtube"]["player_client"], ["tv"]
        )
        self.assertEqual(
            calls[2]["extractor_args"]["youtube"]["player_client"], ["android_vr"]
        )
        self.assertEqual(
            calls[3]["extractor_args"]["youtube"]["player_client"], ["web_safari"]
        )
        self.assertEqual(
            calls[4]["extractor_args"]["youtube"]["player_client"], ["tv_simply"]
        )
        self.assertEqual(
            calls[5]["extractor_args"]["youtube"]["player_client"], ["web_embedded"]
        )
        self.assertEqual(
            calls[6]["extractor_args"]["youtube"]["player_client"], ["tv", "web_safari"]
        )


if __name__ == "__main__":
    unittest.main()
