"""Bounded local video reading: ffmpeg metadata/frame extraction + local Whisper audio transcription."""
import json
import math
import re
import subprocess
from pathlib import Path


VIDEO_SUFFIXES = {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi', '.wmv', '.flv', '.3gp'}
SEGMENT_SECONDS = 30.0
_DURATION_RE = re.compile(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)')
_VIDEO_RE = re.compile(r'Stream\s+#\d+:\d+[^\r\n]*?:\s*Video:\s*([^\r\n]+)')
_AUDIO_RE = re.compile(r'Stream\s+#\d+:\d+[^\r\n]*?:\s*Audio:')
_DIM_RE = re.compile(r'(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)')


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError('缺少本地 ffmpeg 组件；请安装 .[media] 后重试。') from exc


def _run_ffmpeg(args, timeout=60):
    return subprocess.run(
        [_ffmpeg(), '-nostdin', *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )


def probe_video(path):
    """Inspect duration, resolution, and video/audio streams using bundled ffmpeg."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError('本地视频文件不存在。')
    proc = _run_ffmpeg(['-hide_banner', '-i', str(path)], timeout=30)
    output = (proc.stderr or b'').decode('utf-8', errors='replace')
    duration = None
    match = _DURATION_RE.search(output)
    if match:
        hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
        duration = round(hours * 3600 + minutes * 60 + seconds, 3)
    vmatch = _VIDEO_RE.search(output)
    has_video = bool(vmatch)
    width = height = None
    if vmatch:
        dmatch = _DIM_RE.search(vmatch.group(1))
        if dmatch:
            width, height = int(dmatch.group(1)), int(dmatch.group(2))
    has_audio = bool(_AUDIO_RE.search(output))
    if not has_video and not has_audio:
        raise ValueError('无法解析此视频文件，可能已损坏或格式不受支持。')
    return dict(duration=duration, width=width, height=height, has_video=has_video, has_audio=has_audio)


def _format_clock(seconds):
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


def segment_timestamps(seg_start, seg_end, duration):
    """Pick 1-2 representative frame timestamps within a bounded video segment."""
    if not duration or duration <= 0:
        return [0.0]
    seg_len = max(0.0, seg_end - seg_start)
    max_ts = max(0.0, duration - 0.1)
    if seg_len <= 1.5:
        return [round(min(max_ts, seg_start + seg_len * 0.3), 3)]
    first = min(max_ts, seg_start + seg_len * 0.25)
    second = min(max_ts, seg_start + seg_len * 0.75)
    if abs(second - first) < 0.2:
        return [round(first, 3)]
    return [round(first, 3), round(second, 3)]


def extract_frame(path, ts, target, force=False):
    target = Path(target)
    if not force and target.is_file() and target.stat().st_size > 0:
        return target
    target.unlink(missing_ok=True)
    for args in (
        ['-v', 'error', '-y', '-ss', f'{ts:.3f}', '-i', str(path), '-frames:v', '1', str(target)],
        ['-v', 'error', '-y', '-i', str(path), '-ss', f'{ts:.3f}', '-frames:v', '1', str(target)],
    ):
        _run_ffmpeg(args, timeout=30)
        if target.is_file() and target.stat().st_size > 0:
            return target
    target.unlink(missing_ok=True)
    return None


def extract_audio_wav(path, seg_start, seg_dur, wav_path, force=False):
    wav_path = Path(wav_path)
    if not force and wav_path.is_file() and wav_path.stat().st_size > 44:
        return wav_path
    wav_path.unlink(missing_ok=True)
    args = ['-v', 'error', '-y']
    if seg_start > 0:
        args += ['-ss', f'{seg_start:.3f}']
    if seg_dur is not None:
        args += ['-t', f'{seg_dur:.3f}']
    args += ['-i', str(path), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(wav_path)]
    _run_ffmpeg(args, timeout=60)
    if wav_path.is_file() and wav_path.stat().st_size > 44:
        return wav_path
    wav_path.unlink(missing_ok=True)
    return None


def read_video(path, folder, vision, transcribe=None, start=0, count=1, force=False, segment_seconds=SEGMENT_SECONDS):
    """Read bounded time segments of a local video: Whisper audio transcript + sampled frames."""
    path = Path(path)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    meta = probe_video(path)
    duration = meta['duration']
    total = max(1, math.ceil(duration / segment_seconds)) if duration and duration > 0 else 1
    if start < 0 or start >= total:
        raise ValueError(f'start 超出视频片段范围（共 {total} 段）。')
    parts = []
    frame_timestamps = []
    audio_paths = []
    audio_errors = []
    for i in range(start, min(start + count, total)):
        seg_start = i * segment_seconds
        seg_end = min(duration, (i + 1) * segment_seconds) if duration and duration > 0 else segment_seconds
        seg_dur = max(0.1, seg_end - seg_start) if duration and duration > 0 else None
        range_label = f'{seg_start:.1f}–{seg_end:.1f}s，总长 {duration:.1f}s' if duration and duration > 0 else '时长未知'
        res_label = f'，{meta["width"]}×{meta["height"]}' if meta['width'] and meta['height'] else ''
        lines = [f'视频片段 {i + 1}/{total}（{range_label}{res_label}）']

        if meta['has_audio']:
            if transcribe is not None:
                cache_file = folder / f'transcript-{i}.json'
                wav_target = folder / f'audio-{i}.wav'
                if not force and cache_file.is_file():
                    cached = json.loads(cache_file.read_text('utf-8'))
                    lines.append('[语音转写]\n' + cached['text'])
                    if wav_target.is_file():
                        audio_paths.append(str(wav_target))
                else:
                    wav = extract_audio_wav(path, seg_start, seg_dur, wav_target, force=force)
                    if wav is not None:
                        audio_paths.append(str(wav))
                        try:
                            transcript_text, _ = transcribe(wav, offset_sec=seg_start)
                            cache_file.write_text(json.dumps({'text': transcript_text}, ensure_ascii=False), 'utf-8')
                            lines.append('[语音转写]\n' + transcript_text)
                        except RuntimeError as exc:
                            audio_errors.append(str(exc))
                            lines.append(f'[语音转写未完成] {exc}')
                    else:
                        lines.append('[音轨] 当前片段未提取到有效音频。')
            else:
                lines.append('[音轨] 未启用本地语音转写。')
        else:
            lines.append('[音轨] 此视频不含音轨。')

        if meta['has_video']:
            timestamps = segment_timestamps(seg_start, seg_end, duration)
            extracted = 0
            for idx, ts in enumerate(timestamps, 1):
                frame_file = extract_frame(path, ts, folder / f'frame-{i}-{idx}.png', force=force)
                if frame_file is None and idx == 1 and ts > 0:
                    frame_file = extract_frame(path, 0.0, folder / f'frame-{i}-{idx}.png', force=force)
                    ts = 0.0
                if frame_file is not None:
                    extracted += 1
                    frame_timestamps.append(round(ts, 2))
                    lines.append(f'[关键画面 {_format_clock(ts)}]\n{vision(frame_file)}')
            if not extracted:
                lines.append('[画面] 当前片段未能抽取到有效视频帧。')

        parts.append(dict(index=i, text='\n'.join(lines)))

    result = dict(
        parts=parts,
        total_units=total,
        unit='视频片段（每段最多 30 秒，含音轨转写与抽帧）',
        start=start,
        next_start=start + len(parts) if start + len(parts) < total else None,
        page_size=count,
        duration=duration,
        width=meta['width'],
        height=meta['height'],
        has_audio=meta['has_audio'],
        has_video=meta['has_video'],
        segment_seconds=segment_seconds,
        frame_timestamps=frame_timestamps,
    )
    if audio_paths:
        result['audio_paths'] = audio_paths
    if audio_errors:
        result['audio_error'] = audio_errors[0]
    return result
