import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from ruamel.yaml import YAML

from wechat_assistant.cli import main
from wechat_assistant.setup_client import register_workbuddy, register_dsh, write_config, server_spec, check_connection, workbuddy_activation


class ClientSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='微信 安装 ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = {'command': str(self.root / 'wechat-assistant.exe'), 'args': ['--data-dir', str(self.root / '资料'), 'mcp'],
                     'env': {'PYTHONIOENCODING': 'utf-8'}}

    def test_workbuddy_preserves_other_servers_and_exact_backup_and_is_idempotent(self):
        path = self.root / 'mcp.json'
        original = b'\xef\xbb\xbf' + json.dumps({'other': True, 'mcpServers': {'other': {'command': 'other'},
            'wechat-project-assistant': {'command': 'old/wechat-assistant.exe', 'env': {'KEEP': 'yes'}}}}).encode()
        path.write_bytes(original)
        result = register_workbuddy(path, self.spec)
        self.assertEqual(Path(result['backup']).read_bytes(), original)
        data = json.loads(path.read_text('utf-8'))
        self.assertTrue(data['other'])
        self.assertEqual(data['mcpServers']['other'], {'command': 'other'})
        self.assertEqual(data['mcpServers']['wechat-project-assistant']['env']['KEEP'], 'yes')
        self.assertEqual(data['mcpServers']['wechat-project-assistant']['args'], self.spec['args'])
        self.assertNotIn('wechat-chat', data['mcpServers'])
        self.assertIsNone(register_workbuddy(path, self.spec)['backup'])

    def test_invalid_and_conflicting_workbuddy_files_are_unchanged(self):
        for contents in ('{bad', '[]', '{"mcpServers": []}', '{"mcpServers":{"wechat-chat":{"command":"unrelated"}}}'):
            path = self.root / 'mcp.json'
            path.write_text(contents, 'utf-8')
            with self.assertRaises(ValueError):
                register_workbuddy(path, self.spec)
            self.assertEqual(path.read_text('utf-8'), contents)

    def test_workbuddy_registration_never_creates_or_claims_client_approval(self):
        path = self.root / 'mcp.json'
        approval = self.root / 'mcp-approvals.json'
        result = register_workbuddy(path, self.spec)
        self.assertFalse(approval.exists())
        self.assertEqual(result['trust_record_file'], 'not_found')
        self.assertEqual(result['native_activation'], 'requires_client_verification')
        approval.write_bytes(b'{"unrelated-service":123}')
        original = approval.read_bytes()
        register_workbuddy(path, self.spec)
        self.assertEqual(approval.read_bytes(), original)
        self.assertEqual(workbuddy_activation(path)['trust_record_file'], 'present_not_verified')
        self.assertEqual(workbuddy_activation(path)['native_activation'], 'requires_client_verification')

    def profile(self):
        (self.root / 'package.json').write_text(json.dumps({'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-web-app']}}}), 'utf-8')
        return self.root / 'cordis.patch.yml'

    def test_dsh_preserves_comments_expressions_and_other_manager_servers(self):
        path = self.profile()
        original = '# keep me\n- id: mcp-manager\n  config:\n    servers:\n      other:\n        transport: stdio\n        command: !!js process.env.MY_COMMAND\n'
        path.write_text(original, 'utf-8')
        original_bytes = path.read_bytes()
        result = register_dsh(self.root, self.spec)
        text = path.read_text('utf-8')
        self.assertIn('# keep me', text)
        self.assertIn('!!js process.env.MY_COMMAND', text)
        self.assertEqual(Path(result['backup']).read_bytes(), original_bytes)
        doc = YAML().load(text)
        self.assertEqual(doc[0]['config']['servers']['other']['transport'], 'stdio')
        config = doc[1]['insert'][0]['config']
        self.assertEqual(config['command'], self.spec['command'])
        self.assertEqual(config['toolCallTimeoutMs'], 900000)
        self.assertIsNone(register_dsh(self.root, self.spec)['backup'])

    def test_dsh_requires_initialized_profile_and_refuses_collision(self):
        with self.assertRaises(ValueError):
            register_dsh(self.root, self.spec)
        path = self.profile()
        original = '- id: mcp\n  config:\n    servers:\n      wechat-chat: {transport: stdio, command: other}\n'
        path.write_text(original, 'utf-8')
        with self.assertRaises(ValueError):
            register_dsh(self.root, self.spec)
        self.assertEqual(path.read_text('utf-8'), original)

    def test_empty_dsh_patch_and_relocation(self):
        path = self.profile()
        path.write_text('[]\n', 'utf-8')
        register_dsh(self.root, self.spec)
        moved = {**self.spec, 'command': str(self.root / '新版' / 'wechat-assistant.exe')}
        register_dsh(self.root, moved)
        doc = YAML().load(path.read_text('utf-8'))
        self.assertEqual(len(doc), 1)
        self.assertEqual(doc[0]['insert'][0]['config']['command'], moved['command'])

    def test_concurrent_change_is_not_overwritten(self):
        path = self.root / 'mcp.json'
        path.write_bytes(b'newer')
        with self.assertRaises(ValueError):
            write_config(path, b'older', 'ours')
        self.assertEqual(path.read_bytes(), b'newer')
        self.assertFalse(list(self.root.glob('*.bak-*')))

    def test_probe_failure_does_not_register(self):
        path = self.root / 'mcp.json'
        with patch('wechat_assistant.setup_client.check_connection', side_effect=RuntimeError('probe failed')):
            result = CliRunner().invoke(main, ['register-client', 'workbuddy', '--config-path', str(path)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(path.exists())

    def test_real_stdio_probe_uses_isolated_data_and_no_chat_reads(self):
        result = check_connection(server_spec(self.root / '资料'))
        self.assertEqual(result['project_status'], 'ok')
        self.assertGreater(result['tool_count'], 10)
        self.assertIn('待', result['client_session'])


if __name__ == '__main__':
    unittest.main()
