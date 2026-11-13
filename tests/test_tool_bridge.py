import json
from pathlib import Path
import tempfile
import unittest

from click.testing import CliRunner

from wechat_assistant.cli import main
from wechat_assistant.tool_bridge import call


class ToolBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_lists_real_tool_schemas_and_server_instructions(self):
        result = call(self.root)
        self.assertTrue(result['instructions'])
        tools = {tool['name']: tool for tool in result['tools']}
        self.assertIn('discover_chats', tools)
        self.assertIn('inputSchema', tools['discover_chats'])

    def test_status_and_unknown_tool_have_real_mcp_result_and_exit_code(self):
        runner = CliRunner()
        ok = runner.invoke(main, ['--data-dir', str(self.root), 'call-tool', 'project_status'])
        self.assertEqual(ok.exit_code, 0, ok.output)
        self.assertFalse(json.loads(ok.output)['isError'])
        bad = runner.invoke(main, ['--data-dir', str(self.root), 'call-tool', 'nonexistent_tool'])
        self.assertEqual(bad.exit_code, 1)
        self.assertTrue(json.loads(bad.output)['isError'])

    def test_unicode_argument_file_and_mcp_validation(self):
        path = self.root / '参数.json'
        path.write_text(json.dumps({'query': '虚构项目', 'limit': 1}, ensure_ascii=False), 'utf-8-sig')
        runner = CliRunner()
        result = runner.invoke(main, ['--data-dir', str(self.root), 'call-tool', 'discover_chats', '--arguments-file', str(path)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(json.loads(result.output)['isError'])
        path.write_text('{"limit": "not an integer"}', 'utf-8')
        result = runner.invoke(main, ['--data-dir', str(self.root), 'call-tool', 'discover_chats', '--arguments-file', str(path)])
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(json.loads(result.output)['isError'])
        path.write_text('[]', 'utf-8')
        result = runner.invoke(main, ['call-tool', 'discover_chats', '--arguments-file', str(path)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('JSON', result.output)
