import os
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from plexio.models.utils import get_flag_emoji, guid_to_plexio_id, to_camel


class Resolution(str, Enum):
    R480 = '480p'
    R720 = '720p'
    R1080 = '1080p'


RESOLUTION_QUALITY_PARAMS = {
    Resolution.R1080: {
        'name': '1080p',
        'min_width': 1920,
        'plex_args': {
            'videoQuality': 100,
            'maxVideoBitrate': 10,
            'videoResolution': '1920x1080',
        },
    },
    Resolution.R720: {
        'name': '720p',
        'min_width': 1280,
        'plex_args': {
            'videoQuality': 100,
            'maxVideoBitrate': 6.5,
            'videoResolution': '1280x720',
        },
    },
    Resolution.R480: {
        'name': '480p',
        'min_width': 640,
        'plex_args': {
            'videoQuality': 100,
            'maxVideoBitrate': 3.5,
            'videoResolution': '640x480',
        },
    },
}


class PlexMediaType(str, Enum):
    show = 'show'
    movie = 'movie'
    episode = 'episode'


class PlexLibrarySection(BaseModel):
    key: str
    title: str
    type: PlexMediaType


# ---------------------------------------------------------------------------
# Helpers for rich metadata
# ---------------------------------------------------------------------------

VIDEO_RESOLUTION_MAP = {
    '4k': '4K (2160p)',
    '2160': '4K (2160p)',
    '1080': '1080p',
    '720': '720p',
    '576': '576p',
    '480': '480p',
    '360': '360p',
}

HDR_TRANSFER_MAP = {
    'smpte2084': 'HDR10',
    'arib-std-b67': 'HLG',
    'bt2020-10': 'HDR',
    'bt2020-12': 'HDR',
}


def _fmt_size(size_bytes: int | None) -> str | None:
    """Return human-readable file size (e.g. '12.3 GB')."""
    if not size_bytes:
        return None
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size_bytes < 1024:
            return f'{size_bytes:.1f} {unit}'
        size_bytes /= 1024
    return f'{size_bytes:.1f} PB'


def _fmt_bitrate(bitrate_kbps: int | None) -> str | None:
    """Return human-readable bitrate (e.g. '45.2 Mbps')."""
    if not bitrate_kbps:
        return None
    if bitrate_kbps >= 1000:
        return f'{bitrate_kbps / 1000:.1f} Mbps'
    return f'{bitrate_kbps} kbps'


def _get_video_info(media: dict) -> dict:
    """
    Extract structured video metadata from a Plex Media object.
    Returns a dict with keys: resolution, codec, hdr, dv, bitrate, size.
    """
    result = {
        'resolution': None,
        'codec': None,
        'hdr': None,
        'dv': False,
        'bitrate': None,
        'size': None,
    }

    # Resolution
    raw_res = (media.get('videoResolution') or '').lower()
    result['resolution'] = VIDEO_RESOLUTION_MAP.get(raw_res, raw_res.upper() if raw_res else None)

    # Video codec (normalize to uppercase, e.g. h264 -> H.264)
    raw_codec = (media.get('videoCodec') or '').lower()
    codec_map = {
        'h264': 'H.264',
        'h265': 'H.265/HEVC',
        'hevc': 'H.265/HEVC',
        'av1': 'AV1',
        'vp9': 'VP9',
        'mpeg2video': 'MPEG-2',
        'mpeg4': 'MPEG-4',
        'vc1': 'VC-1',
    }
    result['codec'] = codec_map.get(raw_codec, raw_codec.upper() if raw_codec else None)

    # Bitrate & size from the Part
    parts = media.get('Part', [])
    if parts:
        part = parts[0]
        result['bitrate'] = _fmt_bitrate(media.get('bitrate'))
        result['size'] = _fmt_size(part.get('size'))

    # HDR / Dolby Vision — inspect per-stream video entries
    for stream in (parts[0].get('Stream', []) if parts else []):
        if stream.get('streamType') != 1:  # 1 = video stream
            continue
        transfer = (stream.get('transferCharacteristics') or '').lower()
        dov = stream.get('DOVIPresent') or stream.get('doviPresent') or False
        if dov:
            result['dv'] = True
        hdr_label = HDR_TRANSFER_MAP.get(transfer)
        if hdr_label and not result['hdr']:
            result['hdr'] = hdr_label
        # HDR10+ detection via colorTrc
        if 'smpte2094' in transfer:
            result['hdr'] = 'HDR10+'

    return result


def _get_audio_info(media: dict) -> dict:
    """
    Extract structured audio metadata (codec, channels, Atmos/DTS-X flag)
    from the first audio stream of a Plex media object.
    Returns a dict with keys: codec, channels, atmos, dtsx.
    """
    result = {'codec': None, 'channels': None, 'atmos': False, 'dtsx': False}
    parts = media.get('Part', [])
    if not parts:
        return result

    audio_codec_map = {
        'aac': 'AAC',
        'ac3': 'Dolby Digital',
        'eac3': 'Dolby Digital+',
        'truehd': 'TrueHD',
        'dca': 'DTS',
        'dts': 'DTS',
        'flac': 'FLAC',
        'mp3': 'MP3',
        'opus': 'Opus',
        'vorbis': 'Vorbis',
        'pcm_s16le': 'PCM',
        'pcm_s24le': 'PCM',
    }

    for stream in parts[0].get('Stream', []):
        if stream.get('streamType') != 2:  # 2 = audio
            continue
        raw = (stream.get('codec') or '').lower()
        result['codec'] = audio_codec_map.get(raw, raw.upper() if raw else None)

        ch = stream.get('channels')
        if ch:
            ch_map = {1: 'Mono', 2: 'Stereo', 6: '5.1', 8: '7.1'}
            result['channels'] = ch_map.get(ch, f'{ch}ch')

        display = (stream.get('displayTitle') or '').lower()
        extended = (stream.get('extendedDisplayTitle') or '').lower()
        combined = display + ' ' + extended
        if 'atmos' in combined:
            result['atmos'] = True
        if 'dts:x' in combined or 'dtsx' in combined or 'dts-x' in combined:
            result['dtsx'] = True
        break  # only use first audio track

    return result


def _build_stream_description(
    filename: str,
    video: dict,
    audio: dict,
    languages: str,
    quality_prefix: str,
) -> str:
    """
    Build a rich stream description compatible with AIOStreams parsing.

    Line 1  – quality / resolution
    Line 2  – video codec + HDR/DV badges
    Line 3  – audio codec + channels + Atmos/DTS-X
    Line 4  – languages
    Line 5  – file size + bitrate
    Line 6  – filename
    """
    lines = []

    # --- Line 1: resolution / quality prefix ---
    res_label = video.get('resolution') or ''
    lines.append(f'{quality_prefix} {res_label}'.strip())

    # --- Line 2: video codec + HDR/DV ---
    video_parts = []
    if video.get('codec'):
        video_parts.append(f'🎞 {video["codec"]}')
    hdr_badges = []
    if video.get('hdr'):
        hdr_badges.append(video['hdr'])
    if video.get('dv'):
        hdr_badges.append('DV')
    if hdr_badges:
        video_parts.append(' • '.join(hdr_badges))
    if video_parts:
        lines.append(' | '.join(video_parts))

    # --- Line 3: audio ---
    audio_parts = []
    if audio.get('codec'):
        audio_label = audio['codec']
        if audio.get('atmos'):
            audio_label += ' Atmos'
        elif audio.get('dtsx'):
            audio_label += ' DTS:X'
        audio_parts.append(f'🎧 {audio_label}')
    if audio.get('channels'):
        audio_parts.append(f'🔊 {audio["channels"]}')
    if audio_parts:
        lines.append(' '.join(audio_parts))

    # --- Line 4: languages ---
    if languages:
        lines.append(languages)

    # --- Line 5: size + bitrate ---
    meta_parts = []
    if video.get('size'):
        meta_parts.append(f'📦 {video["size"]}')
    if video.get('bitrate'):
        meta_parts.append(f'⚡ {video["bitrate"]}')
    if meta_parts:
        lines.append(' '.join(meta_parts))

    # --- Line 6: filename ---
    lines.append(f'📎 {filename}')

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Plex models
# ---------------------------------------------------------------------------

class PlexMediaMeta(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    guid: str
    type: PlexMediaType
    title: str
    added_at: int = 0

    rating_key: str | None = None
    key: str | None = None
    studio: str | None = None
    title_sort: str | None = None
    library_section_title: str | None = None
    library_sectionID: str | None = None
    library_section_key: str | None = None
    content_rating: str | None = None
    summary: str = ''
    rating: float | None = None
    audience_rating: float | None = None
    year: int | None = None
    tagline: str | None = None
    thumb: str | None = None
    art: str | None = None
    duration: int | None = None
    originally_available_at: str | None = None
    updated_at: int | None = None
    audience_rating_image: str | None = None
    has_premium_primary_extra: str | None = None
    rating_image: str | None = None
    media: list = Field(alias='Media', default_factory=list)
    genre: list = Field(alias='Genre', default_factory=list)
    country: list = Field(alias='Country', default_factory=list)
    guids: list = Field(alias='Guid', default_factory=list)
    ratings: list = Field(alias='Ratings', default_factory=list)
    director: list = Field(alias='Director', default_factory=list)
    writer: list = Field(alias='Writer', default_factory=list)
    role: list = Field(alias='Role', default_factory=list)
    producer: list = Field(alias='Producer', default_factory=list)

    def get_year(self):
        if self.year:
            return str(self.year)
        return datetime.fromtimestamp(self.added_at).strftime('%Y')

    def to_stremio_meta(self, configuration):
        from plexio.models import PLEX_TO_STREMIO_MEDIA_TYPE
        from plexio.models.stremio import StremioMeta

        return StremioMeta(
            id=guid_to_plexio_id(self.guid),
            type=PLEX_TO_STREMIO_MEDIA_TYPE[self.type],
            name=self.title,
            releaseInfo=self.get_year(),
            imdbRating=self.audience_rating,
            description=self.summary,
            poster=str(
                configuration.streaming_url
                / self.thumb[1:]
                % {'X-Plex-Token': configuration.access_token},
            )
            if self.thumb
            else None,
            background=str(
                configuration.streaming_url
                / (self.art or self.thumb)[1:]
                % {'X-Plex-Token': configuration.access_token},
            )
            if (self.art or self.thumb)
            else None,
            genres=[g['tag'] for g in self.genre],
        )

    def to_stremio_meta_review(self, configuration):
        from plexio.models import PLEX_TO_STREMIO_MEDIA_TYPE
        from plexio.models.stremio import StremioMetaPreview

        stremio_id = None
        guids = self.guids
        for guid in guids:
            if guid['id'].startswith('imdb://'):
                stremio_id = guid['id'][7:]

        if not stremio_id:
            if '://' in self.guid:
                stremio_id = guid_to_plexio_id(self.guid)
            else:
                stremio_id = self.guid

        return StremioMetaPreview(
            id=stremio_id,
            name=self.title,
            releaseInfo=str(self.year),
            poster=str(
                configuration.streaming_url
                / self.thumb[1:]
                % {'X-Plex-Token': configuration.access_token},
            )
            if self.thumb
            else None,
            type=PLEX_TO_STREMIO_MEDIA_TYPE[self.type],
            imdbRating=self.audience_rating,
            description=self.summary,
            genres=[g['tag'] for g in self.genre],
        )

    def get_stremio_streams(self, configuration):
        from plexio.models.stremio import StremioStream

        streams = []
        for i, media in enumerate(self.media):
            name = f'{configuration.server_name} {self.library_section_title}'
            filename = os.path.basename(media['Part'][0]['file'])

            audio_languages = set()
            subtitles_languages = set()
            external_subtitles = []
            for part_stream in media['Part'][0].get('Stream', []):
                if part_stream['streamType'] == 2:
                    audio_languages.add(
                        get_flag_emoji(part_stream.get('languageTag', 'Unknown')),
                    )
                elif part_stream['streamType'] == 3:
                    subtitles_languages.add(
                        get_flag_emoji(part_stream.get('languageTag', 'Unknown')),
                    )
                    if 'key' in part_stream:
                        external_subtitles.append(
                            {
                                'id': str(part_stream['id']),
                                'lang': part_stream['displayTitle'],
                                'url': str(
                                    configuration.streaming_url
                                    / part_stream['key'][1:]
                                    % {
                                        'X-Plex-Token': configuration.access_token,
                                    }
                                ),
                            }
                        )

            languages = ' | '.join(sorted(audio_languages))
            if subtitles_languages:
                languages += f' (\'{"/".join(sorted(subtitles_languages))})'

            # --- Rich metadata extraction ---
            video_info = _get_video_info(media)
            audio_info = _get_audio_info(media)

            quality_description = f'Direct Play {media.get("videoResolution", "")}'
            description = _build_stream_description(
                filename=filename,
                video=video_info,
                audio=audio_info,
                languages=languages,
                quality_prefix='Direct Play',
            )
            streams.append(
                StremioStream(
                    name=name,
                    description=description,
                    url=str(
                        configuration.streaming_url
                        / media['Part'][0]['key'][1:]
                        % {
                            'X-Plex-Token': configuration.access_token,
                        },
                    ),
                    subtitles=external_subtitles,
                    behaviorHints={'bingeGroup': quality_description},
                ),
            )

            transcode_url = (
                configuration.streaming_url
                / 'video/:/transcode/universal/start.m3u8'
                % {
                    'path': self.key,
                    'mediaIndex': i,
                    'protocol': 'hls',
                    'fastSeek': 1,
                    'copyts': 1,
                    'autoAdjustQuality': 0,
                    'X-Plex-Platform': 'Chrome',
                    'X-Plex-Token': configuration.access_token,
                }
            )
            if configuration.include_transcode_original:
                quality_description = (
                    f'Transcode {media.get("videoResolution", "")} (original)'
                )
                description = _build_stream_description(
                    filename=filename,
                    video=video_info,
                    audio=audio_info,
                    languages=languages,
                    quality_prefix='Transcode (original)',
                )
                streams.append(
                    StremioStream(
                        name=name,
                        description=description,
                        url=str(transcode_url % {'videoQuality': 100}),
                        subtitles=external_subtitles,
                        behaviorHints={'bingeGroup': quality_description},
                    ),
                )

            if configuration.include_transcode_down:
                for quality in configuration.transcode_down_qualities:
                    quality_params = RESOLUTION_QUALITY_PARAMS[quality]
                    if media['width'] <= quality_params['min_width']:
                        continue
                    quality_description = f'Transcode {quality_params["name"]}'
                    description = _build_stream_description(
                        filename=filename,
                        video={**video_info, 'resolution': quality_params['name']},
                        audio=audio_info,
                        languages=languages,
                        quality_prefix=f'Transcode {quality_params["name"]}',
                    )
                    streams.append(
                        StremioStream(
                            name=name,
                            description=description,
                            url=str(transcode_url % quality_params['plex_args']),
                            subtitles=external_subtitles,
                            behaviorHints={'bingeGroup': quality_description},
                        ),
                    )

            if configuration.include_plex_tv and self.guid.startswith('plex:'):
                streams.append(
                    StremioStream(
                        name=name,
                        description='Open on plex.tv (external)',
                        externalUrl=f'https://app.plex.tv/#!/provider/tv.plex.provider.metadata/details?key=/library/metadata/{self.guid.split("/")[-1]}',
                    ),
                )

        return streams


class PlexEpisodeMeta(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    guid: str
    title: str
    index: int
    parent_index: int = 0
    added_at: int = 0

    type: str | None = None
    rating_key: str | None = None
    key: str | None = None
    parent_rating_key: str | None = None
    grandparent_rating_key: str | None = None
    studio: str | None = None
    grandparent_key: str | None = None
    parent_key: str | None = None
    grandparent_title: str | None = None
    parent_title: str | None = None
    content_rating: str | None = None
    summary: str = ''
    year: int | None = None
    thumb: str | None = None
    art: str | None = None
    parent_thumb: str | None = None
    grandparent_thumb: str | None = None
    grandparent_art: str | None = None
    grandparent_theme: str | None = None
    duration: int | None = None
    originally_available_at: str | None = None
    updated_at: int | None = None
    media: list = Field(default_factory=list)

    def to_stremio_video_meta(self, configuration):
        from plexio.models.stremio import StremioVideoMeta

        if self.originally_available_at:
            released = f'{self.originally_available_at}T00:00:00.000Z'
        else:
            released = datetime.fromtimestamp(self.added_at).strftime(
                '%Y-%m-%dT%H:%M:%S.%fZ',
            )

        return StremioVideoMeta(
            id=guid_to_plexio_id(self.guid),
            title=self.title,
            released=released,
            thumbnail=str(
                configuration.streaming_url
                / self.thumb[1:]
                % {'X-Plex-Token': configuration.access_token},
            )
            if self.thumb
            else None,
            episode=self.index,
            season=self.parent_index,
            overview=self.summary,
        )
