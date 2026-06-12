import asyncio
import multiprocessing
import queue
import re
import shutil
import traceback
import unicodedata
from pathlib import Path

import structlog
from mutagen.mp4 import MP4, MP4Cover
from yt_dlp import YoutubeDL

from ..interface.enums import CoverFormat
from ..interface.interface import AppleMusicInterface
from ..interface.types import MediaTags, PlaylistTags
from ..utils import CustomStringFormatter, async_subprocess
from .constants import FULLWIDTH_REPLACEMENTS, ILLEGAL_CHAR_REPLACEMENT, ILLEGAL_CHARS_RE, TEMP_PATH_TEMPLATE
from .enums import DownloadMode

logger = structlog.get_logger(__name__)


def _download_ytdlp_process(
    stream_url: str,
    download_path: str,
    silent: bool,
    result_queue,
) -> None:
    try:
        with YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "outtmpl": download_path,
                "allow_unplayable_formats": True,
                "overwrites": True,
                "fixup": "never",
                "noprogress": silent,
                "allowed_extractors": ["generic"],
            }
        ) as ydl:
            ydl.download(stream_url)
    except Exception as e:
        result_queue.put(("error", repr(e), traceback.format_exc()))


class AppleMusicBaseDownloader:
    def __init__(
        self,
        interface: AppleMusicInterface,
        output_path: str = "./Apple Music",
        music_video_output_path: str = None,
        temp_path: str = ".",
        nm3u8dlre_path: str = "N_m3u8DL-RE",
        download_mode: DownloadMode = DownloadMode.YTDLP,
        album_folder_template: str = "{album_artist}/{album}",
        compilation_folder_template: str = "Compilations/{album}",
        no_album_folder_template: str = "{artist}/Unknown Album",
        playlist_folder_template: str = "Playlists/{playlist_artist}",
        single_disc_file_template: str = "{track:02d} {title}",
        multi_disc_file_template: str = "{disc}-{track:02d} {title}",
        no_album_file_template: str = "{title}",
        playlist_file_template: str = "{playlist_title}",
        playlist_track_file_template: str = "{artists} - {title}{explicit}",
        music_video_folder_template: str = "{artist_initials}/{album_artist}",
        music_video_file_template: str = "({date:%Y}) {artists} - {title}{explicit}",
        date_tag_template: str = "%Y-%m-%dT%H:%M:%SZ",
        exclude_tags: list[str] = None,
        truncate: int = None,
        silent: bool = False,
        artist_separator: str = " & ",
        use_fullwidth_replacements: bool = True,
    ):
        self.interface = interface
        self.output_path = output_path
        self.music_video_output_path = music_video_output_path or output_path
        self.temp_path = temp_path
        self.nm3u8dlre_path = nm3u8dlre_path
        self.download_mode = download_mode
        self.album_folder_template = album_folder_template
        self.compilation_folder_template = compilation_folder_template
        self.no_album_folder_template = no_album_folder_template
        self.single_disc_file_template = single_disc_file_template
        self.multi_disc_file_template = multi_disc_file_template
        self.playlist_folder_template = playlist_folder_template
        self.no_album_file_template = no_album_file_template
        self.playlist_file_template = playlist_file_template
        self.playlist_track_file_template = playlist_track_file_template
        self.music_video_folder_template = music_video_folder_template
        self.music_video_file_template = music_video_file_template
        self.date_tag_template = date_tag_template
        self.exclude_tags = exclude_tags
        self.truncate = truncate
        self.silent = silent
        # Strip surrounding quotes so config.ini values like " / " work correctly
        self.artist_separator = (
            artist_separator.strip("\"'") if isinstance(artist_separator, str) else " & "
        )
        self.use_fullwidth_replacements = use_fullwidth_replacements

        self._initialize_binary_paths()

    def _initialize_binary_paths(self):
        log = logger.bind(action="initialize_binary_paths")

        self.full_nm3u8dlre_path = shutil.which(self.nm3u8dlre_path)

        log = log.debug(
            "success",
            full_nm3u8dlre_path=self.full_nm3u8dlre_path,
        )

    def get_temp_path(
        self,
        media_id: str,
        folder_tag: str,
        file_tag: str,
        file_extension: str,
    ) -> str:
        log = logger.bind(action="get_temp_path")

        temp_path = str(
            Path(self.temp_path)
            / TEMP_PATH_TEMPLATE.format(folder_tag)
            / (f"{media_id}_{file_tag}" + file_extension)
        )

        log.debug("success", temp_path=temp_path)

        return temp_path

    def _get_artist_initials(self, name: str) -> str:
        """Return first ASCII letter of name, or '#' for non-Latin (matching OrpheusDL)."""
        ch = (name or "").strip()[:1].upper()
        if not ch:
            return "#"
        normalized = "".join(
            c for c in unicodedata.normalize("NFD", ch)
            if unicodedata.category(c) != "Mn"
        )
        return normalized if "A" <= normalized <= "Z" else "#"

    def _apply_artist_separator(self, artist_str: str, featured: list = None) -> str:
        """Split and rejoin artists in alphabetical order for cross-platform consistency."""
        if not artist_str:
            return artist_str

        all_parts = []
        for segment in re.split(r" & ", artist_str):
            all_parts.extend(re.split(r", ", segment))
        all_parts = [p.strip() for p in all_parts if p.strip()]

        return self.artist_separator.join(sorted(all_parts))

    def _sanitize_string(
        self,
        dirty_string: str,
        file_ext: str = None,
    ) -> str:
        # Strip control characters (ASCII 0-31) — illegal in Windows filenames
        sanitized_string = re.sub(r"[\x00-\x1f]", "", dirty_string)

        if self.use_fullwidth_replacements:
            for char, replacement in FULLWIDTH_REPLACEMENTS.items():
                sanitized_string = sanitized_string.replace(char, replacement)
        else:
            sanitized_string = re.sub(
                ILLEGAL_CHARS_RE,
                ILLEGAL_CHAR_REPLACEMENT,
                sanitized_string,
            )

        if file_ext is None:
            sanitized_string = sanitized_string[: self.truncate]
            if sanitized_string.endswith("."):
                sanitized_string = sanitized_string[:-1] + ILLEGAL_CHAR_REPLACEMENT
        else:
            if self.truncate is not None:
                sanitized_string = sanitized_string[: self.truncate - len(file_ext)]
            sanitized_string += file_ext

        return sanitized_string.strip()

    def get_final_path(
        self,
        tags: MediaTags,
        file_extension: str,
        playlist_tags: PlaylistTags | None,
    ) -> str:
        log = logger.bind(action="get_final_path")

        if playlist_tags:
            template_folder_parts = self.playlist_folder_template.split("/")
            template_file_parts = self.playlist_track_file_template.split("/")
        elif tags.album:
            template_folder_parts = (
                self.compilation_folder_template.split("/")
                if tags.compilation
                else self.album_folder_template.split("/")
            )
            template_file_parts = (
                self.multi_disc_file_template.split("/")
                if isinstance(tags.disc_total, int) and tags.disc_total > 1
                else self.single_disc_file_template.split("/")
            )
        else:
            template_folder_parts = self.no_album_folder_template.split("/")
            template_file_parts = self.no_album_file_template.split("/")

        template_parts = template_folder_parts + template_file_parts
        formatted_parts = []

        _artists = self._apply_artist_separator(
            tags.artist or "",
            featured=getattr(tags, "featured_artists", None),
        )
        _album_artists = self._apply_artist_separator(tags.album_artist or "")
        _explicit = (
            " (explicit)" if tags.rating is not None and tags.rating.value == 1 else ""
        )
        _album_clean = re.sub(
            r"\s*-\s*(?:Single|EP|Single Version|Deluxe Edition|Deluxe Version|"
            r"Expanded Edition|Special Edition|Remastered|Remaster)\s*$",
            "",
            tags.album or "",
            flags=re.IGNORECASE,
        ).strip() if tags.album else None
        # Strip parenthetical explicit/clean suffixes: "(Explicit)" / "(Clean)"
        _album_clean = re.sub(
            r"\s*\((Explicit|Clean)\)\s*$", "", _album_clean or "", flags=re.IGNORECASE
        ).strip() or _album_clean
        # Strip artist mention suffixes from album names:
        # (feat. X), (ft. X), (featuring X), (with X), (duet with X), (con X), (& X)
        _feat_kw = r"f(?:ea)?t\.?|featuring|with|duet\s+with|w/|con|junto\s+a|starring|prod\.?\s*by|prod\.?|&"
        _album_clean = re.sub(
            rf"\s*[\(\[]\s*(?:{_feat_kw})\s*[^\)\]]+[\)\]]\s*$",
            "", _album_clean or "", flags=re.IGNORECASE
        ).strip() or _album_clean

        _artist_initials = self._get_artist_initials(tags.album_artist or tags.artist)
        _release = getattr(tags, "release_type", None) or "ALBUM"

        for i, part in enumerate(template_parts):
            is_folder = i < len(template_parts) - 1
            formatted_part = CustomStringFormatter().format(
                part,
                album=(tags.album, "Unknown Album"),
                album_clean=(_album_clean, "Unknown Album"),
                album_artist=(_album_artists or tags.album_artist, "Unknown Artist"),
                artist_initials=(_artist_initials, "#"),
                album_id=(tags.album_id, "Unknown Album ID"),
                artist=(_artists or tags.artist, "Unknown Artist"),
                artist_id=(tags.artist_id, "Unknown Artist ID"),
                artists=(_artists, "Unknown Artist"),
                release=(_release, "ALBUM"),
                composer=(tags.composer, "Unknown Composer"),
                composer_id=(tags.composer_id, "Unknown Composer ID"),
                date=(tags.date, "Unknown Date"),
                disc=(tags.disc, ""),
                disc_total=(tags.disc_total, ""),
                explicit=(_explicit, ""),
                media_type=(tags.media_type, "Unknown Media Type"),
                playlist_artist=(
                    (playlist_tags.artist if playlist_tags else None),
                    "Unknown Playlist Artist",
                ),
                playlist_id=(
                    (playlist_tags.playlist_id if playlist_tags else None),
                    "Unknown Playlist ID",
                ),
                playlist_title=(
                    (playlist_tags.title if playlist_tags else None),
                    "Unknown Playlist Title",
                ),
                playlist_track=(
                    (playlist_tags.track if playlist_tags else None),
                    "",
                ),
                title=(tags.title, "Unknown Title"),
                title_id=(tags.title_id, "Unknown Title ID"),
                track=(tags.track, ""),
                track_total=(tags.track_total, ""),
            )
            sanitized_formatted_part = self._sanitize_string(
                formatted_part,
                file_extension if not is_folder else None,
            )
            formatted_parts.append(sanitized_formatted_part)

        final_path = str(Path(self.output_path, *formatted_parts))

        # If the computed album folder doesn't exist yet, check if a sibling folder
        # with the same album name (ignoring release type suffixes) already has audio
        # files — if so, reuse that folder to avoid duplicates.
        if not playlist_tags and tags.album:
            computed_folder = Path(self.output_path, *formatted_parts[:-1])
            if not computed_folder.exists():
                parent = computed_folder.parent
                _audio_exts = {".m4a", ".flac", ".mp3", ".ogg", ".opus", ".wav", ".mp4", ".m4v"}
                _rt_re = re.compile(
                    r"\s*\((ALBUM|SINGLE|EP|COMPILATION|ANTHOLOGY)\)\s*$", re.IGNORECASE
                )

                def _norm(s: str) -> str:
                    s = _rt_re.sub("", s).strip()
                    decomposed = unicodedata.normalize("NFD", s)
                    return "".join(
                        c for c in decomposed if unicodedata.category(c) != "Mn"
                    ).lower().strip()

                comp_norm = _norm(computed_folder.name)
                # Extract computed release type from folder name for comparison
                comp_rt_match = _rt_re.search(computed_folder.name)
                comp_rt = comp_rt_match.group(1).upper() if comp_rt_match else ""
                try:
                    for sibling in parent.iterdir():
                        if not sibling.is_dir() or sibling == computed_folder:
                            continue
                        if _norm(sibling.name) != comp_norm:
                            continue
                        # Only redirect if sibling has the same release type
                        sib_rt_match = _rt_re.search(sibling.name)
                        sib_rt = sib_rt_match.group(1).upper() if sib_rt_match else ""
                        if sib_rt != comp_rt:
                            continue
                        try:
                            has_audio = any(
                                f.suffix.lower() in _audio_exts
                                for f in sibling.iterdir()
                                if f.is_file()
                            )
                        except OSError:
                            has_audio = False
                        if has_audio:
                            final_path = str(sibling / formatted_parts[-1])
                            log.debug("reusing_existing_folder", folder=str(sibling))
                            break
                except OSError:
                    pass

        log.debug("success", final_path=final_path)

        return final_path

    def get_music_video_final_path(
        self,
        tags: MediaTags,
        file_extension: str,
    ) -> str:
        log = logger.bind(action="get_music_video_final_path")

        template_parts = (
            self.music_video_folder_template.split("/")
            + self.music_video_file_template.split("/")
        )

        _artist_initials = self._get_artist_initials(tags.album_artist or tags.artist)
        _artists = self._apply_artist_separator(
            tags.artist or "",
            featured=getattr(tags, "featured_artists", None),
        )
        _album_artists = self._apply_artist_separator(tags.album_artist or "")
        _explicit = (
            " (explicit)" if tags.rating is not None and tags.rating.value == 1 else ""
        )

        formatted_parts = []
        for i, part in enumerate(template_parts):
            is_folder = i < len(template_parts) - 1
            formatted_part = CustomStringFormatter().format(
                part,
                album=(tags.album, "Unknown Album"),
                album_artist=(_album_artists or tags.album_artist, "Unknown Artist"),
                artist_initials=(_artist_initials, "#"),
                artist=(_artists or tags.artist, "Unknown Artist"),
                artists=(_artists, "Unknown Artist"),
                date=(tags.date, "Unknown Date"),
                explicit=(_explicit, ""),
                title=(tags.title, "Unknown Title"),
            )
            sanitized = self._sanitize_string(
                formatted_part,
                file_extension if not is_folder else None,
            )
            formatted_parts.append(sanitized)

        final_path = str(Path(self.music_video_output_path, *formatted_parts))
        log.debug("success", final_path=final_path)
        return final_path

    async def download_stream(self, stream_url: str, download_path: str):
        log = logger.bind(
            action="download_stream", stream_url=stream_url, download_path=download_path
        )

        if self.download_mode == DownloadMode.YTDLP:
            await self._download_ytdlp_async(stream_url, download_path)

        if self.download_mode == DownloadMode.NM3U8DLRE:
            await self._download_nm3u8dlre(stream_url, download_path)

        log.debug("success")

    async def _download_ytdlp_async(self, stream_url: str, download_path: str) -> None:
        ctx = multiprocessing.get_context()
        result_queue = ctx.Queue()
        process = ctx.Process(
            target=_download_ytdlp_process,
            args=(stream_url, download_path, self.silent, result_queue),
        )
        process.start()

        try:
            while process.is_alive():
                await asyncio.sleep(0.1)

            process.join()

            try:
                status, error_repr, error_traceback = result_queue.get_nowait()
            except queue.Empty:
                status = None

            if status == "error":
                raise RuntimeError(
                    f"yt-dlp failed: {error_repr}\n{error_traceback}"
                ) from None

            if process.exitcode != 0:
                raise RuntimeError(f"yt-dlp exited with code {process.exitcode}")
        finally:
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join)
            process.close()

    async def _download_nm3u8dlre(self, stream_url: str, download_path: str):
        download_path_obj = Path(download_path)

        download_path_obj.parent.mkdir(parents=True, exist_ok=True)
        await async_subprocess(
            self.full_nm3u8dlre_path,
            stream_url,
            "--binary-merge",
            "--no-log",
            "--log-level",
            "off",
            "--save-name",
            download_path_obj.stem,
            "--save-dir",
            download_path_obj.parent,
            "--tmp-dir",
            download_path_obj.parent,
            silent=self.silent,
        )

    async def apply_tags(
        self,
        media_path: str,
        tags: MediaTags,
        cover_bytes: bytes | None,
    ):
        log = logger.bind(action="apply_tags", media_path=media_path)

        exclude_tags = self.exclude_tags or []

        filtered_tags = MediaTags(
            **{
                k: v
                for k, v in tags.__dict__.items()
                if v is not None and k not in exclude_tags
            }
        )
        mp4_tags = filtered_tags.as_mp4_tags(self.date_tag_template)

        skip_tagging = "all" in exclude_tags

        await asyncio.to_thread(
            self._apply_mp4_tags,
            media_path,
            mp4_tags,
            cover_bytes,
            skip_tagging,
        )

        log.debug("success")

    def _apply_mp4_tags(
        self,
        media_path: str,
        tags: dict,
        cover_bytes: bytes | None,
        skip_tagging: bool,
    ):
        mp4 = MP4(media_path)
        mp4.clear()

        if not skip_tagging:
            if cover_bytes is not None:
                mp4["covr"] = [
                    MP4Cover(
                        data=cover_bytes,
                        imageformat=(
                            MP4Cover.FORMAT_JPEG
                            if self.interface.base.cover_format == CoverFormat.JPG
                            else MP4Cover.FORMAT_PNG
                        ),
                    )
                ]
            mp4.update(tags)

        mp4.save()

    async def _apply_cover(
        self,
        mp4: MP4,
        cover_bytes: bytes | None,
    ) -> None:
        if cover_bytes is None:
            return

        mp4["covr"] = [
            MP4Cover(
                data=cover_bytes,
                imageformat=(
                    MP4Cover.FORMAT_JPEG
                    if self.interface.base.cover_format == CoverFormat.JPG
                    else MP4Cover.FORMAT_PNG
                ),
            )
        ]

    def get_playlist_file_path(
        self,
        tags: PlaylistTags,
    ) -> str:
        log = logger.bind(action="get_playlist_file_path")

        template_folder_parts = self.playlist_folder_template.split("/")
        template_file_parts = self.playlist_file_template.split("/")
        template_parts = template_folder_parts + template_file_parts
        formatted_parts = []

        for i, part in enumerate(template_parts):
            is_folder = i < len(template_parts) - 1
            formatted_part = CustomStringFormatter().format(
                part,
                playlist_artist=(tags.artist, "Unknown Playlist Artist"),
                playlist_id=(tags.playlist_id, "Unknown Playlist ID"),
                playlist_title=(tags.title, "Unknown Playlist Title"),
                playlist_track=(tags.track, ""),
            )
            file_ext = None if is_folder else ".m3u"
            sanitized_formatted_part = self._sanitize_string(
                formatted_part,
                file_ext,
            )
            formatted_parts.append(sanitized_formatted_part)

        final_path = str(Path(self.output_path, *formatted_parts))

        log.debug("success", playlist_file_path=final_path)

        return final_path
