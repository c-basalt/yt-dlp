import json
import random
import time
import urllib.parse

from .common import InfoExtractor
from ..utils import (
    ExtractorError,
    UserNotLive,
    clean_html,
    int_or_none,
    jwt_decode_hs256,
    parse_iso8601,
    traverse_obj,
    try_get,
    url_or_none,
)


class WithnyBaseIE(InfoExtractor):
    _NETRC_MACHINE = 'withny'

    def _login_hint(self, *args, **kwargs):
        return super()._login_hint().replace('--cookies-from-browser, ', '') + '. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass cookies'

    def _get_cookie(self, key, transform=lambda x: x):
        return try_get(self._get_cookies('https://www.withny.fun/').get(key), lambda x: transform(x.value))

    def _parse_token_expire(self, token):
        return try_get(token, lambda x: int(jwt_decode_hs256(x.split()[-1])['exp'])) or 0

    @property
    def bearer_token(self):
        token = self._get_cookie('auth._token.local', urllib.parse.unquote)
        if not token:
            self.report_warning(self._login_hint())
        elif time.time() > self._parse_token_expire(token):
            self.report_warning(f'Token cookie has expired, please provide updated cookies. {self._login_hint()}')
        return token

    def _perform_login(self, username, password):
        if time.time() < self._parse_token_expire(self._get_cookie('auth._token.local', urllib.parse.unquote)) - 3600:
            return
        self.report_login()
        data = self._download_json(
            'https://www.withny.fun/api/auth/login', None, False,
            data=json.dumps({'email': username, 'password': password}).encode(),
            headers={'Referer': 'https://www.withny.fun/login', 'Content-Type': 'application/json'})

        set_cookie = lambda key, value: self._set_cookie('www.withny.fun', key, str(value))

        set_cookie('auth._token.local', urllib.parse.quote(f'{data["tokenType"]} {data["token"]}'))
        set_cookie('auth._token_expiration.local', self._parse_token_expire(data['token']) * 1000)
        set_cookie('auth._refresh_token.local', data['refreshToken'])
        set_cookie('auth._refresh_token_expiration.local',
                   int((self._parse_token_expire(data['token']) + 2505600 + random.random()) * 1000))

    def _download_webpage_nuxt(self, url, video_id, login_msg='You need to login to access', **kwargs):
        webpage, urlh = self._download_webpage_handle(url, video_id, **kwargs)
        if urlh.url == 'https://www.withny.fun/login':
            self.raise_login_required(login_msg)
        return self._search_nuxt_data(webpage, video_id)

    def _parse_archive_data(self, archive_data, video_id):
        m3u8_url = traverse_obj(archive_data, ('ivsRecords', ..., 'archiveUrl', {url_or_none}, any))
        return {
            'id': video_id,
            'formats': self._extract_m3u8_formats(m3u8_url, video_id),
            'age_limit': 18,
            'live_status': 'was_live',
            **traverse_obj(archive_data, {
                'title': ('title', {str}),
                'description': ('description', {str}, {clean_html}),
                'thumbnail': ('thumbnailUrl', {url_or_none}),
                'timestamp': ('createdAt', {parse_iso8601}),
                'uploader': ('cast', 'agencySecret', 'name'),
                'uploader_id': ('cast', 'uuid', {str}),
                'duration': ('ivsRecords', ..., 'recordingDurationMs', {lambda x: int_or_none(x, scale=1000)}, any),
            }),
        }


class WithnyArchiveIE(WithnyBaseIE):
    _VALID_URL = r'https://www.withny.fun/user/archives/(?P<id>[\d\w\-]+)'
    _TESTS = [{
        'url': 'https://www.withny.fun/user/archives/463019c0-3c34-494e-a3b3-ea6546ee63ac',
        'info_dict': {
            'id': '463019c0-3c34-494e-a3b3-ea6546ee63ac',
            'ext': 'mp4',
            'title': 'md5:d15ae047797fba83617af139aa0eeca2',
            'description': 'md5:91d282a920eda4dc84184e16a6957f26',
            'uploader': '逢瀬ふらち',
            'uploader_id': '80e30ffe-19e2-40d1-be16-2ed7dfe5c131',
            'duration': 4032,
            'thumbnail': r're:https://.*',
            'timestamp': 1723990247,
            'upload_date': '20240818',
            'age_limit': 18,
            'live_status': 'was_live',
        },
    }]

    def _real_extract(self, url):
        video_id = self._match_id(url)

        archive_data = self._download_webpage_nuxt(url, video_id, 'You need to login to access archive')['archive']
        return self._parse_archive_data(archive_data, video_id)


class WithnyUserArchivesIE(WithnyBaseIE):
    _VALID_URL = r'https://www.withny.fun/user/(?P<id>archives)/?(?:[?#]|$)'
    _TESTS = [{
        'url': 'https://www.withny.fun/user/archives',
        'info_dict': {
            'id': 'archives',
        },
        'playlist_mincount': 1,
    }]

    def _real_extract(self, url):
        archives_data = self._download_webpage_nuxt(url, 'archive', 'You need to login to access archives')

        def _entries():
            for archive_data in archives_data['archives']:
                yield {
                    **self._parse_archive_data(archive_data, archive_data.get('uuid')),
                    'extractor_key': WithnyArchiveIE.ie_key(),
                    'extractor': WithnyArchiveIE.IE_NAME,
                }
        return self.playlist_result(_entries(), 'archives')


class WithnyLiveChannelIE(WithnyBaseIE):
    _VALID_URL = r'https://www.withny.fun/channels/(?P<id>[\d\w]+)'
    _TESTS = [{
        'url': 'https://www.withny.fun/channels/Lunahlive',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        channel_id = self._match_id(url)

        streams = traverse_obj(self._download_json(
            'https://www.withny.fun/api/streams', channel_id, note='Downloading stream info',
            headers={'Referer': url}, query={'username': channel_id}),
            lambda _, v: v.get('actualStartedAt') or v.get('startedAt'))
        if not streams:
            raise UserNotLive
        stream = streams[0]
        stream_id = stream['uuid']
        if stream.get('streamingType') == 'withny':
            return self.url_result(f'https://www.withny.fun/room/user?id={stream_id}', WithnyLiveRoomIE, stream_id)

        m3u8_url = self._download_json(
            f'https://www.withny.fun/api/streams/{stream_id}/playback-url', channel_id, note='Downloading stream url',
            headers={'Referer': url, 'Authorization': self.bearer_token})

        m3u8_headers = {'Referer': 'https://www.withny.fun/', 'Origin': 'https://www.withny.fun'}
        formats = self._extract_m3u8_formats(m3u8_url, channel_id, headers=m3u8_headers)

        return {
            'id': stream_id,
            'formats': formats,
            'age_limit': 18,
            'live_status': 'is_live',
            **traverse_obj(stream, {
                'title': ('title', {str}),
                'description': ('about', {str}, {clean_html}),
                'timestamp': ('startedAt', {parse_iso8601}),
                'thumbnail': ('thumbnailUrl', {url_or_none}),
                'uploader': ('cast', 'agencySecret', 'name', {str}),
                'uploader_id': ('cast', 'uuid', {str}),
            }),
            'http_headers': m3u8_headers,
        }


class WithnyLiveRoomIE(WithnyBaseIE):
    _VALID_URL = r'https://www.withny.fun/room/user\?id=(?P<id>[\w\d\-]+)'
    _TESTS = [{
        'url': 'https://www.withny.fun/room/user?id=223c9cf3-725d-486b-bc89-8a08b34ed771',
        'info_dict': {
            'id': '223c9cf3-725d-486b-bc89-8a08b34ed771',
            'ext': 'mp4',
            'title': r're:久々交尾【ASMR】.*',
            'description': 'md5:41a23eac532d7eb73c8e6a3157e8495f',
            'uploader': '猫乃らて',
            'uploader_id': '6b144210-d232-447e-9845-d9800ea25084',
            'timestamp': 1724684595,
            'upload_date': '20240826',
            'thumbnail': r're:https://.*',
            'age_limit': 18,
            'live_status': 'is_live',
        },
        'skip': 'live',
    }]

    def _real_extract(self, url):
        room_id = self._match_id(url)

        room_info = self._download_json(
            f'https://www.withny.fun/api/rooms/{room_id}', room_id, note='Downloading stream info',
            headers={'Referer': url, 'Authorization': self.bearer_token})

        m3u8_url = room_info.get('ivsPlaybackUrl')
        if not m3u8_url:
            raise ExtractorError('Unable to get live stream url')
        m3u8_headers = {'Referer': 'https://www.withny.fun/', 'Origin': 'https://www.withny.fun'}
        formats = self._extract_m3u8_formats(m3u8_url, room_id, headers=m3u8_headers)

        return {
            'id': room_id,
            'formats': formats,
            'age_limit': 18,
            'live_status': 'is_live',
            **traverse_obj(room_info, {
                'title': ('title', {str}),
                'description': ('about', {str}, {clean_html}),
                'timestamp': ('actualStartedAt', {parse_iso8601}),
                'thumbnail': ('thumbnailUrl', {url_or_none}),
                'uploader': ('owner', 'name', {str}),
                'uploader_id': ('owner', 'uuid', {str}),
            }),
            'http_headers': m3u8_headers,
        }
