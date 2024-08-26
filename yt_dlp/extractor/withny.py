import time
import urllib.parse

from .common import InfoExtractor
from ..utils import (
    UserNotLive,
    clean_html,
    int_or_none,
    parse_iso8601,
    traverse_obj,
    try_get,
    url_or_none,
)


class WithnyBaseIE(InfoExtractor):
    def _login_hint(self, *_, **__):
        return 'Use --cookies for the authentication. See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass cookies'

    def _get_cookie(self, key, transform=lambda x: x):
        return try_get(self._get_cookies('https://www.withny.fun/').get(key), lambda x: transform(x.value))

    def _update_token(self):
        expire = self._get_cookie('auth._token_expiration.local', int)
        refresh_token = self._get_cookie('auth._refresh_token.local')
        if expire and refresh_token:
            if time.time() * 1000 > expire:
                self._download_json(
                    'https://www.withny.fun/api/auth/token', 'token', note='refreshing token',
                    data=f'{"refreshToken":"{refresh_token}"}'.encode(),
                    headers={
                        'content-type': 'application/json',
                        'authorization': self._get_cookie('auth._token.local', urllib.parse.unquote),
                        'referer': 'https://www.withny.fun/',
                        'origin': 'https://www.withny.fun',
                    })

    @property
    def bearer_token(self):
        self._update_token()
        return self._get_cookie('auth._token.local', urllib.parse.unquote)

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


class WithnyLiveIE(WithnyBaseIE):
    _VALID_URL = r'https://www.withny.fun/channels/(?P<id>[\d\w]+)'

    def _real_extract(self, url):
        channel_id = self._match_id(url)

        streams = self._download_json('https://www.withny.fun/api/streams', channel_id,
                                      headers={'referer': url}, query={'username': channel_id})
        if not streams:
            raise UserNotLive
        stream = streams[0]
        stream_id = stream['uuid']
        m3u8_url = self._download_json(
            f'https://www.withny.fun/api/streams/{stream_id}/playback-url', channel_id,
            headers={'referer': url, 'Authorization': self.bearer_token})

        m3u8_headers = {'referer': 'https://www.withny.fun/', 'origin': 'https://www.withny.fun'}
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
