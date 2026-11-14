"""Prepare speech during installation; never download inside an MCP tool call."""
from functools import lru_cache


@lru_cache(maxsize=1)
def speech_model(folder, name):
    from faster_whisper import WhisperModel
    try:
        return WhisperModel(name, device='cpu', compute_type='int8', cpu_threads=4,
                            download_root=folder, local_files_only=True)
    except Exception as exc:
        raise RuntimeError('本地语音模型未就绪，请在相同资料目录运行 prepare-media 完成下载和测试；'
                           '本次没有在工具调用中下载模型。原始错误：' + str(exc)) from exc


def prepare(root, config):
    from faster_whisper.utils import download_model
    import numpy as np
    name = config.get('speech_model', 'small')
    try:
        download_model(name, cache_dir=str(root / 'models'), local_files_only=True)
    except Exception:
        download_model(name, cache_dir=str(root / 'models'))
    speech_model.cache_clear()
    model = speech_model(str(root / 'models'), name)
    # Force lazy inference, not just file presence. Silence is a runtime smoke test,
    # not a claim about transcription accuracy on actual messages.
    segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language='zh', beam_size=1,
                                   vad_filter=False)
    list(segments)
    return {'speech': 'ready', 'model': name, 'test': '本地模型加载及静音推理通过；真实语音准确度另行核对。'}


def readiness(root, config):
    try:
        from faster_whisper.utils import download_model
        path = download_model(config.get('speech_model', 'small'), cache_dir=str(root / 'models'),
                              local_files_only=True)
        return {'status': 'cached', 'path': path, 'note': '文件已缓存；prepare-media 才会执行加载和推理测试。'}
    except Exception:
        return {'status': 'not_ready', 'next_step': '运行 prepare-media；不会在读取语音时临时下载。'}
