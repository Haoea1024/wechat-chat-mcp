"""Keep the local page and its Python backend on the same revision."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .config import load


APP = 'wechat-project-assistant'


def fingerprint():
    package = Path(__file__).parent
    files = sorted([*package.glob('*.py'), *package.glob('static/*')])
    digest = hashlib.sha256()
    for path in files:
        if path.is_file():
            digest.update(path.relative_to(package).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def request(port, route='/', token=None, body=None):
    headers = {'X-Local-Token': token} if token else {}
    if body is not None:headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(f'http://127.0.0.1:{port}{route}',
                                 None if body is None else json.dumps(body).encode(), headers)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as response:
        return response.read()


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.5):return True
    except OSError:return False


def probe(port):
    if not listening(port):return None
    try:
        page = request(port).decode('utf-8')
        token = re.search(r'name="local-token" content="([^"]+)"', page)
        if not token:raise ValueError('该端口不是可识别的项目网页，未修改已有进程。')
        try:
            runtime = json.loads(request(port, '/api/runtime', token[1]))
            if runtime.get('app') != APP:raise ValueError('网页服务身份不匹配，未修改已有进程。')
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code not in {403,404}:raise
            runtime = None  # Old releases have no runtime endpoint.
        return {'token': token[1], 'runtime': runtime}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('网页后台未响应或正在退出；请稍后重试，不要另启重复后台。') from exc


def legacy_command_matches(command, root):
    tokens = [part.strip('"') for part in shlex.split(command or '', posix=False)]
    try:
        module = '-m' in tokens and tokens[tokens.index('-m')+1] == 'wechat_assistant.cli'
        executable = any(Path(token).name.lower() in {'wechat-assistant.exe','wechat-assistant'} for token in tokens[:2])
        return ((module or executable) and tokens.count('--data-dir') == 1
                and Path(tokens[tokens.index('--data-dir')+1]).resolve() == root.resolve()
                and any(action in tokens for action in ('serve','demo')))
    except (ValueError,IndexError):return False


def legacy_owned(root, port):
    """Verify the listener's command line (or its Windows Python wrapper).

    Never trust a stale PID file or assume an omitted --data-dir means default:
    the process could have inherited a different WECHAT_ASSISTANT_HOME.
    """
    if os.name != 'nt':return False
    try:
        script = f'''
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$owners = @(Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)
if ($owners.Count -ne 1) {{ exit 2 }}
$processNumber = $owners[0]
$chain = @()
for ($i=0; $i -lt 2 -and $processNumber -gt 0; $i++) {{
    $entry = Get-CimInstance Win32_Process -Filter "ProcessId = $processNumber"
    if (-not $entry) {{ break }}
    $chain += $entry | Select-Object ProcessId,ParentProcessId,CommandLine
    $processNumber = $entry.ParentProcessId
}}
ConvertTo-Json -InputObject $chain -Compress
'''
        output = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, text=True, encoding='utf-8', timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        chain = json.loads(output.stdout)
        return any(legacy_command_matches(row['CommandLine'],root) for row in chain)
    except (OSError, ValueError, IndexError, KeyError, subprocess.SubprocessError):
        return False


def ensure_web(root=None, demo=False, only_if_running=False, restart=False):
    root, config = load(root)
    port = 8767 if demo else int(config['port'])
    expected = fingerprint()
    current = probe(port)
    if current:
        runtime = current['runtime']
        if runtime:
            if Path(runtime['data_dir']).resolve() != root.resolve() or runtime.get('demo',False) != demo:
                raise ValueError('此端口运行着其他资料目录的网页，未停止或替换。请核对资料目录和端口。')
            if runtime['revision'] == expected and not restart:
                return {'web': 'current', 'url': f'http://127.0.0.1:{port}', 'revision': expected}
        elif not legacy_owned(root, port):
            raise ValueError('旧网页缺少版本信息，且无法确认它属于此资料目录。请核对原启动目录，停止对应旧网页后重试。')
        request(port, '/api/stop', current['token'], {})
        deadline = time.monotonic() + 30
        while listening(port):
            if time.monotonic() >= deadline:
                raise ValueError('旧网页正在等待当前任务结束，尚未退出；未强制终止。稍后重跑 refresh-web。')
            time.sleep(.2)
    elif only_if_running:
        return {'web': 'not_running', 'note': '未运行网页，无需重启；MCP 可独立使用。'}
    with (root / 'server.log').open('a', encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable, '-m', 'wechat_assistant.cli', '--data-dir', str(root.resolve()),
                                    'demo' if demo else 'serve'],
                                   cwd=Path(__file__).resolve().parents[1], stdout=log, stderr=log,
                                   env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    (root / 'server.pid').write_text(str(process.pid), 'ascii')
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        ready = probe(port)
        if ready and ready['runtime']:
            runtime = ready['runtime']
            if runtime['revision'] != expected or Path(runtime['data_dir']).resolve() != root.resolve():
                raise ValueError('启动后的网页版本或资料目录不匹配，请检查端口占用。')
            return {'web': 'restarted' if current else 'started', 'url': f'http://127.0.0.1:{port}', 'revision': expected}
        if process.poll() is not None:break
        time.sleep(.2)
    raise ValueError(f'网页未完成启动，请检查本机日志 {root / "server.log"}。')
