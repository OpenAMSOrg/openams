# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/context_pack.py: deterministic C and Python extraction, line ranges,
the not-found status and the model summary path with `ask` patched. No model and
no network are used."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'tools'))
import context_pack  # noqa: E402

C_SOURCE = '''\
/* A doc comment. */
static int helper(int x);

static int fps_update(km_operation_core_t *c, uint32_t now)
{
    if (c->armed) {
        c->count++;
    }
    return 0;
}

int other(void)
{
    return 1;
}
'''
PY_SOURCE = '''\
class A:
    def one(self):
        """Doc."""
        return 1

    def two(self):
        return 2
'''


class ContextPackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text)
        return path

    def run_tool(self, argv):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = context_pack.main(argv)
        return status, stdout.getvalue()

    def test_c_definition_but_not_prototype(self):
        path = self.write('a.c', C_SOURCE)
        status, out = self.run_tool(['--no-model', '%s:fps_update' % path])
        self.assertEqual(status, 0)
        self.assertIn(':4-10 fps_update', out)
        self.assertIn('static int fps_update(km_operation_core_t *c, uint32_t now)', out)
        self.assertNotIn('helper', out)

    def test_nested_braces_do_not_end_the_body(self):
        path = self.write('a.c', C_SOURCE)
        _, out = self.run_tool(['--no-model', '--with-code', '%s:fps_update' % path])
        self.assertIn('        c->count++;', out)
        self.assertIn('    return 0;\n}', out)

    def test_multiple_matches_are_all_reported(self):
        src = 'int dup(void)\n{\n    return 1;\n}\n\nint dup(void)\n{\n    return 2;\n}\n'
        path = self.write('dup.c', src)
        status, out = self.run_tool(['--no-model', '%s:dup' % path])
        self.assertEqual(status, 0)
        self.assertIn(':1-4 dup', out)
        self.assertIn(':6-9 dup', out)
        self.assertEqual(out.count('### '), 2)

    def test_prototype_only_and_missing_symbol_are_not_found(self):
        path = self.write('a.c', C_SOURCE)
        for spec in ('%s:helper' % path, '%s:nowhere' % path, '/nonexistent/x.c:foo'):
            status, out = self.run_tool(['--no-model', spec])
            self.assertEqual(status, 1, spec)
            self.assertIn('not found', out)

    def test_python_method_body_and_docstring(self):
        path = self.write('m.py', PY_SOURCE)
        status, out = self.run_tool(['--no-model', '--with-code', '%s:one' % path])
        self.assertEqual(status, 0)
        self.assertIn(':2-4 one', out)
        self.assertIn('    def one(self):', out)
        self.assertIn('        return 1', out)
        self.assertNotIn('def two', out)

    def test_python_class(self):
        path = self.write('c.py', 'class Foo:\n    x = 1\n\nclass Foo2:\n    pass\n')
        status, out = self.run_tool(['--no-model', '%s:Foo' % path])
        self.assertEqual(status, 0)
        self.assertIn(':1-2 Foo', out)
        self.assertIn('class Foo:', out)
        self.assertNotIn('Foo2', out)

    def test_line_range(self):
        path = self.write('a.c', C_SOURCE)
        status, out = self.run_tool(['--no-model', '--with-code', '%s:6-8' % path])
        self.assertEqual(status, 0)
        self.assertIn(':6-8', out)
        self.assertIn('if (c->armed) {', out)
        self.assertIn('c->count++;', out)
        self.assertNotIn('return 0;', out)

    def test_range_out_of_bounds_is_not_found(self):
        path = self.write('a.c', C_SOURCE)
        status, out = self.run_tool(['--no-model', '%s:99-120' % path])
        self.assertEqual(status, 1)
        self.assertIn('not found', out)

    def test_no_model_never_calls_the_model(self):
        path = self.write('a.c', C_SOURCE)
        with mock.patch.object(context_pack, 'ask', side_effect=AssertionError('called')):
            status, out = self.run_tool(['--no-model', '%s:fps_update' % path])
        self.assertEqual(status, 0)
        self.assertIn('static int fps_update', out)

    def test_one_summary_per_function_with_the_instruction_first(self):
        path = self.write('a.c', C_SOURCE)
        seen = []

        def fake(text):
            seen.append(text)
            return 'Does the thing.'

        with mock.patch.object(context_pack, 'ask', fake):
            status, out = self.run_tool(['%s:fps_update' % path, '%s:other' % path])
        self.assertEqual(status, 0)
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0].startswith(context_pack.SUMMARY_PROMPT))
        self.assertIn('fps_update', seen[0])
        self.assertIn('int other(void)', seen[1])
        self.assertIn('return 1;', seen[1])
        self.assertEqual(out.count('Does the thing.'), 2)

    def test_model_failure_is_reported_inline(self):
        path = self.write('a.c', C_SOURCE)
        with mock.patch.object(context_pack, 'ask', side_effect=RuntimeError('server down')):
            status, out = self.run_tool(['%s:fps_update' % path])
        self.assertEqual(status, 0)
        self.assertIn('(summary unavailable: server down)', out)
        self.assertIn('static int fps_update', out)


if __name__ == '__main__':
    unittest.main()
