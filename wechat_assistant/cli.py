import contextlib
import io
import json
import os
from pathlib import Path

import click

from .service import Service


@click.group()
@click.option("--data-dir", type=click.Path(), default=None, envvar="WECHAT_ASSISTANT_HOME")
@click.pass_context
def main(ctx, data_dir):
    """微信项目助理：本地采集、项目跟进、主动提醒。"""
    ctx.obj = data_dir


@main.command()
@click.option("--port", type=int, default=None)
@click.pass_obj
def serve(root, port):
    """运行本地页面和后台任务。"""
    from .web import serve as start
    service = Service(root)
    start(service, port or service.config["port"])


@main.command()
@click.option("--port", type=int, default=8767)
@click.pass_obj
def demo(root, port):
    """用独立目录的虚构消息演示，不读取微信。"""
    from .demo import seed
    from .web import serve as start
    service = Service(root or Path.home() / ".wechat-project-assistant-demo", demo=True)
    seed(service.store)
    start(service, port)


@main.command()
@click.option("--db-dir", type=click.Path(exists=True), default=None)
def connect(db_dir):
    """通过上游 wechat-cli 初始化本人本机微信（不显示密钥）。"""
    from wechat_cli.core.config import auto_detect_db_dir, CONFIG_FILE, KEYS_FILE, STATE_DIR
    from wechat_cli.keys import extract_keys
    path = db_dir
    if not path and Path(CONFIG_FILE).exists():
        path = json.loads(Path(CONFIG_FILE).read_text('utf-8')).get('db_dir')
    if not path:
        try:
            path = auto_detect_db_dir()
        except ValueError as exc:
            raise click.ClickException(str(exc)+' 使用 connect --db-dir 指定。') from None
    if not path and os.name == 'nt':
        candidates = list((Path.home() / 'Documents' / 'xwechat_files').glob('*/db_storage'))
        if len(candidates) == 1:
            path = candidates[0]
        elif candidates:
            raise click.ClickException('找到多个账户，请用 --db-dir 指定微信设置中显示的 db_storage 目录。')
    if not path:
        raise click.ClickException("未找到微信数据目录，请保持微信登录或传入 --db-dir。")
    Path(STATE_DIR).mkdir(exist_ok=True, parents=True)
    click.echo("正在检查微信数据库访问，可能需要几十秒……")
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            keys = extract_keys(path, KEYS_FILE)
        if not keys:
            raise RuntimeError("上游未取得可用密钥；可能需要管理员权限或当前微信版本尚未适配。")
        Path(CONFIG_FILE).write_text(json.dumps({"db_dir": str(path)}, ensure_ascii=False), "utf-8")
    except Exception as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"已初始化 {len(keys)} 个数据库。密钥保存在用户目录，不进入项目仓库。")


@main.command()
@click.option('--enable/--disable', default=True)
@click.pass_obj
def autostart(root, enable):
    """设置当前 Windows 用户登录后后台运行；不要求管理员权限。"""
    if os.name != 'nt':
        raise click.ClickException('此命令用于 Windows。')
    import subprocess
    import sys
    import winreg
    pythonw = str(Path(sys.executable).with_name('pythonw.exe'))
    launcher = Path(__file__).resolve().parents[1] / 'launch.py'
    if not launcher.exists():
        raise click.ClickException('请从源码目录安装后使用登录启动。')
    command = [pythonw, str(launcher), '--background']
    if root:
        command += ['--data-dir', str(Path(root).resolve())]
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        if enable:
            winreg.SetValueEx(key, 'WeChatProjectAssistant', 0, winreg.REG_SZ, subprocess.list2cmdline(command))
        else:
            try:
                winreg.DeleteValue(key, 'WeChatProjectAssistant')
            except FileNotFoundError:
                pass
    click.echo('已启用登录后后台运行。' if enable else '已关闭登录后后台运行。')


@main.command()
@click.pass_obj
def status(root):
    click.echo(json.dumps(Service(root).store.status(), ensure_ascii=False, indent=2))


@main.command()
@click.option('--port', type=int, default=None)
@click.pass_obj
def stop(root, port):
    """停止本地后台；正在运行的模型分析会先收尾。"""
    import re
    import urllib.request
    from .config import load
    _, config = load(root)
    url = f"http://127.0.0.1:{port or config['port']}"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            token = re.search(r'name="local-token" content="([^"]+)"', response.read().decode())[1]
        request = urllib.request.Request(url + '/api/stop', b'{}', {'X-Local-Token': token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=3):
            pass
    except (OSError, TypeError) as exc:
        raise click.ClickException('未找到运行中的项目助理。') from exc
    click.echo('已请求停止；当前分析完成后退出。登录启动设置不受影响。')


@main.command()
@click.pass_obj
def sync(root):
    try:
        click.echo(json.dumps(Service(root).perform("sync"), ensure_ascii=False, indent=2))
    except Exception as exc:
        raise click.ClickException(str(exc)) from None


@main.command("analyze")
@click.pass_obj
def analyze_command(root):
    try:
        click.echo(json.dumps(Service(root).perform("analyze"), ensure_ascii=False, indent=2))
    except Exception as exc:
        raise click.ClickException(str(exc)) from None


@main.command()
@click.argument("question")
@click.option("--project", type=int, default=None)
@click.pass_obj
def ask(root, question, project):
    try:
        click.echo(json.dumps(Service(root).perform("ask", {"question": question, "project_id": project}), ensure_ascii=False, indent=2))
    except Exception as exc:
        raise click.ClickException(str(exc)) from None


@main.command()
@click.pass_obj
def mcp(root):
    """提供聊天检索、附件读取及本地项目配置和反馈工具。"""
    from .mcp import run
    run(root)


@main.command('register-client')
@click.argument('client', type=click.Choice(['workbuddy', 'codex', 'dsh']))
@click.option('--config-path', type=click.Path(), help='WorkBuddy mcp.json 或 Codex config.toml 的实际路径。')
@click.option('--profile-dir', type=click.Path(exists=True), help='DSH 当前 profile 目录，不自动猜测。')
@click.pass_obj
def register_client(root, client, config_path, profile_dir):
    """检查 MCP 后注册到指定客户端，备份原配置并保留其他服务。"""
    from .setup_client import server_spec, check_connection, register_workbuddy, register_codex, register_dsh, media_readiness
    try:
        if client == 'dsh' and (not profile_dir or config_path):
            raise ValueError('DSH 需要 --profile-dir 指向当前 profile，不能使用 --config-path。')
        if client in {'workbuddy','codex'} and profile_dir:
            raise ValueError('WorkBuddy / Codex 不使用 --profile-dir。')
        spec = server_spec(root)
        checks = check_connection(spec)
        if client == 'workbuddy':
            result = register_workbuddy(Path(config_path).resolve() if config_path else Path.home() / '.workbuddy' / 'mcp.json', spec)
        elif client == 'codex':
            path = Path(config_path).resolve() if config_path else Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'config.toml'
            result = register_codex(path, spec)
        else:
            result = register_dsh(Path(profile_dir).resolve(), spec)
        result.update(checks)
        result['vision'] = media_readiness(root)
        from .config import load
        from .speech import readiness
        data_root, config = load(root)
        result['speech'] = readiness(data_root, config)
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        raise click.ClickException(str(exc)) from None


@main.command('doctor')
@click.pass_obj
def doctor(root):
    """验证独立 stdio 握手和状态工具，不读取聊天正文。"""
    from .setup_client import server_spec, check_connection, media_readiness
    try:
        result = check_connection(server_spec(root))
        result['vision'] = media_readiness(root)
        from .config import load
        from .speech import readiness
        data_root, config = load(root)
        result['speech'] = readiness(data_root, config)
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        raise click.ClickException(str(exc)) from None


@main.command('use-client')
@click.pass_obj
def use_client(root):
    """切换到当前客户端负责理解和总结，备份旧配置并关闭独立后台分析。"""
    from .setup_client import use_current_client
    click.echo(json.dumps(use_current_client(root), ensure_ascii=False, indent=2))


@main.command('prepare-media')
@click.pass_obj
def prepare_media(root):
    """安装阶段下载本地语音模型并完成加载/推理测试；不读取私人附件。"""
    from .config import load
    from .speech import prepare
    data_root, config = load(root)
    click.echo('正在准备本地语音模型，首次需要联网下载；完成前不算语音已就绪。')
    try:
        click.echo(json.dumps(prepare(data_root, config), ensure_ascii=False, indent=2))
    except Exception as exc:
        raise click.ClickException('语音准备失败；检查网络、磁盘和依赖后重跑 prepare-media。' + str(exc)) from None


@main.command('call-tool')
@click.argument('tool', required=False)
@click.option('--arguments-file', type=click.Path(exists=True, dir_okay=False), help='UTF-8 JSON 参数文件；省略时传入空对象。')
@click.pass_context
def call_tool(ctx, tool, arguments_file):
    """当前客户端未加载 MCP 时调用同一套工具；不传工具名则列出定义与使用约定。"""
    from .tool_bridge import call
    try:
        if arguments_file and not tool:
            raise ValueError('传参数文件时需要指定工具名。')
        arguments = json.loads(Path(arguments_file).read_text('utf-8-sig')) if arguments_file else {}
        if not isinstance(arguments, dict):
            raise ValueError('工具参数必须是 JSON 对象。')
        result = call(ctx.obj, tool, arguments)
    except Exception as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(json.dumps(result, ensure_ascii=False))
    if result.get('isError'):
        ctx.exit(1)


@main.group()
def project():
    """管理保存在本机的项目配置。"""


@project.command('add')
@click.argument('name')
@click.option('--alias', 'aliases', multiple=True, help='项目别名，可重复指定。')
@click.option('--notes', default=None, help='个人分工或项目说明。')
@click.pass_obj
def project_add(root,name,aliases,notes):
    from .work import configure_project
    service=Service(root)
    p=configure_project(service.store,name,list(aliases) if aliases else None,notes)
    click.echo(json.dumps({'id':p['id'],'name':p['name'],'aliases':p['aliases']},ensure_ascii=False))


@project.command('list')
@click.pass_obj
def project_list(root):
    click.echo(json.dumps([{'id':p['id'],'name':p['name'],'aliases':p['aliases'],'status':p['status']} for p in Service(root).store.projects()],ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
