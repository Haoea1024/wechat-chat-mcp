"""Return exact attachment text and images to the current client, without a model API."""
import base64
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

from .archive import verified_backup
from .documents import read_document
from .images import decode_dat, find_image_keys, normalized_image
from .knowledge import digest, message_digest


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def delivery_image(path):
    """Bound transport size; keep full resolution separately and disclose resizing."""
    from PIL import Image, ImageOps
    with Image.open(path) as original:
        img = ImageOps.exif_transpose(original).convert('RGB')
        original_size = list(img.size)
        img.thumbnail((2560, 2560))
        data = io.BytesIO()
        img.save(data, format='PNG')
        mime = 'image/png'
        if data.tell() > 2 * 1024 * 1024:
            data = io.BytesIO()
            img.save(data, format='JPEG', quality=90)
            mime = 'image/jpeg'
        while data.tell() > 2 * 1024 * 1024:
            img.thumbnail((max(1, img.width * 3 // 4), max(1, img.height * 3 // 4)))
            data = io.BytesIO()
            img.save(data, format='JPEG', quality=90)
            mime = 'image/jpeg'
        target = Path(path).with_name(Path(path).stem + '.delivery' + ('.png' if mime == 'image/png' else '.jpg'))
        target.write_bytes(data.getvalue())
        return {'path': str(target), 'original_path': str(path), 'mime_type': mime,
                'original_size': original_size, 'delivered_size': list(img.size),
                'resized': original_size != list(img.size), 'sha256': file_digest(target)}


def read_for_client(reader, message, start, member, visual_offset):
    mid = message['id']
    result = {'message_id': mid, 'kind': message['kind'], 'member': member, 'text': '',
              'detail': {}, 'images': []}
    try:
        loc = json.loads(message.get('locator') or '{}')
        folder = reader.root / 'media' / str(mid) / message_digest(message)[:16] / 'client'
        folder.mkdir(parents=True, exist_ok=True)
        try:
            backup = verified_backup(reader.store, message)
        except FileNotFoundError as exc:
            backup = None
            result['detail']['backup_note'] = str(exc)
        if not backup and loc['account'] != reader.source.account:
            raise ValueError('当前连接账户与这条消息的账户不同。')
        images = []
        def collect(path):
            images.append(Path(path))
            return f'[附图 {len(images)}，交由当前客户端识读；此标记不是图片正文]'

        kind = loc['type'] & 0xffffffff
        if kind == 3:
            if start or member:
                raise ValueError('图片不使用 start/member；附图分页使用 visual_offset。')
            if backup:
                path = Path(backup['path'])
                quality = backup['quality']
            else:
                original, quality = reader._image_path(loc)
                data = original.read_bytes()
                keys = find_image_keys(reader.root, Path(reader.source.db_dir).parent) if data[:6] == bytes.fromhex('070856320807') else (None, None)
                path = normalized_image(decode_dat(data, *keys), folder / 'image.png')
            images.append(path)
            document = {'start': 0, 'total_units': 1, 'next_start': None, 'unit': 'image', 'parts': []}
            result['detail']['quality'] = quality
        else:
            path = Path(backup['path']) if backup else reader._file_path(message, loc)
            source_hash = file_digest(path)
            # Separate each source version/member/page to avoid one page overwriting
            # another page's evidence artifacts.
            folder = folder / source_hash[:16] / (hashlib.sha256(member.encode()).hexdigest()[:16] if member else 'document') / str(start)
            folder.mkdir(parents=True, exist_ok=True)
            target = path
            if member:
                with zipfile.ZipFile(path) as archive:
                    info = archive.getinfo(member)
                    if info.file_size > 100 * 1024 * 1024:
                        raise ValueError('此压缩包成员超过 100 MB，请先单独提取后处理。')
                    blob = archive.read(member)
                target = folder / ('member' + Path(member).suffix)
                target.write_bytes(blob)
            document = read_document(target, folder, collect, start, count=1)
            if file_digest(path) != source_hash:
                raise ValueError('读取期间附件发生变化，请重试。')
        if visual_offset > len(images) or (images and visual_offset == len(images)):
            raise ValueError('visual_offset 超出当前页的附图范围。')
        selected = images[visual_offset:visual_offset + 2]
        result['images'] = [dict(index=visual_offset+i+1, **delivery_image(p)) for i, p in enumerate(selected)]
        result['path'] = str(path)
        result['text'] = '\n\n'.join(p['text'] for p in document['parts'])
        listing = kind == 49 and not member and path.suffix.lower() == '.zip'
        result['status'] = 'listed' if listing else 'prepared' if images else 'partial' if document['next_start'] is not None else 'ready'
        result['detail'].update({k: v for k, v in document.items() if k != 'parts'})
        result['detail'].update(filename=path.name, archive_members_only=listing,
                                visual_offset=visual_offset, total_images=len(images),
                                next_visual_offset=visual_offset+len(selected) if visual_offset+len(selected)<len(images) else None,
                                method='文本本地提取；附图由当前客户端识读，无需 Codex CLI。',
                                note='prepared 只表示图片已准备。必须实际看到图片才能引用图中内容；未展示时用当前客户端读图工具打开 original_path。缩小图看不清时补读原图，不猜字。先读完当前页附图，再按 next_start 继续。')
        # Store prepared state for local preview/backup, never pretend it is OCR.
        with reader.store.connect() as db:
            current = db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
            if current is None or message_digest(dict(current)) != message_digest(message):
                raise ValueError('读取期间消息发生变化，请重试。')
            db.execute('INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?,?)',
                       (mid, result['status'], message['kind'], str(path), result['text'],
                        json.dumps(result['detail'], ensure_ascii=False), int(time.time())))
        if not listing and (result['text'] or selected):
            files = {str(path): file_digest(path)}
            files.update({i['path']: i['sha256'] for i in result['images']})
            body = dict(message_id=mid, message_digest=message_digest(message),
                        coverage={'kind': 'client_content', 'member': member, 'indices': [start],
                                  'image_indices': [i['index'] for i in result['images']],
                                  'total_units': document['total_units'],
                                  'note': '仅凭据所列文本页及附图内容已提供；不证明客户端已成功识读，不代表整份文件读完。'},
                        files=files, content_digest=digest({'text': result['text'], 'files': files}))
            token = digest(body)
            with reader.store.connect() as db:
                db.execute('INSERT OR IGNORE INTO attachment_receipts VALUES (?,?,?)',
                           (token, mid, json.dumps(body, ensure_ascii=False)))
            result['attachment_receipt'] = dict(token=token, **body)
    except FileNotFoundError as exc:
        result.update(status='missing', images=[])
        result['detail']['error'] = str(exc)
    except Exception as exc:
        result.update(status='error', images=[])
        result['detail']['error'] = str(exc)
    return result


def tool_result(result):
    from mcp.types import CallToolResult, ImageContent, TextContent
    content = [TextContent(type='text', text=json.dumps(result, ensure_ascii=False))]
    for picture in result.get('images', []):
        data = Path(picture['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != picture['sha256']:
            raise ValueError('附图在返回前发生变化，请重试。')
        content.append(ImageContent(type='image', data=base64.b64encode(data).decode(), mimeType=picture['mime_type']))
    return CallToolResult(content=content, structuredContent=result, isError=result.get('status') in {'error','missing'})
