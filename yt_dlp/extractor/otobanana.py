import re

from .common import InfoExtractor
from ..utils import (
    int_or_none,
    str_or_none,
    traverse_obj,
    url_or_none,
)

class OtobananaIE(InfoExtractor):
    _VALID_URL = r'https://otobanana.com/cast/(?P<id>[a-f0-9\-]+)'
    _TESTS = []
    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id)
        raw_string = self._search_regex(r'<script>window\.__NUXT__=.*?data:(\[[^]]+\])', webpage, 'raw_info', video_id)
        raw_string = re.sub(r':[abcd]\b', ':null', raw_string)
        raw_string = re.sub(r'([,{\[])([A-Za-z0-9_]+):', r'\1"\2":', raw_string)
        return {
            'id': video_id,
            'age_limit': 18,
            **traverse_obj(self._parse_json(raw_string, video_id), (0, 'cast', {
                'title': ('title', {str_or_none}),
                'url': ('audio_url', {url_or_none}),
                'description': ('text', {str_or_none}),
                'thumbnail': (('thumbnail_url', ('user', 'avatar_url')), {url_or_none}),
                'uploader': ('user', 'name', {str_or_none}),
                'uploader_id': ('user', 'username', {str_or_none}),
                'view_count': ('play_count', {int_or_none}),
                'like_count': ('like_count', {int_or_none}),
            }), get_all=False)
        }
