"""The launchd jobs written by mac/bot.sh must be valid commands for the bot's CLI."""
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from mm.__main__ import build_parser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB_LAUNCHCTL = """#!/bin/bash
mkdir -p "$HOME/.ld"
case "$1" in
  print) [ -f "$HOME/.ld/$(basename "$2")" ] ;;
  bootstrap) touch "$HOME/.ld/$(basename "$3" .plist)" ;;
  bootout) rm -f "$HOME/.ld/$(basename "$2")" ;;
esac
"""


@unittest.skipUnless(shutil.which("bash"), "bash not available")
class MacScriptTests(unittest.TestCase):
    def test_plists_are_valid_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            # a copy of the script, so its bot.conf and plists never touch the real installation
            os.makedirs(os.path.join(tmp, "app", "mac"))
            script = os.path.join(tmp, "app", "mac", "bot.sh")
            shutil.copy(os.path.join(ROOT, "mac", "bot.sh"), script)
            bindir, home = os.path.join(tmp, "bin"), os.path.join(tmp, "home")
            os.makedirs(bindir)
            agents = os.path.join(home, "Library", "LaunchAgents")
            os.makedirs(agents)
            for name, body in (("launchctl", STUB_LAUNCHCTL), ("lsof", "#!/bin/bash\nexit 0\n")):
                path = os.path.join(bindir, name)
                with open(path, "w") as f:
                    f.write(body)
                os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
            open(os.path.join(agents, "local.polymarket-mm.plist"), "w").close()  # "installed"
            open(os.path.join(agents, "local.polymarket-mm-pages.plist"), "w").close()  # pages enabled
            env = dict(os.environ, HOME=home, PATH=bindir + os.pathsep + os.environ["PATH"])
            r = subprocess.run(["bash", script, "apply"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            seen = set()
            for fname in os.listdir(agents):
                with open(os.path.join(agents, fname), "rb") as f:
                    args = plistlib.load(f)["ProgramArguments"]
                cli = args[args.index("mm") + 1:]
                build_parser().parse_args(cli)  # exits (SystemExit) on unknown or bad arguments
                seen.add(cli[0])
            self.assertEqual(seen, {"paper", "dashboard", "publish"})


if __name__ == "__main__":
    unittest.main()
