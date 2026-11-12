"""Local WeChat image decoding. Format/key derivation informed by wechatauto-replica
(Apache-2.0), see NOTICE. Reuses upstream read-only Windows memory access.
"""
import collections
import contextlib
import hashlib
import io
import json
import re
import struct
import subprocess
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def image_header(data):
    return data.startswith((b'\xff\xd8\xff',b'\x89PNG',b'GIF8',b'BM',b'wxgf')) or (data[:4]==b'RIFF' and data[8:12]==b'WEBP')


def decode_dat(data, aes_key=None, xor_key=None):
    if image_header(data):
        return data
    if data[:6] in (bytes.fromhex('070856320807'),bytes.fromhex('070856310807')):
        key=b'cfcd208495d565ef' if data[3]==49 else aes_key
        if not key:
            raise ValueError('尚未取得此账户图片密钥。')
        aes_size,xor_size=struct.unpack_from('<II',data,6)
        size=(aes_size//16+1)*16
        clear=unpad(AES.new(key,AES.MODE_ECB).decrypt(data[15:15+size]),16)
        end=len(data)-xor_size
        if xor_size and xor_key is None:
            raise ValueError('尚未取得图片尾部解码参数。')
        return clear+data[15+size:end]+bytes(b^(xor_key or 0) for b in data[end:])
    for magic in (b'\xff\xd8\xff',b'\x89PNG',b'GIF8',b'BM'):
        key=data[0]^magic[0]
        if bytes(b^key for b in data[:len(magic)])==magic:
            return bytes(b^key for b in data)
    raise ValueError('此图片的本地格式尚不能解码。')


def find_image_keys(root, account_dir):
    cache=root/'image-keys.json'
    account=account_dir.name
    saved=json.loads(cache.read_text('utf-8')) if cache.exists() else {}
    probes=sorted((account_dir/'msg'/'attach').glob('*/*/Img/*_t.dat'),key=lambda p:p.stat().st_mtime,reverse=True)[:20]
    probe=None; tails=collections.Counter()
    for p in probes:
        with p.open('rb') as f:
            head=f.read(31)
            f.seek(-2,2);tail=f.read(2)
        if head[:6]==bytes.fromhex('070856320807'):
            probe=probe or head[15:31]
            if tail[0]^0xff==tail[1]^0xd9:tails[tail[0]^0xff]+=1
    if probe is None:
        raise ValueError('本地未找到可验证的新版图片。')
    def valid(key):
        return len(key)==16 and image_header(AES.new(key,AES.MODE_ECB).decrypt(probe))
    if account in saved and valid(bytes.fromhex(saved[account]['aes'])):
        return bytes.fromhex(saved[account]['aes']),saved[account]['xor']
    from wechat_cli.keys.scanner_windows import _get_pids,_enum_regions,_read_mem,kernel32
    with contextlib.redirect_stdout(io.StringIO()):
        pids=_get_pids()
    wxid=account.rsplit('_',1)[0]
    found=None
    # 4.1.15 global_config stores the account-specific derivation value.
    for pid,_ in pids:
        h=kernel32.OpenProcess(0x0010|0x0400,False,pid)
        if not h:continue
        try:
            for base,size in _enum_regions(h):
                for offset in range(0,size,4*1024*1024):
                    data=_read_mem(h,base+offset,min(4*1024*1024+64,size-offset))
                    if not data:continue
                    for match in re.finditer(b'global_config\x00',data):
                        address=base+offset+match.start()
                        for back in (0x138,0x130):
                            first=_read_mem(h,address+16-back,8)
                            second=_read_mem(h,struct.unpack('<Q',first)[0]+0x68,8) if first else None
                            code=_read_mem(h,struct.unpack('<Q',second)[0]+0x40,4) if second else None
                            if code:
                                number=struct.unpack('<I',code)[0]
                                key=hashlib.md5((str(number)+wxid).encode()).hexdigest()[:16].encode()
                                if valid(key):found=(key,number&255);break
                        if found:break
                    if found:break
                if found:break
        finally:
            kernel32.CloseHandle(h)
        if found:break
    if not found:
        seen=set()
        for pid,_ in pids:
            h=kernel32.OpenProcess(0x0010|0x0400,False,pid)
            if not h:continue
            try:
                for base,size in _enum_regions(h):
                    for offset in range(0,size,4*1024*1024):
                        data=_read_mem(h,base+offset,min(4*1024*1024+64,size-offset)) or b''
                        for match in re.finditer(rb'(?<![a-zA-Z0-9])([a-zA-Z0-9]{16}|[a-zA-Z0-9]{32})(?![a-zA-Z0-9])',data):
                            key=match[1][:16]
                            if key in seen:continue
                            seen.add(key)
                            if valid(key):found=(key,tails.most_common(1)[0][0] if tails else None);break
                        if found:break
                    if found:break
            finally:
                kernel32.CloseHandle(h)
            if found:break
    if not found:
        raise RuntimeError('未取得图片密钥，请在当前微信中打开一张图片后重试读取。')
    saved[account]={'aes':found[0].hex(),'xor':found[1]}
    cache.write_text(json.dumps(saved),'utf-8')
    return found


def normalized_image(data, destination):
    from PIL import Image
    if data[:4]==b'wxgf':
        # The wxgf container carries an Annex-B HEVC bitstream.
        import imageio_ffmpeg
        start=next((i for i in range(4,len(data)-5) if data[i:i+4]==b'\x00\x00\x00\x01' and ((data[i+4]>>1)&63) in (32,33)),None)
        if start is None:raise ValueError('未能定位 wxgf 图像码流。')
        result=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-f','hevc','-i','pipe:0','-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],input=data[start:],capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise ValueError('wxgf 图像解码失败。')
        data=result.stdout
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        im.convert('RGB').save(destination,'PNG')
    return destination
