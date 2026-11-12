"""Small Windows launcher: reuse a running server, otherwise start it hidden."""
import argparse
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from wechat_assistant.config import load


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--data-dir')
    args = parser.parse_args()
    root, config = load(args.data_dir or (Path.home() / '.wechat-project-assistant-demo' if args.demo else None))
    port = 8767 if args.demo else config['port']
    url = f'http://127.0.0.1:{port}'

    def alive():
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return 'local-token' in response.read().decode('utf-8')
        except OSError:
            return False

    if not alive():
        with (root / 'server.log').open('a', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, '-m', 'wechat_assistant.cli', '--data-dir', str(root),
                                        'demo' if args.demo else 'serve'],
                                       cwd=Path(__file__).parent, stdout=log, stderr=log,
                                       env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        (root / 'server.pid').write_text(str(process.pid), 'ascii')
        for _ in range(40):
            if alive():
                break
            if process.poll() is not None:
                raise SystemExit(f'启动失败，请查看 {root / "server.log"}')
            time.sleep(.25)
    if not args.background:
        webbrowser.open(url)


if __name__ == '__main__':
    main()
