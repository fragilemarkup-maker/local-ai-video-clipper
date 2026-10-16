"""Local-first YouTube topic clipper with optional OpenAI topic matching."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import traceback
from pathlib import Path
from typing import Any

import streamlit as st


st.set_page_config(page_title="Local AI Video Clipper", page_icon="✂️", layout="centered")

MAX_CLIPS = 5
MAX_CLIP_SECONDS = 60
OUTPUT_WIDTH = 720
OUTPUT_HEIGHT = 1280

LOCAL_DOWNLOAD_GUIDANCE = (
    "YouTube returned HTTP 403 from the app host. On Streamlit Community Cloud this commonly means "
    "the shared datacenter IP is being blocked; changing request headers cannot reliably fix that. "
    "If you are using Community Cloud, run the app on your own computer instead: install FFmpeg, open a terminal in the app folder, "
    "run `python -m pip install -r requirements.txt`, then run `streamlit run app.py`."
)


class VideoDownloadError(RuntimeError):
    """A download failure that has already been displayed with its diagnostic trace."""


def clean_filename(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE)
    return value.strip("._")[:80] or "clip"


def require_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg was not found. Install FFmpeg and ensure `ffmpeg` is on PATH.")
    return executable


def validate_youtube_url(url: str) -> None:
    if not re.match(r"^https?://", url.strip(), re.IGNORECASE):
        raise ValueError("Enter a full YouTube URL beginning with https://")
    host = re.sub(r"^https?://", "", url.strip(), flags=re.IGNORECASE).split("/", 1)[0].split(":", 1)[0].lower()
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"}:
        raise ValueError("That URL does not appear to be a YouTube link.")


def server_setting(name: str, default: str = "") -> str:
    """Read a server-side setting from environment variables or Streamlit secrets."""
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def download_video(
    url: str,
    folder: Path,
    progress: Any,
    max_seconds: int | None = None,
) -> tuple[Path, str]:
    traces: list[str] = []
    try:
        import yt_dlp

        folder.mkdir(parents=True, exist_ok=True)
        output_template = str(folder / "source.%(ext)s")
        clients = ["tv", "tv_embedded", "ios", "android", "web_safari", "web"]

        # PO tokens are video/session-specific and should come from a maintained provider.
        # Configure these as Streamlit secrets or environment variables; never hard-code them.
        token = server_setting("YTDLP_PO_TOKEN")
        token_client = server_setting("YTDLP_PO_TOKEN_CLIENT", "mweb") or "mweb"
        if token and token_client not in clients:
            clients.insert(0, token_client)

        cookies_file = server_setting("YTDLP_COOKIES_FILE")
        cookies_content = server_setting("YTDLP_COOKIES")
        if cookies_content:
            # Useful on hosted services where the cookie file itself cannot be mounted.
            cookies_path = folder / "youtube_cookies.txt"
            cookies_path.write_text(cookies_content, encoding="utf-8")
            cookies_file = str(cookies_path)
        if cookies_file and not Path(cookies_file).is_file():
            raise FileNotFoundError("YTDLP_COOKIES_FILE is set, but that cookie file does not exist on this server.")

        common_options: dict[str, Any] = {
            "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.youtube.com/",
                "Origin": "https://www.youtube.com",
            },
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            "file_access_retries": 2,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 1,
            "progress_hooks": [lambda data: progress(data)],
        }
        if cookies_file:
            common_options["cookiefile"] = cookies_file
        if max_seconds is not None:
            common_options["download_ranges"] = lambda _info, _ydl: [{"start_time": 0, "end_time": max_seconds}]
            common_options["force_keyframes_at_cuts"] = True

        for client in clients:
            # Remove partial output from a previous failed client attempt.
            for old_file in folder.glob("source.*"):
                if old_file.is_file():
                    old_file.unlink()
            options = {
                **common_options,
                "extractor_args": {"youtube": {"player_client": [client]}},
            }
            if token and client == token_client:
                options["extractor_args"]["youtube"]["po_token"] = [f"{client}.gvs+{token}"]
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url.strip(), download=True)
                candidates = [p for p in folder.glob("source.*") if p.suffix.lower() not in {".part", ".ytdl"}]
                if candidates:
                    return candidates[0], str(info.get("title") or "YouTube video")
                raise RuntimeError(f"yt-dlp client '{client}' completed without producing a video file.")
            except Exception:
                traces.append(f"Client '{client}' failed:\n{traceback.format_exc()}")

        error_text = "\n\n".join(traces)
        is_403 = any("403" in item or "forbidden" in item.lower() for item in traces)
        summary = LOCAL_DOWNLOAD_GUIDANCE if is_403 else "All configured YouTube download clients failed. Check the trace below for the exact yt-dlp errors."
        st.error(f"{summary}\n\n```text\n{error_text or traceback.format_exc()}\n```")
        if is_403:
            st.info("If you administer this deployment, a supported PO Token provider may help for formats that require tokens. Configure YTDLP_PO_TOKEN and YTDLP_PO_TOKEN_CLIENT secrets. If authentication is actually needed, use YTDLP_COOKIES with a Netscape-format cookie export or YTDLP_COOKIES_FILE with a server-side file. Treat cookies like passwords. yt-dlp's YouTube OAuth login is no longer supported.")
        raise VideoDownloadError(summary)
    except VideoDownloadError:
        raise
    except Exception as exc:
        # Keep the exact stack visible in Streamlit, including import/configuration errors.
        if not traces:
            st.error(f"Video download failed: {exc}\n\n```text\n{traceback.format_exc()}\n```")
        raise VideoDownloadError(f"Video download failed: {exc}") from exc


def transcribe(video_path: Path, model_name: str, device: str, progress: Any) -> list[dict[str, Any]]:
    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError("openai-whisper is not installed. Install requirements.txt first.") from exc
    progress(0.1, f"Loading Whisper {model_name} model ({device})…")
    try:
        model = whisper.load_model(model_name, device=device)
        result = model.transcribe(str(video_path), word_timestamps=True, verbose=False, fp16=(device == "cuda"))
    except Exception as exc:
        raise RuntimeError(f"Transcription failed: {exc}") from exc
    segments = []
    for segment in result.get("segments", []):
        words = []
        for word in segment.get("words") or []:
            if word.get("start") is not None and word.get("end") is not None:
                words.append({"word": str(word.get("word", "")).strip(), "start": float(word["start"]), "end": float(word["end"])})
        segments.append({
            "start": float(segment["start"]),
            "end": float(segment["end"]),
            "text": str(segment.get("text", "")).strip(),
            "words": words,
        })
    return segments


def transcript_for_prompt(segments: list[dict[str, Any]]) -> str:
    lines = []
    for seg in segments:
        words = seg["words"]
        if words:
            lines.extend(f"[{w['start']:.2f}-{w['end']:.2f}] {w['word']}" for w in words)
        else:
            lines.append(f"[{seg['start']:.2f}-{seg['end']:.2f}] {seg['text']}")
    return "\n".join(lines)


def local_topic_matches(segments: list[dict[str, Any]], topic: str, max_clips: int, max_seconds: int) -> list[dict[str, Any]]:
    """Find topic-related utterances using normalized word overlap and fuzzy token scores."""
    from difflib import SequenceMatcher

    topic_terms = [w for w in re.findall(r"[\w']+", topic.lower()) if len(w) > 2]
    if not topic_terms:
        topic_terms = re.findall(r"[\w']+", topic.lower())
    words: list[dict[str, Any]] = []
    for seg in segments:
        if seg["words"]:
            words.extend(seg["words"])
        elif seg["text"]:
            toks = re.findall(r"\S+", seg["text"])
            span = max(0.05, seg["end"] - seg["start"])
            for i, token in enumerate(toks):
                words.append({"word": token, "start": seg["start"] + span * i / len(toks), "end": seg["start"] + span * (i + 1) / len(toks)})
    if not words or not topic_terms:
        return []

    # Search short overlapping windows so a broad topic match does not return most of the video.
    search_window = min(15, max_seconds)
    windows: list[dict[str, Any]] = []
    left = 0
    while left < len(words):
        right = left
        while right < len(words) and words[right]["start"] - words[left]["start"] < search_window:
            right += 1
        chunk = words[left:right]
        text = " ".join(w["word"] for w in chunk)
        normalized = [w.lower().strip(".,!?;:()[]{}\"'—-") for w in re.findall(r"[\w'—-]+", text)]
        matched = 0
        for term in topic_terms:
            if any(t == term or (len(term) >= 5 and SequenceMatcher(None, t, term).ratio() >= 0.86) for t in normalized):
                matched += 1
        score = matched / len(topic_terms)
        if score >= (0.5 if len(topic_terms) > 1 else 1.0):
            start = max(0.0, float(chunk[0]["start"]) - 0.35)
            end = min(float(chunk[-1]["end"]) + 0.35, start + max_seconds)
            windows.append({"start": start, "end": end, "reason": f"Local transcript keyword match ({matched}/{len(topic_terms)} topic terms)", "score": score})
        # Move forward by ~half the window to allow distinct moments without duplicates.
        left += max(1, len(chunk) // 2)

    windows.sort(key=lambda x: (-x["score"], x["start"]))
    chosen: list[dict[str, Any]] = []
    for item in windows:
        if all(item["end"] <= old["start"] or item["start"] >= old["end"] for old in chosen):
            chosen.append(item)
            if len(chosen) >= max_clips:
                break
    return sorted(chosen, key=lambda x: x["start"])


def openai_topic_matches(segments: list[dict[str, Any]], topic: str, api_key: str, max_clips: int, max_seconds: int) -> list[dict[str, Any]]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("The openai package is missing. Install requirements.txt.") from exc
    if not api_key.strip():
        raise ValueError("Enter an OpenAI API key to enable OpenAI matching.")
    client = OpenAI(api_key=api_key.strip())
    transcript = transcript_for_prompt(segments)
    # Keep context bounded; a single call handles typical videos and avoids running off the request budget.
    transcript = transcript[:80000]
    prompt = f"""Find up to {max_clips} distinct, self-contained video moments in this timestamped transcript that are relevant to the topic: {topic!r}.
Return only a JSON object with a 'clips' array. Each item must have numeric start and end seconds and a short reason. Use only timestamps covered by the transcript, make each clip at most {max_seconds} seconds, include enough context to make the thought understandable, and do not invent matches. Return an empty array when there are no relevant moments.

Timestamped transcript:
{transcript}"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You select precise timestamp ranges from transcripts. Follow the requested JSON schema."}, {"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        data = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        raise RuntimeError(f"OpenAI topic analysis failed: {exc}") from exc
    duration = max((seg["end"] for seg in segments), default=0)
    results = []
    for item in data.get("clips", []):
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= start < end <= duration + 1 and end - start <= max_seconds:
            results.append({"start": start, "end": end, "reason": str(item.get("reason", "Topic match")), "score": 1.0})
    results.sort(key=lambda x: x["start"])
    # Remove overlapping duplicate ranges and enforce the requested limit.
    distinct = []
    for item in results:
        if all(item["end"] <= old["start"] or item["start"] >= old["end"] for old in distinct):
            distinct.append(item)
        if len(distinct) == max_clips:
            break
    return distinct


def ass_time(seconds: float) -> str:
    total = max(0, int(round(seconds * 100)))
    h, rem = divmod(total, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def subtitle_events(segments: list[dict[str, Any]], start: float, end: float) -> list[str]:
    tokens = []
    for seg in segments:
        for word in seg["words"]:
            if word["end"] >= start and word["start"] <= end:
                tokens.append({**word, "word": word["word"].strip()})
    if not tokens:
        for seg in segments:
            if seg["end"] >= start and seg["start"] <= end and seg["text"]:
                pieces = re.findall(r"\S+", seg["text"])
                if pieces:
                    span = seg["end"] - seg["start"]
                    for i, piece in enumerate(pieces):
                        tokens.append({"word": piece, "start": seg["start"] + span * i / len(pieces), "end": seg["start"] + span * (i + 1) / len(pieces)})

    events: list[str] = []
    # Short karaoke lines keep captions readable on phone screens.
    for idx in range(0, len(tokens), 5):
        group = tokens[idx:idx + 5]
        if not group:
            continue
        local_start = max(0.0, group[0]["start"] - start)
        local_end = min(end - start, max(group[-1]["end"] - start, local_start + 0.35))
        karaoke = []
        for word in group:
            centiseconds = max(1, round((word["end"] - word["start"]) * 100))
            escaped = word["word"].replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
            karaoke.append(f"{{\\kf{centiseconds}}}{escaped}")
        events.append(f"Dialogue: 0,{ass_time(local_start)},{ass_time(local_end)},Caption,,0,0,0,,{' '.join(karaoke)}")
    return events


def write_ass(path: Path, events: list[str]) -> None:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,55,&H00FFFFFF,&H0000D7FF,&H00101010,&H99000000,-1,0,0,0,100,100,0,0,1,3,1,2,48,48,225,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def ffmpeg_filter_path(path: Path) -> str:
    # FFmpeg filter strings use their own escaping rules; normalize separators for Windows too.
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def make_clip(ffmpeg: str, video: Path, segments: list[dict[str, Any]], match: dict[str, Any], index: int, folder: Path) -> Path:
    start, end = float(match["start"]), float(match["end"])
    ass_path = folder / f"captions_{index:02d}.ass"
    output = folder / f"clip_{index:02d}.mp4"
    events = subtitle_events(segments, start, end)
    if not events or not any(r"\kf" in event for event in events):
        raise RuntimeError("No timed caption events were available for this clip; Whisper must return speech timestamps.")
    write_ass(ass_path, events)
    vf = f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},setsar=1,subtitles='{ffmpeg_filter_path(ass_path)}'"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{end-start:.3f}", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-maxrate", "2500k", "-bufsize", "5000k", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20 * 60)
    if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        detail = (result.stderr or "FFmpeg did not create an output file.").strip()[-2500:]
        raise RuntimeError(f"Could not render clip {index}: {detail}")
    return output


def verify_vertical_caption_clip(ffmpeg: str, clip: Path, ass_path: Path) -> None:
    """Check the rendered file dimensions and that timed ASS captions were burned by FFmpeg."""
    if not ass_path.is_file():
        raise RuntimeError("Caption verification failed: the generated ASS subtitle file is missing.")
    subtitle_text = ass_path.read_text(encoding="utf-8")
    if "[Events]" not in subtitle_text or "Dialogue:" not in subtitle_text or r"\kf" not in subtitle_text:
        raise RuntimeError("Caption verification failed: no word-timed subtitle events were rendered.")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe was not found, so the rendered clip could not be verified.")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(clip)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not inspect the rendered clip: {(result.stderr or '').strip()}")
    try:
        stream = json.loads(result.stdout)["streams"][0]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not read video dimensions from the rendered clip.") from exc
    if (stream.get("width"), stream.get("height")) != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
        raise RuntimeError(f"Expected a {OUTPUT_WIDTH}×{OUTPUT_HEIGHT} vertical clip; got {stream.get('width')}×{stream.get('height')}.")


def run_diagnostic(youtube_url: str) -> tuple[bytes, list[str]]:
    """Run a small end-to-end downloader, Whisper, crop and caption smoke check."""
    validate_youtube_url(youtube_url)
    ffmpeg = require_ffmpeg()
    log = ["URL validation passed.", "FFmpeg is available."]
    with tempfile.TemporaryDirectory(prefix="clipper_diagnostic_") as temp:
        folder = Path(temp)
        try:
            video, title = download_video(youtube_url, folder, lambda _data: None, max_seconds=12)
        except VideoDownloadError:
            raise
        except Exception as exc:
            if "403" in str(exc) or "forbidden" in str(exc).lower():
                raise RuntimeError(f"Diagnostic download was blocked with HTTP 403 after all configured YouTube client fallbacks: {exc}") from exc
            raise
        log.append(f"YouTube snippet download passed ({title}).")
        segments = transcribe(video, "tiny", "cpu", lambda _progress, _message: None)
        word_count = sum(len(segment["words"]) for segment in segments)
        if word_count == 0:
            raise RuntimeError("Whisper completed but returned no word timestamps; the caption check cannot continue.")
        log.append(f"Whisper tiny transcription passed ({word_count} word timestamps).")
        duration = max((segment["end"] for segment in segments), default=0.0)
        end = min(12.0, duration)
        if end <= 0.5:
            raise RuntimeError("The downloaded snippet was too short to render a diagnostic clip.")
        match = {"start": 0.0, "end": end}
        clip = make_clip(ffmpeg, video, segments, match, 1, folder)
        verify_vertical_caption_clip(ffmpeg, clip, folder / "captions_01.ass")
        log.append("Vertical 720×1280 MP4 rendered with word-timed ASS captions burned in via FFmpeg.")
        return clip.read_bytes(), log


def run_workflow(url: str, topic: str, api_key: str, use_openai: bool, model: str, device: str, clip_count: int, clip_duration: int) -> None:
    progress_bar = st.progress(0)
    status = st.status("Starting…", expanded=True)
    messages = st.empty()

    def update_download(data: dict[str, Any]) -> None:
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            if total:
                progress_bar.progress(min(0.18, 0.02 + 0.16 * downloaded / total))
                messages.caption(f"Downloading: {downloaded / total:.0%}")

    try:
        validate_youtube_url(url)
        ffmpeg = require_ffmpeg()
        with tempfile.TemporaryDirectory(prefix="local_clipper_") as temp:
            folder = Path(temp)
            with status:
                status.update(label="Downloading video…", state="running")
                video, title = download_video(url, folder, update_download)
                progress_bar.progress(0.2)
                status.write(f"Downloaded: {title}")

                status.update(label="Transcribing speech…", state="running")
                device_name = device
                if device == "cuda":
                    try:
                        import torch
                        if not torch.cuda.is_available():
                            device_name = "cpu"
                    except Exception:
                        device_name = "cpu"
                segments = transcribe(video, model, device_name, lambda _, msg: status.write(msg))
                if not any(s["text"] for s in segments):
                    raise RuntimeError("Whisper did not detect speech in this video.")
                progress_bar.progress(0.62)
                status.write(f"Transcribed {len(segments)} speech segments.")

                status.update(label="Finding topic moments…", state="running")
                matches = openai_topic_matches(segments, topic, api_key, clip_count, clip_duration) if use_openai else local_topic_matches(segments, topic, clip_count, clip_duration)
                progress_bar.progress(0.72)
                if not matches:
                    raise RuntimeError("No matching moments found. Try a broader topic or enable OpenAI semantic matching.")
                status.write(f"Found {len(matches)} candidate clip(s).")

                clips = []
                for i, match in enumerate(matches, 1):
                    status.update(label=f"Rendering clip {i} of {len(matches)}…", state="running")
                    path = make_clip(ffmpeg, video, segments, match, i, folder)
                    clips.append({"bytes": path.read_bytes(), "start": match["start"], "end": match["end"], "reason": match["reason"], "name": f"{clean_filename(topic)}_{i:02d}.mp4"})
                    progress_bar.progress(0.72 + 0.28 * i / len(matches))
            st.session_state["clips"] = clips
            st.session_state["video_title"] = title
            status.update(label="Finished", state="complete", expanded=False)
    except Exception as exc:
        status.update(label="Could not complete the workflow", state="error")
        if not isinstance(exc, VideoDownloadError):
            st.error(str(exc))


st.title("✂️ Local AI Video Clipper")
st.caption("Find topic moments, crop them to vertical 9:16, and burn in word-timed subtitles.")

with st.form("clipper_form", clear_on_submit=False):
    youtube_url = st.text_input("YouTube URL", placeholder="https://www.youtube.com/watch?v=…")
    topic_query = st.text_input("Topic or search query", placeholder="e.g. how compound interest works")
    use_openai = st.toggle("Use OpenAI for semantic topic matching (optional; API usage may cost money)", value=False)
    api_key = st.text_input("OpenAI API key", type="password", disabled=not use_openai, help="Used only for transcript topic matching. Whisper and video rendering run locally.")
    col1, col2 = st.columns(2)
    with col1:
        whisper_model = st.selectbox("Whisper model", ["tiny", "base", "small", "medium"], index=0, help="Larger models improve accuracy but need more memory and time.")
        device = st.selectbox("Compute", ["auto", "cpu", "cuda"], index=0)
    with col2:
        clip_count = st.slider("Maximum clips", min_value=1, max_value=MAX_CLIPS, value=3)
        clip_duration = st.slider("Maximum clip length (seconds)", min_value=10, max_value=MAX_CLIP_SECONDS, value=40)
    submitted = st.form_submit_button("Find and create clips", type="primary", use_container_width=True)

if submitted:
    if not youtube_url.strip() or not topic_query.strip():
        st.error("Enter both a YouTube URL and a topic.")
    elif use_openai and not api_key.strip():
        st.error("Enter an OpenAI API key or turn off OpenAI matching to use local keyword matching.")
    else:
        if device == "auto":
            try:
                import torch
                chosen_device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                chosen_device = "cpu"
        else:
            chosen_device = device
        run_workflow(youtube_url, topic_query, api_key, use_openai, whisper_model, chosen_device, clip_count, clip_duration)

with st.expander("Run end-to-end diagnostic"):
    st.write("Downloads the first 12 seconds of a public YouTube video, transcribes it with Whisper tiny on CPU, and renders a vertical sample with word-timed captions. This uses the same downloader and FFmpeg rendering path as clip creation.")
    diagnostic_url = st.text_input("YouTube URL for diagnostic", key="diagnostic_youtube_url", placeholder="https://www.youtube.com/watch?v=…")
    if st.button("Run diagnostic", key="run_diagnostic", use_container_width=True):
        if not diagnostic_url.strip():
            st.error("Enter a YouTube URL to run the diagnostic.")
        else:
            with st.status("Running end-to-end diagnostic…", expanded=True) as diagnostic_status:
                try:
                    diagnostic_video, diagnostic_log = run_diagnostic(diagnostic_url)
                    for line in diagnostic_log:
                        diagnostic_status.write(f"✓ {line}")
                    diagnostic_status.update(label="Diagnostic passed", state="complete", expanded=False)
                    st.video(diagnostic_video)
                    st.download_button("Download diagnostic clip", diagnostic_video, file_name="diagnostic_vertical_captioned.mp4", mime="video/mp4", key="diagnostic_download", use_container_width=True)
                except Exception as exc:
                    diagnostic_status.update(label="Diagnostic failed", state="error", expanded=True)
                    if not isinstance(exc, VideoDownloadError):
                        st.error(str(exc))

clips = st.session_state.get("clips", [])
if clips:
    st.subheader(f"Clips from: {st.session_state.get('video_title', 'video')}")
    for i, clip in enumerate(clips, 1):
        with st.container(border=True):
            st.markdown(f"**Clip {i} · {clip['start']:.1f}s–{clip['end']:.1f}s**")
            st.caption(clip["reason"])
            st.video(clip["bytes"])
            st.download_button("Download MP4", data=clip["bytes"], file_name=clip["name"], mime="video/mp4", key=f"download_{i}", use_container_width=True)

with st.expander("Privacy and runtime notes"):
    st.write("Video download, transcription, cropping, and subtitle rendering run on the machine hosting this app. The video is not uploaded to OpenAI. If enabled, only the timestamped transcript and topic are sent to OpenAI for matching. Temporary source files are removed when processing ends; generated clips remain in this browser session for download.")
    st.write("This app is intended for videos you are allowed to download and reuse. Respect the source platform's terms and the creator's rights.")
