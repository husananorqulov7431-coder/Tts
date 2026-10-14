from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable

import edge_tts
import srt
import yt_dlp
from deep_translator import GoogleTranslator

log = logging.getLogger(__name__)
Progress = Callable[[str], Awaitable[None]]

VOICE = os.getenv("DEFAULT_VOICE", "uz-UZ-SardorNeural")
TTS_RATE = os.getenv("TTS_RATE", "+0%")
TTS_VOLUME = os.getenv("TTS_VOLUME", "+0%")
ORIGINAL_AUDIO_VOLUME = min(1.0, max(0.0, float(os.getenv("ORIGINAL_AUDIO_VOLUME", "0.10"))))
TRANSLATE_BY_DEFAULT = os.getenv("TRANSLATE_TO_UZBEK", "true").lower() in {"1", "true", "yes", "on"}
MAX_VIDEO_MINUTES = int(os.getenv("MAX_VIDEO_DURATION_MINUTES", "360"))
MAX_OUTPUT_MB = int(os.getenv("MAX_OUTPUT_MB", "1900"))
WORK_ROOT = Path(os.getenv("WORK_DIR", "work/jobs"))
WORK_ROOT.mkdir(parents=True, exist_ok=True)


class ProcessingError(RuntimeError):
    """User-safe video-processing error."""


def run_checked(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcessingError(f"Buyruq vaqt limitidan oshdi: {Path(args[0]).name}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "noma’lum xato").strip()
        log.error("Command failed (%s): %s", args[0], detail[-2500:])
        raise ProcessingError(f"{Path(args[0]).name} bajarilmadi. Loglarni tekshirish kerak.") from exc


def probe_duration(media_path: Path) -> float:
    result = run_checked([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(media_path),
    ], timeout=60)
    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError as exc:
        raise ProcessingError("Video davomiyligini aniqlab bo‘lmadi.") from exc


def has_audio(media_path: Path) -> bool:
    result = run_checked([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(media_path),
    ], timeout=60)
    return bool(result.stdout.strip())


def _download_video(url: str, work_dir: Path) -> Path:
    """Download a YouTube video, retrying with cookie-free player clients if challenged."""
    max_seconds = MAX_VIDEO_MINUTES * 60 if MAX_VIDEO_MINUTES > 0 else 0

    def duration_filter(info: dict, *, incomplete: bool = False) -> str | None:
        duration = info.get("duration")
        if max_seconds and duration and duration > max_seconds:
            return f"Video {MAX_VIDEO_MINUTES} daqiqalik konfiguratsiya limitidan uzun."
        if info.get("is_live"):
            return "Jonli efirlarni qayta ishlash qo‘llanmaydi."
        return None

    # First keep yt-dlp's default behavior, then try clients that can work
    # without browser cookies/PO tokens. YouTube can change these rules.
    client_profiles: list[tuple[str, list[str] | None]] = [
        ("default", None),
        ("tv", ["tv"]),
        ("android_vr", ["android_vr"]),
        ("web_safari", ["web_safari"]),
        ("tv_simply", ["tv_simply"]),
        ("web_embedded", ["web_embedded"]),
        ("tv+web_safari", ["tv", "web_safari"]),
    ]
    options: dict = {
        "format": "bestvideo*+bestaudio/best",
        "outtmpl": str(work_dir / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "overwrites": True,
        "match_filter": duration_filter,
    }
    # Optional only: ordinary public videos are attempted without requiring cookies.
    cookie_path = os.getenv("YOUTUBE_COOKIES_FILE")
    if cookie_path and Path(cookie_path).is_file() and Path(cookie_path).stat().st_size > 0:
        options["cookiefile"] = cookie_path

    errors: list[str] = []
    last_error: Exception | None = None
    for profile_name, clients in client_profiles:
        # A failed format attempt may leave partial files; do not let a later
        # attempt accidentally return one of those incomplete files.
        for old_file in work_dir.glob("source.*"):
            if old_file.is_file():
                try:
                    old_file.unlink()
                except OSError:
                    pass

        attempt_options = dict(options)
        if clients is not None:
            attempt_options["extractor_args"] = {
                "youtube": {"player_client": clients}
            }
        log.info("Trying YouTube player profile: %s", profile_name)

        try:
            with yt_dlp.YoutubeDL(attempt_options) as downloader:
                info = downloader.extract_info(url, download=True)
                prepared = Path(downloader.prepare_filename(info))
                candidates = [
                    p for p in work_dir.glob("source.*")
                    if p.is_file()
                    and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
                    and not p.name.endswith((".part", ".ytdl"))
                ]
                if prepared.exists() and prepared.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
                    return prepared
                if candidates:
                    return max(candidates, key=lambda p: p.stat().st_size)
                errors.append(f"{profile_name}: downloader returned no complete video file")
        except Exception as exc:
            message = str(exc)
            if "limitidan uzun" in message or "Jonli efir" in message:
                raise ProcessingError(message) from exc
            last_error = exc
            errors.append(f"{profile_name}: {message[:350]}")
            log.warning("YouTube profile %s failed: %s", profile_name, message[:350])

    joined_errors = "\n".join(errors)
    if ("Sign in to confirm" in joined_errors or "not a bot" in joined_errors.lower()
            or "confirm you’re not a bot" in joined_errors.lower()):
        raise ProcessingError(
            "YouTube anonim yuklashni bot-tekshiruv sababli rad etdi. Kod avtomatik ravishda "
            "default, tv, android_vr va web_safari player profillarini sinab ko‘rdi, ammo "
            "bu server/IP uchun ularning hech biri ishlamadi. Bu video kodi emas, YouTube "
            "tomonidagi cheklov bo‘lishi mumkin; birozdan keyin qayta urinib ko‘ring."
        ) from last_error

    log.error("All YouTube download profiles failed: %s", joined_errors[-1800:])
    raise ProcessingError(
        "YouTube videosini yuklab bo‘lmadi. Havola, video ochiqligi yoki YouTube "
        "serverining vaqtinchalik cheklovini tekshiring."
    ) from last_error


def _clean_caption(text: str) -> str:
    # SRT line breaks may be represented by either \\N or \\n (one/two slashes).
    text = re.sub(r"\\{1,2}[Nn]", " ", text).replace("\n", " ")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{[^}]*\}", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _translate_entries(entries: list[srt.Subtitle]) -> list[srt.Subtitle]:
    """Translate caption batches while preserving SRT timings and indexes."""
    translator = GoogleTranslator(source="auto", target="uz")
    original = [entry.content for entry in entries]
    batch_size = 8
    try:
        for start in range(0, len(entries), batch_size):
            chunk = entries[start:start + batch_size]
            texts = [_clean_caption(item.content) for item in chunk]
            translated = translator.translate_batch(texts)
            if not isinstance(translated, list) or len(translated) != len(chunk):
                raise RuntimeError("Tarjimon to‘liq natija qaytarmadi")
            for entry, result in zip(chunk, translated):
                if result and result.strip():
                    entry.content = result.strip()
            time.sleep(0.15)
    except Exception as exc:
        for entry, text in zip(entries, original):
            entry.content = text
        log.warning("SRT translation failed; using the original captions: %s", exc)
        raise ProcessingError(
            "SRT’ni o‘zbekchaga tarjima qilib bo‘lmadi. /translate off buyrug‘i bilan "
            "tarjimani o‘chirib, o‘zbekcha SRT faylini yuboring yoki qayta urinib ko‘ring."
        ) from exc
    return entries


def _atempo_chain(factor: float) -> str:
    """Build valid FFmpeg atempo filters, each factor in [0.5, 2.0]."""
    factor = max(1.0, factor)
    filters: list[str] = []
    while factor > 2.0:
        filters.append("atempo=2.0")
        factor /= 2.0
    filters.append(f"atempo={factor:.5f}")
    return ",".join(filters)


def _make_speech_segment(mp3_path: Path, wav_path: Path, slot_seconds: float, speech_seconds: float) -> None:
    if slot_seconds <= 0.02:
        raise ProcessingError("Subtitr oralig‘i juda qisqa; SRT vaqtlarini tekshiring.")
    speech_window = min(slot_seconds, max(0.08, speech_seconds))
    speed = max(1.0, speech_seconds / speech_window)
    filters = _atempo_chain(speed) if speed > 1.01 else "anull"
    # Pad after speech, so every rendered segment occupies its exact timeline slot.
    audio_filter = (
        f"{filters},apad,atrim=duration={slot_seconds:.4f},"
        "asetpts=PTS-STARTPTS"
    )
    run_checked([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mp3_path),
        "-vn", "-af", audio_filter, "-ar", "48000", "-ac", "2",
        "-c:a", "pcm_s16le", str(wav_path),
    ], timeout=180)


async def _synthesize(text: str, voice: str, output_path: Path, semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                communicate = edge_tts.Communicate(text, voice=voice, rate=TTS_RATE, volume=TTS_VOLUME)
                await communicate.save(str(output_path))
                if output_path.exists() and output_path.stat().st_size > 500:
                    return
                raise RuntimeError("Edge TTS bo‘sh audio yaratdi")
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(1.0 + attempt)
        raise ProcessingError(f"Edge TTS ovoz yaratolmadi: {last_error}") from last_error


async def _build_voice_track(
    srt_path: Path,
    voice_path: Path,
    work_dir: Path,
    voice: str,
    translate: bool,
    progress: Progress,
) -> None:
    try:
        raw_text = srt_path.read_text(encoding="utf-8-sig", errors="replace")
        entries = list(srt.parse(raw_text))
    except Exception as exc:
        raise ProcessingError(".srt faylini o‘qib bo‘lmadi.") from exc

    entries = [
        entry for entry in entries
        if _clean_caption(entry.content) and entry.end.total_seconds() > entry.start.total_seconds()
    ]
    if not entries:
        raise ProcessingError("SRT ichida ovozlashtiriladigan subtitrlar topilmadi.")
    entries.sort(key=lambda item: item.start)

    if translate:
        await progress(f"Subtitrlar tarjima qilinmoqda ({len(entries)} ta qator)…")
        original_entries = copy.deepcopy(entries)
        try:
            entries = await asyncio.to_thread(_translate_entries, entries)
        except ProcessingError as exc:
            # Continue with original captions rather than losing the whole task.
            entries = original_entries
            await progress(
                "Tarjima xizmati ishlamadi; asl SRT matni ovozlashtiriladi. "
                f"Sabab: {str(exc)[:120]}"
            )
        translated_srt = work_dir / "captions_uz.srt"
        translated_srt.write_text(srt.compose(entries), encoding="utf-8")

    await progress(f"Edge TTS ovozi yaratilmoqda ({len(entries)} ta subtitr)…")
    clips_dir = work_dir / "tts"
    clips_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(3)
    clean_texts = [_clean_caption(item.content) for item in entries]
    mp3_paths = [clips_dir / f"speech_{idx:05d}.mp3" for idx in range(len(entries))]
    await asyncio.gather(*[
        _synthesize(text, voice, path, semaphore)
        for text, path in zip(clean_texts, mp3_paths)
    ])

    await progress("Ovozlar subtitr vaqtlariga moslanmoqda…")
    wav_paths: list[Path] = []
    starts = [max(0.0, item.start.total_seconds()) for item in entries]
    ends = [max(starts[i] + 0.05, item.end.total_seconds()) for i, item in enumerate(entries)]
    initial_silence = starts[0]
    for idx, (entry, mp3_path) in enumerate(zip(entries, mp3_paths)):
        tts_duration = probe_duration(mp3_path)
        if idx + 1 < len(entries):
            slot = max(0.05, starts[idx + 1] - starts[idx])
            speech_window = min(max(0.08, ends[idx] - starts[idx]), slot)
        else:
            slot = max(0.08, ends[idx] - starts[idx], tts_duration)
            speech_window = slot
        wav_path = clips_dir / f"segment_{idx:05d}.wav"
        _make_speech_segment(mp3_path, wav_path, slot, speech_window if tts_duration <= speech_window else tts_duration)
        wav_paths.append(wav_path)

    concat_list = clips_dir / "concat.txt"
    with concat_list.open("w", encoding="utf-8") as handle:
        if initial_silence > 0.01:
            silence_path = clips_dir / "initial_silence.wav"
            run_checked([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", f"{initial_silence:.4f}", "-c:a", "pcm_s16le", str(silence_path),
            ], timeout=60)
            handle.write(f"file '{silence_path.as_posix()}'\n")
        for wav_path in wav_paths:
            handle.write(f"file '{wav_path.as_posix()}'\n")

    run_checked([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(voice_path),
    ], timeout=900)


def _mix_audio(video_path: Path, voice_path: Path, output_path: Path, duration: float) -> None:
    if duration <= 0:
        raise ProcessingError("Video davomiyligi noto‘g‘ri.")
    duration_arg = f"{duration:.3f}"
    if has_audio(video_path):
        filter_graph = (
            f"[0:a:0]volume={ORIGINAL_AUDIO_VOLUME:.3f},apad,atrim=duration={duration_arg}[bg];"
            f"[1:a:0]volume=1.0,apad,atrim=duration={duration_arg}[voice];"
            "[bg][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.95[aout]"
        )
    else:
        filter_graph = (
            f"[1:a:0]volume=1.0,apad,atrim=duration={duration_arg},"
            "alimiter=limit=0.95[aout]"
        )

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_path), "-i", str(voice_path),
        "-filter_complex", filter_graph,
        "-map", "0:v:0", "-map", "[aout]", "-map_metadata", "0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-t", duration_arg,
        "-max_muxing_queue_size", "1024", str(output_path),
    ]
    run_checked(command, timeout=1800)


async def process_video(
    url: str,
    srt_path: Path,
    voice: str,
    translate: bool,
    progress: Progress,
) -> tuple[Path, Path]:
    """Return (job directory, final video path); caller removes directory after sending."""
    import uuid

    job_dir = WORK_ROOT / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=False)
    try:
        local_srt = job_dir / "captions.srt"
        shutil.copy2(srt_path, local_srt)

        await progress("YouTube videosi eng yuqori mavjud oqimda yuklanmoqda…")
        video_path = await asyncio.to_thread(_download_video, url, job_dir)
        size_mb = video_path.stat().st_size / (1024 * 1024)
        await progress(f"Video yuklandi ({size_mb:.0f} MB). Davomiylik va audio tekshirilmoqda…")

        duration = await asyncio.to_thread(probe_duration, video_path)
        if MAX_VIDEO_MINUTES > 0 and duration > MAX_VIDEO_MINUTES * 60:
            raise ProcessingError(f"Video {MAX_VIDEO_MINUTES} daqiqalik sozlama limitidan uzun.")

        voice_path = job_dir / "uzbek_voice.wav"
        await _build_voice_track(local_srt, voice_path, job_dir, voice, translate, progress)

        await progress("Asl audio 10% darajaga tushirilib, o‘zbekcha ovoz qo‘shilmoqda…")
        output_path = job_dir / "uzbek_dub.mp4"
        await asyncio.to_thread(_mix_audio, video_path, voice_path, output_path, duration)

        if not output_path.exists() or output_path.stat().st_size < 100_000:
            raise ProcessingError("Yakuniy video yaratilmadi yoki fayl juda kichik.")
        output_mb = output_path.stat().st_size / (1024 * 1024)
        if output_mb > MAX_OUTPUT_MB:
            raise ProcessingError(
                f"Yakuniy fayl {output_mb:.0f} MB. Telegram uchun sozlangan limit {MAX_OUTPUT_MB} MB. "
                "MAX_OUTPUT_MB qiymatini tekshiring yoki videoni qismlarga ajrating."
            )
        return job_dir, output_path
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
