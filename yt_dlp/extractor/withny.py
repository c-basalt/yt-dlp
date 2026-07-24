import itertools
import json
import random
import re
import time

from .common import InfoExtractor
from ..networking import Request
from ..utils import (
    ExtractorError,
    UserNotLive,
    clean_html,
    int_or_none,
    jwt_decode_hs256,
    parse_iso8601,
    traverse_obj,
    url_or_none,
)


class WithnyBaseIE(InfoExtractor):
    def _download_webpage(self, url, video_id, login_msg='You need to login to access video', **kwargs):
        webpage, urlh = self._download_webpage_handle(url, video_id, **kwargs)
        if urlh.url.startswith('https://www.withny.fun/login'):
            self.raise_login_required(login_msg)
        return webpage

    def _search_next_seg(self, keyword, webpage, video_id):
        # _search_nextjs_v13_data() does not work properly, manually parse data instead
        return traverse_obj(re.findall(r'<script\b[^>]*>self\.__next_f\.push\((\[.+?\])\)</script>', webpage), (
            lambda _, v: rf'\"{keyword}\"' in v,
            {lambda x: self._parse_json(x, video_id)[1]}, {lambda x: x[x.find('['):]}, {json.loads},
        ))

    def _parse_archive(self, archive_data, video_id):
        if (record_count := len(archive_data.get('ivsRecords'))) != 1:
            self.report_warning(f'Expected single ivsRecords, got {record_count}')
        m3u8_url = traverse_obj(archive_data, ('ivsRecords', 0, 'archiveUrl', {url_or_none}))
        for name, value in archive_data['cookies'].items():
            self._set_cookie('.withny.fun', name, value, discard=True)
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
                'uploader': ('cast', 'user', 'name'),
                'uploader_id': ('cast', 'user', 'username', {str}),
                'duration': ('ivsRecords', 0, 'recordingDurationMs', {lambda x: int_or_none(x, scale=1000)}),
            }),
        }


class WithnyVideoIE(WithnyBaseIE):
    _VALID_URL = r'https://www.withny.fun/(?:user/)?archives/(?P<id>[\d\w\-]+)'
    _TESTS = [{
        'url': 'https://www.withny.fun/user/archives/463019c0-3c34-494e-a3b3-ea6546ee63ac',
        'info_dict': {
            'id': '463019c0-3c34-494e-a3b3-ea6546ee63ac',
            'ext': 'mp4',
            'title': 'md5:d15ae047797fba83617af139aa0eeca2',
            'description': 'md5:91d282a920eda4dc84184e16a6957f26',
            'uploader': '桜彗ふらち',
            'uploader_id': 'OuseFurachi',
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

        webpage = self._download_webpage(f'https://www.withny.fun/user/archives/{video_id}', video_id)
        archive_data = traverse_obj(self._search_next_seg('archiveData', webpage, video_id), (
            ..., ..., 'children', ..., 'archiveData', {dict}, any))
        if not archive_data:
            raise ExtractorError('Failed to find archive data')
        return self._parse_archive(archive_data, video_id)


class WithnyPurchaseListIE(WithnyBaseIE):
    _VALID_URL = r'https://www.withny.fun/user/(?P<id>archives)/?(?:[?#]|$)'
    _TESTS = [{
        'url': 'https://www.withny.fun/user/archives',
        'info_dict': {
            'id': 'archives',
        },
        'playlist_mincount': 1,
    }]

    def _real_extract(self, url):
        def _entries():
            page_size = 1
            for page in itertools.count(1):
                webpage = self._download_webpage(url, 'archive', query={'page': page})
                archives = traverse_obj(self._search_next_seg('initialArchives', webpage, f'page-{page}'), (
                    ..., ..., 'initialArchives', {dict}, any))
                if not archives.get('data'):
                    break
                for item in traverse_obj(archives, ('data', ..., {
                    'id': ('uuid', {str}),
                    'title': ('title', {str}),
                })):
                    yield self.url_result(f'https://www.withny.fun/user/archives/{item["id"]}', WithnyVideoIE, **item)
                page_size = max(page_size, len(archives['data']))
                if page * page_size >= archives['count']:
                    break
        return self.playlist_result(_entries(), 'archives')


class WithnyLiveIE(WithnyBaseIE):
    _VALID_URL = r'https://www.withny.fun/channels/(?P<id>[\d\w]+)'
    _TESTS = [{
        'url': 'https://www.withny.fun/channels/nekonametuna',
        'only_matching': True,
    }, {
        'url': 'https://www.withny.fun/channels/mikuru',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        user_id = self._match_id(url)

        webpage = self._download_webpage(url, user_id)
        channel_data = traverse_obj(self._search_next_seg('initialCast', webpage, user_id), (
            ..., ..., 'children', ..., ..., 'initialCast', {dict}, any))
        channel_id = channel_data['ivsChannel']['uuid']
        if (live_status := channel_data['ivsChannel']['state']) != 'live':
            if not self._downloader.params.get('wait_for_video'):
                raise UserNotLive(f'Channel is not live: {live_status}')

        token = traverse_obj(self._search_next_seg('accessToken', webpage, user_id), (
            ..., ..., 'children', ..., ..., 'children', ..., ..., 'session', 'accessToken', {str}, any))
        if not token or not (expiry := traverse_obj(token, ({jwt_decode_hs256}, 'exp', {int}))):
            self.raise_login_required()

        ws = self._request_webpage(Request(
            'wss://api.withny.fun/socket.io/', headers={'Origin': 'https://www.withny.fun'},
            query={'uuid': channel_id, 'token': token, 'passCode': 'undefined', 'EIO': 4, 'transport': 'websocket'}),
            user_id, note='Fetching stream info via WebSocket')
        ws.send('40/channels,{"sessionID":"%s"}' % ''.join(random.choices('0123456789abcdef', k=16)))
        while True:
            if isinstance(msg := ws.recv(), str):
                if expiry - time.time() < 300:  # we should get token valid for 24hr and heartbeat every 25s
                    raise UserNotLive
                if 'token is invalid' in msg or 'Forbidden' in msg:
                    self.raise_login_required(f'Invalid login info: {msg}')

                if msg.startswith('42/channels,["stream"'):
                    stream_data = json.loads(msg.split(',', maxsplit=1)[1])[1]
                    break
                elif msg == '2':
                    ws.send('3')  # heartbeat
                elif 'changeNumOfStandby' in msg:
                    if not self._downloader.params.get('wait_for_video'):
                        self.to_screen(f'{user_id}: channel is on standby')
                    else:
                        raise UserNotLive

        stream_id = stream_data['uuid']
        m3u8_url = self._download_json(f'https://www.withny.fun/api/streams/{stream_id}/playback-url', user_id,
                                       headers={'Authorization': f'Bearer {token}'})
        m3u8_headers = {'referer': 'https://www.withny.fun/', 'origin': 'https://www.withny.fun'}

        return {
            'id': stream_id,
            'formats': self._extract_m3u8_formats(m3u8_url, user_id, headers=m3u8_headers),
            'age_limit': 18,
            'live_status': 'is_live',
            'http_headers': m3u8_headers,
            **traverse_obj(stream_data, {
                'title': ('title', {str}),
                'description': ('about', {str}, {clean_html}),
                'timestamp': ('startedAt', {parse_iso8601}),
                'thumbnail': ('thumbnailUrl', {url_or_none}),
            }),
            **traverse_obj(channel_data, {
                'uploader': ('user', 'name', {str}),
                'uploader_id': ('user', 'username', {str}),
            }),
        }
