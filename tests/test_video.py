import asyncio
import base64
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from wechat_assistant.archive import copy_attachment
from wechat_assistant.client_media import tool_result
from wechat_assistant.knowledge import receipt_current, save_brief
from wechat_assistant.media import MediaReader, video_identifiers
from wechat_assistant.retrieval import attachments, is_attachment
from wechat_assistant.store import Store
from wechat_assistant.video import probe_video, read_video
from wechat_assistant.work import configure_project


def _pb_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_bytes(field, data):
    return _pb_varint((field << 3) | 2) + _pb_varint(len(data)) + data


def _make_video(target, duration=3, color='blue', audio=True):
    import imageio_ffmpeg
    args = [
        imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-y',
        '-f', 'lavfi', '-i', f'color=c={color}:s=96x64:r=2:d={duration}',
    ]
    if audio:
        args += ['-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=16000:duration={duration}']
    args += ['-c:v', 'mpeg4', '-pix_fmt', 'yuv420p']
    if audio:
        args += ['-c:a', 'aac', '-shortest']
    args.append(str(target))
    proc = subprocess.run(args, capture_output=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode('utf-8', errors='replace'))
    return Path(target)


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.now = int(time.time())
        self.reader = MediaReader(self.root, {'provider': 'client'}, self.store)

    def _add_video_message(self, raw='<msg><videomsg playlength="3" /></msg>', locator=None):
        mid = self.store.status()['total'] + 1
        loc = locator or {'account': 'test', 'chat': 'group1@chatroom', 'shard': 'message/message_0.db',
                          'local_id': mid, 'server_id': 1000 + mid, 'time': self.now, 'type': 43}
        self.store.ingest('test', [dict(
            uid=str(mid), chat_id='test:group1@chatroom', chat_name='视频测试群',
            sender='测试同事', ts=self.now, kind='视频', text='[视频]', raw=raw,
            locator=json.dumps(loc),
        )], mid, self.now)
        return mid

    def _fake_whisper(self, text='这是视频里的测试语音'):
        def transcribe(samples, **kw):
            return ([SimpleNamespace(start=0.5, end=2.0, text=text)], SimpleNamespace(duration=2.5))
        return SimpleNamespace(transcribe=transcribe)

    def test_video_message_is_listed_as_attachment_and_probes_streams(self):
        mid = self._add_video_message()
        row = self.store.evidence([mid])[0]
        self.assertTrue(is_attachment(row))
        listed = attachments(self.store)
        self.assertEqual([mid], [m['id'] for m in listed['messages']])

        mp4_audio = _make_video(self.root / 'with_audio.mp4', duration=3, color='green', audio=True)
        meta = probe_video(mp4_audio)
        self.assertTrue(meta['has_video'])
        self.assertTrue(meta['has_audio'])
        self.assertEqual((96, 64), (meta['width'], meta['height']))
        self.assertGreaterEqual(meta['duration'], 2.5)

        mp4_silent = _make_video(self.root / 'silent.mp4', duration=2, color='red', audio=False)
        meta_silent = probe_video(mp4_silent)
        self.assertTrue(meta_silent['has_video'])
        self.assertFalse(meta_silent['has_audio'])

    def test_client_mode_reads_segmented_video_with_audio_and_frames_and_preserves_receipts(self):
        # 35s video -> 2 segments (0..30s, 30..35s)
        mp4 = _make_video(self.root / 'demo.mp4', duration=35, color='green', audio=True)
        mid = self._add_video_message()
        copy_attachment(self.store, self.store.evidence([mid])[0], mp4, 'original_video')

        with patch('wechat_assistant.media.speech_model', return_value=self._fake_whisper('联调窗口改为周四')), \
             patch('wechat_assistant.model.Model.call', side_effect=AssertionError('不应调用外部模型')):
            first = self.reader.read(mid, start=0)
            second = self.reader.read(mid, start=1)

        self.assertEqual('prepared', first['status'])
        self.assertIn('[0.5–2.0s] 联调窗口改为周四', first['text'])
        self.assertEqual(2, first['detail']['total_units'])
        self.assertEqual(1, first['detail']['next_start'])
        self.assertEqual(2, len(first['images']))
        self.assertTrue(first['detail']['video_downloaded'])
        self.assertTrue(Path(first['detail']['video_path']).is_file())

        # MCP CallToolResult carries TextContent + 2 ImageContent frames
        protocol = tool_result(first)
        self.assertFalse(protocol.isError)
        self.assertEqual(['text', 'image', 'image'], [c.type for c in protocol.content])
        with Image.open(io.BytesIO(base64.b64decode(protocol.content[1].data))) as img:
            self.assertEqual((96, 64), img.size)

        # Second segment shifts transcript timestamps by +30.0s
        self.assertEqual('prepared', second['status'])
        self.assertIn('[30.5–32.0s] 联调窗口改为周四', second['text'])
        self.assertIsNone(second['detail']['next_start'])

        # First segment's receipt survives after reading second segment
        tok1 = first['attachment_receipt']['token']
        tok2 = second['attachment_receipt']['token']
        self.assertIsNotNone(receipt_current(self.store, tok1))
        self.assertIsNotNone(receipt_current(self.store, tok2))
        configure_project(self.store, '视频项目')
        saved = save_brief(
            self.store, '视频项目', 0,
            [dict(key='video_note', section='status', basis='reported', text='视频确认联调改周四', attachment_tokens=[tok1, tok2])],
            [], '已读取虚构测试视频两段'
        )
        self.assertTrue(saved['saved'])

        # Out-of-range segment returns error cleanly
        out = self.reader.read(mid, start=2)
        self.assertEqual('error', out['status'])

    def test_silent_video_and_missing_whisper_still_deliver_frames_honestly(self):
        silent_mp4 = _make_video(self.root / 'silent.mp4', duration=2, color='red', audio=False)
        mid1 = self._add_video_message()
        copy_attachment(self.store, self.store.evidence([mid1])[0], silent_mp4, 'local_video')
        with patch('wechat_assistant.media.speech_model', side_effect=AssertionError('无音轨不应调用 Whisper')):
            res1 = self.reader.read(mid1)
        self.assertEqual('prepared', res1['status'])
        self.assertIn('不含音轨', res1['text'])
        self.assertGreaterEqual(len(res1['images']), 1)

        audio_mp4 = _make_video(self.root / 'audio.mp4', duration=2, color='blue', audio=True)
        mid2 = self._add_video_message()
        copy_attachment(self.store, self.store.evidence([mid2])[0], audio_mp4, 'local_video')
        with patch('wechat_assistant.media.speech_model', side_effect=RuntimeError('本地语音模型未就绪')):
            res2 = self.reader.read(mid2)
        self.assertEqual('prepared', res2['status'])
        self.assertIn('语音转写未完成', res2['text'])
        self.assertIn('audio_error', res2['detail'])
        self.assertGreaterEqual(len(res2['images']), 1)

    def test_wechat4_video_lookup_packed_info_hardlink_thumbnail_and_backup(self):
        account_dir = self.root / 'wx_account'
        db_dir = account_dir / 'db_storage'
        db_dir.mkdir(parents=True)
        month = datetime.fromtimestamp(self.now).strftime('%Y-%m')
        video_dir = account_dir / 'msg' / 'video' / month
        video_dir.mkdir(parents=True)

        # Build synthetic message_resource.db, hardlink.db, and message_0.db
        res_db = self.root / 'message_resource.db'
        with closing(sqlite3.connect(res_db)) as db:
            db.execute('CREATE TABLE ChatName2Id (user_name TEXT)')
            db.execute('CREATE TABLE MessageResourceInfo (chat_id INT, message_local_id INT, message_create_time INT, message_local_type INT, message_svr_id INT, packed_info BLOB)')
            db.execute("INSERT INTO ChatName2Id VALUES ('group1@chatroom')")
            vid1 = 'a' * 32
            packed1 = _pb_bytes(2, _pb_bytes(1, vid1.encode()))
            db.execute('INSERT INTO MessageResourceInfo VALUES (1, 1, ?, 43, 1001, ?)', (self.now, packed1))
            vid_thumb = 'c' * 32
            packed_thumb = _pb_bytes(2, _pb_bytes(1, vid_thumb.encode()))
            db.execute('INSERT INTO MessageResourceInfo VALUES (1, 3, ?, 43, 1003, ?)', (self.now, packed_thumb))
            db.commit()

        msg_db = self.root / 'message_0.db'
        table = 'Msg_' + hashlib.md5(b'group1@chatroom').hexdigest()
        vid2 = 'b' * 32
        packed2 = _pb_bytes(4, _pb_bytes(8, vid2.encode()))
        self.assertEqual([vid2], video_identifiers(packed2))
        with closing(sqlite3.connect(msg_db)) as db:
            db.execute(f'CREATE TABLE [{table}] (local_id INT, create_time INT, server_id INT, packed_info_data BLOB)')
            db.execute(f'INSERT INTO [{table}] VALUES (2, ?, 1002, ?)', (self.now, packed2))
            db.commit()

        hardlink_db = self.root / 'hardlink.db'
        with closing(sqlite3.connect(hardlink_db)) as db:
            db.execute('CREATE TABLE dir2id (username TEXT)')
            db.execute('CREATE TABLE video_hardlink_info_v4 (md5 TEXT, file_name TEXT, dir1 INT, dir2 INT)')
            db.execute('INSERT INTO dir2id VALUES (?)', (month,))
            # Create files on disk
            mp4_raw = _make_video(video_dir / f'{vid1}_raw.mp4', duration=2, color='green', audio=False)
            _make_video(video_dir / f'{vid1}.mp4', duration=2, color='red', audio=False)
            mp4_2 = _make_video(video_dir / f'{vid2}.mp4', duration=2, color='blue', audio=False)
            Image.new('RGB', (40, 30), 'yellow').save(video_dir / f'{vid_thumb}.jpg')
            mp4_hl = _make_video(video_dir / 'hardlinked_clip.mp4', duration=2, color='white', audio=False)
            hl_md5 = hashlib.md5(mp4_hl.read_bytes()).hexdigest()
            db.execute('INSERT INTO video_hardlink_info_v4 VALUES (?, ?, 1, NULL)', (hl_md5, 'hardlinked_clip.mp4'))
            db.commit()

        cache_map = {
            'message/message_resource.db': str(res_db),
            'message/message_0.db': str(msg_db),
            'hardlink/hardlink.db': str(hardlink_db),
        }
        fake_source = SimpleNamespace(
            account='test',
            db_dir=str(db_dir),
            cache=SimpleNamespace(get=lambda rel: cache_map.get(rel)),
        )
        self.reader._source = fake_source

        # 1. MessageResourceInfo packed_info prefers _raw.mp4 -> original_video, and backup survives original deletion
        mid1 = self._add_video_message(raw=f'<msg><videomsg rawmd5="{hashlib.md5(mp4_raw.read_bytes()).hexdigest()}" /></msg>')
        path1, q1 = self.reader._video_path(self.store.evidence([mid1])[0], json.loads(self.store.evidence([mid1])[0]['locator']))
        self.assertEqual(mp4_raw.resolve(), path1)
        self.assertEqual('original_video', q1)
        backup = self.reader.backup(mid1)
        self.assertTrue(backup['verified'])
        self.assertEqual('original_video', backup['quality'])
        mp4_raw.unlink()
        self.reader._source = None
        with patch('wechat_assistant.media.WeChatSource', side_effect=AssertionError('有备份不应连接微信')):
            res_from_backup = self.reader.read(mid1)
        self.assertEqual('prepared', res_from_backup['status'])

        # 2. Msg.packed_info_data (videoInfo.filename) lookup
        self.reader._source = fake_source
        mid2 = self._add_video_message()
        path2, q2 = self.reader._video_path(self.store.evidence([mid2])[0], json.loads(self.store.evidence([mid2])[0]['locator']))
        self.assertEqual(mp4_2.resolve(), path2)
        self.assertEqual('local_video', q2)

        # 3. Thumbnail-only fallback when mp4 is not downloaded
        mid3 = self._add_video_message()
        res_thumb = self.reader.read(mid3)
        self.assertEqual('prepared', res_thumb['status'])
        self.assertEqual('thumbnail', res_thumb['detail']['quality'])
        self.assertFalse(res_thumb['detail']['video_downloaded'])
        self.assertIn('仅本地封面缩略图', res_thumb['text'])
        self.assertEqual('error', self.reader.read(mid3, start=1)['status'])

        # 4. Hardlink video_hardlink_info_v4 lookup by XML md5
        mid4 = self._add_video_message(raw=f'<msg><videomsg md5="{hl_md5}" /></msg>')
        path4, q4 = self.reader._video_path(self.store.evidence([mid4])[0], json.loads(self.store.evidence([mid4])[0]['locator']))
        self.assertEqual(mp4_hl.resolve(), path4)
        self.assertEqual('local_video', q4)

        # 5. Missing video returns status=missing and no receipt
        mid5 = self._add_video_message(raw='<msg><videomsg md5="dddddddddddddddddddddddddddddddd" /></msg>')
        res_missing = self.reader.read(mid5)
        self.assertEqual('missing', res_missing['status'])
        self.assertNotIn('attachment_receipt', res_missing)

    def test_video_sent_as_file_and_zip_member_and_legacy_provider_paging(self):
        mp4 = _make_video(self.root / 'clip.mp4', duration=35, color='yellow', audio=False)
        # File attachment (type=49)
        mid_file = self.store.status()['total'] + 1
        self.store.ingest('test', [dict(
            uid=str(mid_file), chat_id='test:group1@chatroom', chat_name='视频测试群',
            sender='测试同事', ts=self.now, kind='链接/文件', text='[文件] clip.mp4',
            raw='<msg><appmsg><type>6</type><title>clip.mp4</title></appmsg></msg>',
            locator=json.dumps(dict(account='test', type=49)),
        )], mid_file, self.now)
        copy_attachment(self.store, self.store.evidence([mid_file])[0], mp4, 'original_file')
        res_file = self.reader.read(mid_file, start=0)
        self.assertEqual('prepared', res_file['status'])
        self.assertEqual(1, res_file['detail']['next_start'])
        self.assertIn('video_path', res_file['detail'])

        # Video inside ZIP archive
        zpath = self.root / 'bundle.zip'
        with zipfile.ZipFile(zpath, 'w') as zf:
            zf.write(mp4, 'demos/clip.mp4')
        mid_zip = self.store.status()['total'] + 1
        self.store.ingest('test', [dict(
            uid=str(mid_zip), chat_id='test:group1@chatroom', chat_name='视频测试群',
            sender='测试同事', ts=self.now, kind='链接/文件', text='[文件] bundle.zip',
            raw='<msg><appmsg><type>6</type><title>bundle.zip</title></appmsg></msg>',
            locator=json.dumps(dict(account='test', type=49)),
        )], mid_zip, self.now)
        copy_attachment(self.store, self.store.evidence([mid_zip])[0], zpath, 'original_file')
        res_zip = self.reader.read(mid_zip, member='demos/clip.mp4', start=0)
        self.assertEqual('prepared', res_zip['status'])
        self.assertEqual('demos/clip.mp4', res_zip['member'])

        # Legacy provider='codex' multi-segment paging (70s -> 3 segments, page_size=2)
        long_mp4 = _make_video(self.root / 'long.mp4', duration=70, color='green', audio=False)
        legacy_reader = MediaReader(self.root, {'provider': 'codex'}, self.store)
        mid_legacy = self._add_video_message()
        copy_attachment(self.store, self.store.evidence([mid_legacy])[0], long_mp4, 'local_video')
        with patch.object(legacy_reader, 'vision', return_value='画面正常'):
            p1 = legacy_reader.read(mid_legacy, start=0)
            self.assertEqual('partial', p1['status'])
            self.assertEqual(2, p1['detail']['next_start'])
            self.assertEqual([0, 1], p1['attachment_receipt']['coverage']['indices'])
            p2 = legacy_reader.read(mid_legacy, start=2)
            self.assertEqual('ready', p2['status'])
            self.assertIsNone(p2['detail']['next_start'])
            self.assertEqual([2], p2['attachment_receipt']['coverage']['indices'])

    def test_real_stdio_mcp_delivers_video_frames_and_transcript(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        mp4 = _make_video(self.root / 'stdio_clip.mp4', duration=2, color='green', audio=False)
        mid = self._add_video_message()
        copy_attachment(self.store, self.store.evidence([mid])[0], mp4, 'original_video')

        async def run():
            params = StdioServerParameters(command=sys.executable, args=['-m', 'wechat_assistant.cli', '--data-dir', str(self.root), 'mcp'])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool('read_attachment', {'message_id': mid})
                    self.assertFalse(result.isError, str(result))
                    self.assertEqual('text', result.content[0].type)
                    self.assertTrue(any(c.type == 'image' for c in result.content))
                    payload = json.loads(result.content[0].text)
                    self.assertEqual('prepared', payload['status'])
                    self.assertTrue(payload['detail']['video_downloaded'])
                    self.assertIn('attachment_receipt', payload)

        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
