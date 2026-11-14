"""Local client registration; never read chats or client credentials here."""
import asyncio
from datetime import datetime, timezone, timedelta
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


SERVER = 'wechat-chat'
DSH_ENTRY = 'wechat-chat-mcp'


def server_spec(root=None):
    executable = Path(sys.executable).with_name('wechat-assistant.exe' if os.name == 'nt' else 'wechat-assistant')
    if not executable.is_file():
        raise ValueError('找不到 wechat-assistant 可执行文件，请先在 .venv 安装 .[mcp,media]。')
    args = ['mcp']
    if root:
        args = ['--data-dir', str(Path(root).resolve()), *args]
    return {'command': str(executable.resolve()), 'args': args,
            'env': {'PYTHONIOENCODING': 'utf-8'}}


def write_config(path, original, content):
    """Back up exact original bytes and avoid overwriting a concurrent edit."""
    path = Path(path)
    encoded = content.encode('utf-8')
    if original == encoded:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(encoded)
    try:
        current = path.read_bytes() if path.exists() else None
        if current != original:
            raise ValueError('客户端配置刚被其他程序修改，请重新运行注册。')
        if original is not None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            backup = path.with_name(path.name + '.bak-' + stamp)
            with backup.open('xb') as stream:
                stream.write(original)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return str(backup) if backup else None


def register_workbuddy(path, spec):
    path = Path(path)
    original = path.read_bytes() if path.exists() else None
    doc = json.loads(original.decode('utf-8-sig')) if original is not None else {}
    if not isinstance(doc, dict) or not isinstance(doc.get('mcpServers', {}), dict):
        raise ValueError('WorkBuddy 配置不是有效的 mcpServers 对象，原文件未修改。')
    servers = doc.setdefault('mcpServers', {})
    names = [name for name in (SERVER, 'wechat-project-assistant') if name in servers]
    if len(names) > 1:
        raise ValueError('已有两份微信服务配置，请先核对要保留哪一份。')
    name = names[0] if names else SERVER
    old = servers.get(name, {})
    if not isinstance(old, dict) or (old and 'wechat-assistant' not in str(old.get('command', ''))):
        raise ValueError('同名服务不是本项目，原配置未修改。')
    # timeout is the connection budget, not a documented tool-call budget.
    servers[name] = {**old, **spec, 'env': {**old.get('env', {}), **spec['env']},
                     'type': 'stdio', 'timeout': max(old.get('timeout', 0), 60000)}
    backup = write_config(path, original, json.dumps(doc, ensure_ascii=False, indent=2) + '\n')
    return {'client': 'workbuddy', 'config': str(path), 'server': name, 'backup': backup,
            **workbuddy_activation(path)}


def workbuddy_activation(config_path):
    # Presence alone never proves that this exact command/config was approved.
    # WorkBuddy owns the approval store; this installer must never write it.
    approvals = Path(config_path).parent / 'mcp-approvals.json'
    return {
        'native_activation': 'requires_client_verification',
        'configuration_status': 'saved',
        'client_trust': 'verify_in_client',
        'native_call': 'not_verified_by_installer',
        'trust_record_file': 'present_not_verified' if approvals.is_file() else 'not_found',
        'next_step': 'WorkBuddy 5.9.3：专家·技能·连接器 → 连接器 → 自定义连接器，查看本服务；若提示首次信任，点击“信任”后连接，再在当前对话通过原生工具调用 project_status。列表未更新时先使用客户端刷新/配置保存入口。',
        'approval_boundary': '信任确认由 WorkBuddy 管理；不能写入审批文件，也不能用 call-tool 绕过待确认、拒绝或禁用状态。重启不代替信任确认。',
    }


def register_dsh(profile_dir, spec):
    """Insert a standalone MCP row; never replace the manager's inherited map."""
    from ruamel.yaml import YAML
    profile_dir = Path(profile_dir)
    manifest = profile_dir / 'package.json'
    if not manifest.is_file() or not isinstance(json.loads(manifest.read_text('utf-8-sig')).get('dsh', {}).get('profile'), dict):
        raise ValueError('不是已初始化的 DSH profile；请从当前客户端确认实际 profile 目录。')
    path = profile_dir / 'cordis.patch.yml'
    original = path.read_bytes() if path.exists() else None
    yaml = YAML()
    yaml.preserve_quotes = True
    doc = yaml.load(original.decode('utf-8-sig')) if original is not None else []
    if not isinstance(doc, list):
        raise ValueError('DSH patch 应是 YAML 列表，原文件未修改。')
    matches = []

    def visit(value):
        if isinstance(value, dict):
            if value.get('id') == DSH_ENTRY:
                matches.append(value)
            elif value.get('serverName') == SERVER or (isinstance(value.get('servers'), dict) and SERVER in value['servers']):
                raise ValueError('其他条目已经注册 wechat-chat，请由当前智能体核对，避免重复服务。')
            for key, child in value.items():
                if value.get('id') == DSH_ENTRY and key == 'config':
                    continue
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(doc)
    if len(matches) > 1:
        raise ValueError('DSH 中存在重复微信服务条目，原文件未修改。')
    if matches:
        row = matches[0]
        if row.get('name') != '@deepseek-ai/dsh-mcp-client':
            raise ValueError('DSH 同名条目属于其他插件，原文件未修改。')
    else:
        row = {'id': DSH_ENTRY, 'name': '@deepseek-ai/dsh-mcp-client'}
        doc.append({'insert': [row]})
    config = row.setdefault('config', {})
    env = {**config.get('env', {}), **spec['env']}
    config.update({**spec, 'env': env, 'serverName': SERVER, 'transport': 'stdio',
                   'startupTimeoutMs': 60000, 'toolCallTimeoutMs': 900000})
    output = io.StringIO()
    yaml.dump(doc, output)
    backup = write_config(path, original, output.getvalue())
    return {'client': 'dsh', 'config': str(path), 'server': SERVER, 'backup': backup,
            'note': '独立 MCP 条目；不显示在 MCP manager 列表中，以实际工具调用验证。'}


async def probe(spec):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async with stdio_client(StdioServerParameters(**spec)) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
            await session.initialize()
            result = await session.list_tools()
            names = {tool.name for tool in result.tools}
            required = {'project_status', 'discover_chats', 'get_project_brief', 'read_attachment'}
            if not required <= names:
                raise ValueError('MCP 工具不完整，请检查安装版本。')
            status = await session.call_tool('project_status', {})
            if status.isError:
                raise ValueError('project_status 调用失败，请检查本地资料目录。')
            return {'stdio': 'ok', 'project_status': 'ok', 'tool_count': len(names),
                    'client_session': '待当前客户端实际调用验证'}


def check_connection(spec):
    return asyncio.run(asyncio.wait_for(probe(spec), timeout=90))


def media_readiness(root=None):
    from .config import load
    _, config = load(root)
    if config['provider'] == 'codex':
        found = bool(shutil.which(config.get('codex_command', 'codex')))
        return '检测到 Codex CLI；登录与图片识读仍需实测。' if found else '未配置图片识读：需要 Codex CLI 或兼容视觉 API；文字检索可用。'
    ready = bool(config.get('api_base') and config.get('model') and os.environ.get(config['api_key_env']))
    return '检测到 API 配置；视觉能力仍需实测。' if ready else '视觉 API 配置不完整；文字检索可用。'
