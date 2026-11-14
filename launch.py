"""Open the local page, upgrading a verified old backend when necessary."""
import argparse
from pathlib import Path
import webbrowser

from wechat_assistant.web_runtime import ensure_web


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--data-dir')
    parser.add_argument('--restart', action='store_true')
    args = parser.parse_args()
    root = args.data_dir or (Path.home() / '.wechat-project-assistant-demo' if args.demo else None)
    try:
        result = ensure_web(root, demo=args.demo, restart=args.restart)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if not args.background:
        webbrowser.open(result['url'])


if __name__ == '__main__':
    main()
