"""Install project-local FFmpeg tools on Windows for the video clipper."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parent
BIN_DIR = PROJECT_ROOT / "bin"
ARCHIVE_NAME = "ffmpeg-release-essentials.zip"
ARCHIVE_URL = f"https://www.gyan.dev/ffmpeg/builds/{ARCHIVE_NAME}"
CHECKSUM_URL = f"{ARCHIVE_URL}.sha256"


def locate_archive() -> Path | None:
    candidates = (
        PROJECT_ROOT / ARCHIVE_NAME,
        Path.home() / "Downloads" / ARCHIVE_NAME,
        Path.home() / "download" / ARCHIVE_NAME,
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def download_archive(destination: Path) -> None:
    print(f"Downloading FFmpeg essentials from {ARCHIVE_URL}")
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl:
        # Resolve the release redirect and archive size with a one-byte range request.
        probe = subprocess.run(
            [curl, "--silent", "--show-error", "--fail", "--location", "--range", "0-0", "--dump-header", "-", "--output", "NUL", "--write-out", "\nFINAL_URL:%{url_effective}", ARCHIVE_URL],
            check=True, capture_output=True, text=True,
        )
        match = re.search(r"(?im)^Content-Range:\s*bytes\s+0-0/(\d+)\s*$", probe.stdout)
        url_match = re.search(r"(?m)^FINAL_URL:(.+)$", probe.stdout)
        if not match or not url_match:
            raise RuntimeError("Could not determine the FFmpeg archive size from its HTTP range response.")
        total_size = int(match.group(1))
        final_url = url_match.group(1).strip()
        workers = min(8, max(2, (os.cpu_count() or 2)))
        chunk_size = (total_size + workers - 1) // workers
        parts_dir = Path(tempfile.mkdtemp(prefix=".ffmpeg_parts_", dir=PROJECT_ROOT))
        completed_bytes = 0
        lock = threading.Lock()

        def fetch_part(index: int) -> tuple[Path, int]:
            start = index * chunk_size
            end = min(total_size - 1, start + chunk_size - 1)
            part = parts_dir / f"part_{index:02d}"
            subprocess.run(
                [curl, "--silent", "--show-error", "--fail", "--location", "--retry", "3", "--connect-timeout", "30", "--max-time", "900", "--range", f"{start}-{end}", "--output", str(part), final_url],
                check=True,
            )
            expected_size = end - start + 1
            if part.stat().st_size != expected_size:
                raise RuntimeError(f"FFmpeg archive range {start}-{end} returned {part.stat().st_size} bytes; expected {expected_size}.")
            nonlocal completed_bytes
            with lock:
                completed_bytes += expected_size
                print(f"Download progress: {completed_bytes * 100 / total_size:.1f}%", flush=True)
            return part, expected_size

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output, ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(fetch_part, index) for index in range(workers)]
                parts = {}
                for future in as_completed(futures):
                    part, _ = future.result()
                    parts[part.name] = part
                for index in range(workers):
                    with parts[f"part_{index:02d}"].open("rb") as source:
                        shutil.copyfileobj(source, output)
        finally:
            shutil.rmtree(parts_dir, ignore_errors=True)
        return

    request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "local-ai-video-clipper-setup/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            output.write(block)
            print(f"Downloaded {output.tell() / (1024 * 1024):.1f} MiB", end="\r", flush=True)
    print()


def verify_checksum(archive: Path) -> None:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    try:
        if curl:
            result = subprocess.run(
                [curl, "--fail", "--location", "--silent", "--show-error", "--max-time", "60", CHECKSUM_URL],
                check=True, capture_output=True, text=True,
            )
            checksum_text = result.stdout
        else:
            request = urllib.request.Request(CHECKSUM_URL, headers={"User-Agent": "local-ai-video-clipper-setup/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                checksum_text = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError(f"Could not retrieve FFmpeg's published SHA-256 checksum: {exc}") from exc
    match = re.search(r"\b([a-fA-F0-9]{64})\b", checksum_text)
    if not match:
        raise RuntimeError("The FFmpeg checksum response did not contain a SHA-256 digest.")
    expected = match.group(1).lower()
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise RuntimeError("FFmpeg archive SHA-256 verification failed; no files were extracted.")
    print("FFmpeg archive SHA-256 verified.")


def extract_executables(archive: Path) -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        members = {}
        for member in source.infolist():
            normalized = member.filename.replace("\\", "/")
            if PurePosixPath(normalized).parent.name.lower() == "bin":
                members[PurePosixPath(normalized).name.lower()] = member
        for executable in ("ffmpeg.exe", "ffprobe.exe"):
            member = members.get(executable)
            if member is None:
                raise RuntimeError(f"{ARCHIVE_NAME} does not contain bin/{executable}.")
            with source.open(member) as input_file, (BIN_DIR / executable).open("wb") as output_file:
                shutil.copyfileobj(input_file, output_file)
            print(f"Installed {BIN_DIR / executable}")


def install_imageio_ffmpeg() -> bool:
    """Use MoviePy's bundled FFmpeg build when a full archive is not available."""
    try:
        import imageio_ffmpeg

        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return False
    if not bundled.is_file():
        return False
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / "ffmpeg.exe"
    shutil.copy2(bundled, target)
    print(f"Installed project-local FFmpeg from imageio-ffmpeg: {target}")
    print("This bundled build includes the subtitles filter; ffprobe will be used if available.")
    return True


def verify_installation() -> None:
    os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")
    for executable in ("ffmpeg", "ffprobe"):
        resolved = shutil.which(executable)
        if not resolved:
            if executable == "ffprobe":
                print("ffprobe is not installed; the app can verify clip dimensions through FFmpeg's input report.")
                continue
            raise RuntimeError(f"shutil.which('{executable}') could not find the project-local executable.")
        result = subprocess.run([resolved, "-version"], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"{resolved} failed its version check: {result.stderr.strip()}")
        print(f"shutil.which('{executable}') -> {resolved}")

    ffmpeg = shutil.which("ffmpeg")
    filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30)
    if filters.returncode != 0 or not re.search(r"\bsubtitles\b", filters.stdout):
        raise RuntimeError("FFmpeg is installed but its build does not include the subtitles (libass) filter.")
    print("FFmpeg subtitles (libass) filter is available.")


def main() -> None:
    archive = locate_archive()
    temporary_archive: Path | None = None
    try:
        if archive is not None:
            print(f"Using existing archive: {archive}")
            verify_checksum(archive)
            extract_executables(archive)
        elif not install_imageio_ffmpeg():
            with tempfile.NamedTemporaryFile(prefix=".ffmpeg_essentials_", suffix=".zip", dir=PROJECT_ROOT, delete=False) as temp:
                temporary_archive = Path(temp.name)
            archive = temporary_archive
            download_archive(archive)
            verify_checksum(archive)
            extract_executables(archive)
        else:
            # A full archive can still be installed later to add ffprobe.exe.
            print("Skipping the 109 MiB FFmpeg archive because a subtitle-capable local FFmpeg is already bundled.")
        verify_installation()
        print("FFmpeg setup completed successfully. Restart Streamlit to use the local binaries.")
    finally:
        if temporary_archive and temporary_archive.exists():
            temporary_archive.unlink()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"FFmpeg setup failed: {exc}") from exc
