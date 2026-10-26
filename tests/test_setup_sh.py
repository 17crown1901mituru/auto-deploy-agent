"""termux-bridge/setup.sh のテスト。偽のHOMEで実行し、インストールと起動は省略する。
実行: python3 -m unittest discover -s tests -p "test_setup_sh.py"
"""
import os
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(HERE, "..", "termux-bridge", "setup.sh")
TOKEN = "AbCdEfGhIjKlMnOpQrStUvWx-_12"


def run(home, token=TOKEN, extra=None):
    env = {"PATH": os.environ["PATH"], "HOME": home, "SETUP_SKIP_INSTALL": "1", "SETUP_SKIP_START": "1"}
    if token is not None:
        env["BRIDGE_TOKEN"] = token
    env.update(extra or {})
    return subprocess.run(["bash", SETUP], env=env, capture_output=True, text=True, timeout=60)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.props = os.path.join(self.home, ".termux", "termux.properties")

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_fresh_install_writes_property_and_token(self):
        r = run(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("allow-external-apps = true", self.read(self.props))
        token_file = os.path.join(self.home, ".auto-deploy", "bridge_token")
        self.assertEqual(self.read(token_file), TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(token_file).st_mode), 0o600)

    def test_is_idempotent_and_keeps_other_settings(self):
        os.makedirs(os.path.dirname(self.props))
        with open(self.props, "w") as fh:
            fh.write("extra-keys = [['ESC','TAB']]\nbell-character = vibrate\n")
        run(self.home)
        run(self.home)
        text = self.read(self.props)
        self.assertEqual(text.count("allow-external-apps"), 1)
        self.assertIn("extra-keys = [['ESC','TAB']]", text)
        self.assertIn("bell-character = vibrate", text)

    def test_enables_commented_or_false_lines_without_duplicates(self):
        os.makedirs(os.path.dirname(self.props))
        for original in ("# allow-external-apps = true\n", "allow-external-apps = false\n", "allow-external-apps=false\n"):
            with open(self.props, "w") as fh:
                fh.write(original + "other = 1\n")
            self.assertEqual(run(self.home).returncode, 0)
            text = self.read(self.props)
            self.assertEqual(text.count("allow-external-apps"), 1, (original, text))
            self.assertIn("allow-external-apps = true", text)
            self.assertNotIn("false", text)
            self.assertIn("other = 1", text)

    def test_rejects_bad_tokens(self):
        for bad in ["short", "has space in it 1234567", "semi;colon-1234567890", "quote'quote-1234567890"]:
            r = run(self.home, token=bad)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".auto-deploy", "bridge_token")))

    def test_without_token_keeps_existing(self):
        os.makedirs(os.path.join(self.home, ".auto-deploy"))
        token_file = os.path.join(self.home, ".auto-deploy", "bridge_token")
        with open(token_file, "w") as fh:
            fh.write("existing-token-1234567")
        self.assertEqual(run(self.home, token=None).returncode, 0)
        self.assertEqual(self.read(token_file), "existing-token-1234567")


if __name__ == "__main__":
    unittest.main()
