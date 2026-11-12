"""Content-addressed local copies of exactly located attachments; originals are untouched."""
import hashlib
import os
import tempfile
import time
from pathlib import Path

from .knowledge import message_digest


def file_hash(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def verified_backup(store,message):
    rows=store.rows('SELECT * FROM attachment_backups WHERE message_id=? AND message_digest=? ORDER BY created DESC',
                    (message['id'],message_digest(message)))
    if not rows:return None
    root=(store.path.parent/'backups').resolve()
    for row in rows:
        path=Path(row['path']).resolve()
        if not path.is_relative_to(root):raise ValueError('备份位置超出助理备份目录。')
        if path.is_file() and file_hash(path)==row['sha256']:return row
    raise FileNotFoundError('已有备份缺失或校验失败；未将它当作有效原件，请重新备份。')


def copy_attachment(store,message,path,quality):
    path=Path(path).resolve()
    archive=(store.path.parent/'backups').resolve();archive.mkdir(parents=True,exist_ok=True)
    initial=path.stat();temporary=None
    try:
        with path.open('rb') as source,tempfile.NamedTemporaryFile(dir=archive,suffix='.tmp',delete=False) as output:
            temporary=Path(output.name);sha=hashlib.sha256()
            for block in iter(lambda:source.read(1024*1024),b''):
                sha.update(block);output.write(block)
        after=path.stat()
        if (initial.st_size,initial.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise ValueError('复制期间原文件发生变化，请重试。')
        checksum=sha.hexdigest()
        destination=(archive/checksum/path.name).resolve()
        if not destination.is_relative_to(archive):raise ValueError('备份目标超出助理备份目录。')
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists() and file_hash(destination)==checksum:
            temporary.unlink();temporary=None
        else:
            os.replace(temporary,destination);temporary=None
        if file_hash(destination)!=checksum:raise ValueError('备份校验失败。')
        record=dict(message_id=message['id'],message_digest=message_digest(message),sha256=checksum,
            path=str(destination),name=path.name,kind=message['kind'],quality=quality,size=initial.st_size,created=int(time.time()))
        with store.connect() as db:
            current=db.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone()
            if current is None or message_digest(dict(current))!=record['message_digest']:
                raise ValueError('备份期间消息发生变化，请重新定位附件。')
            db.execute('''INSERT OR REPLACE INTO attachment_backups VALUES
                (:message_id,:message_digest,:sha256,:path,:name,:kind,:quality,:size,:created)''',record)
        return record|dict(verified=True,note='已保存并校验本机副本，原文件未修改；同盘副本不能替代异地灾备。')
    finally:
        if temporary is not None and temporary.exists():temporary.unlink()
