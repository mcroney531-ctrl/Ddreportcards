"""
Stage 2C-6 (post-convergence audit F3): Ddreportcards' dependency controls
understand exactly one immutable VCS form, reserved for the shared package:

  dynasty-core @ git+https://github.com/mcroney531-ctrl/dynasty-core.git@<40 lowercase hex>

Everything else must still be an exact ==-pin, and anything that is neither
fails validation instead of being skipped. The real requirement is NOT in
requirements.txt yet; these are fixture-string tests. The actual-file
contract (exactly eight ==-pins today) lives in test_requirements_are_pinned.py
and is deliberately unchanged until 2C-7 adds the real pin.

The installed-source checks mock importlib.metadata, so they test checker
behavior, not whatever this sandbox happens to have installed.
"""
import json
import unittest
from unittest import mock

import scripts.check_dependency_versions as cdv

SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = "https://github.com/mcroney531-ctrl/dynasty-core"
VALID = f"dynasty-core @ git+{REPO}.git@{SHA}"

CURRENT_EIGHT = """\
google-adk[extensions]==2.9.2
litellm==1.102.0
anthropic==1.7.0
streamlit==1.64.0
httpx==0.28.1
python-dotenv==1.2.3
fastapi==0.141.1
uvicorn[standard]==0.53.0
"""


class ImmutableGitFormTest(unittest.TestCase):
    def test_valid_immutable_line_is_accepted(self):
        name, spec = cdv.parse_requirement_line(VALID)
        self.assertEqual(name, "dynasty-core")
        self.assertEqual(spec, {"kind": "git", "value": SHA, "repo": REPO})

    def test_future_nine_line_file_parses_with_one_git_entry(self):
        parsed = cdv.parse_requirements_text(CURRENT_EIGHT + VALID + "\n")
        self.assertEqual(len(parsed), 9)
        self.assertEqual([n for n, s in parsed.items() if s["kind"] == "git"], ["dynasty-core"])
        self.assertEqual(sum(s["kind"] == "version" for s in parsed.values()), 8)

    REJECTED = {
        "branch main": f"dynasty-core @ git+{REPO}.git@main",
        "branch master": f"dynasty-core @ git+{REPO}.git@master",
        "tag": f"dynasty-core @ git+{REPO}.git@v0.1.0",
        "short sha 7": f"dynasty-core @ git+{REPO}.git@0123456",
        "short sha 16": f"dynasty-core @ git+{REPO}.git@0123456789abcdef",
        "39 hex": f"dynasty-core @ git+{REPO}.git@{SHA[:-1]}",
        "41 hex": f"dynasty-core @ git+{REPO}.git@{SHA}0",
        "uppercase sha": f"dynasty-core @ git+{REPO}.git@{SHA.upper()}",
        "missing revision": f"dynasty-core @ git+{REPO}.git",
        "different owner": f"dynasty-core @ git+https://github.com/someone-else/dynasty-core.git@{SHA}",
        "different repository": f"dynasty-core @ git+https://github.com/mcroney531-ctrl/other-repo.git@{SHA}",
        "git+ssh": f"dynasty-core @ git+ssh://git@github.com/mcroney531-ctrl/dynasty-core.git@{SHA}",
        "plain https archive": f"dynasty-core @ https://github.com/mcroney531-ctrl/dynasty-core/archive/{SHA}.zip",
        "embedded credentials": f"dynasty-core @ git+https://token@github.com/mcroney531-ctrl/dynasty-core.git@{SHA}",
        "user:pass credentials": f"dynasty-core @ git+https://u:p@github.com/mcroney531-ctrl/dynasty-core.git@{SHA}",
        "other package via vcs": f"httpx @ git+{REPO}.git@{SHA}",
        "other package via its own repo": f"requests @ git+https://github.com/psf/requests.git@{SHA}",
        "non-canonical dist spelling": f"dynasty_core @ git+{REPO}.git@{SHA}",
        "missing .git suffix": f"dynasty-core @ git+{REPO}@{SHA}",
        "dynasty-core as ordinary index pin": "dynasty-core==0.1.0",
        "dynasty-core unpinned": "dynasty-core",
        "extras on the git form": f"dynasty-core[extra] @ git+{REPO}.git@{SHA}",
        "trailing marker": f"{VALID} ; python_version >= '3.12'",
    }

    def test_every_non_immutable_form_is_rejected(self):
        for label, line in self.REJECTED.items():
            with self.subTest(label):
                with self.assertRaises(cdv.RequirementsError):
                    cdv.parse_requirement_line(line)

    def test_rejections_also_fail_a_whole_file(self):
        for label, line in self.REJECTED.items():
            with self.subTest(label):
                with self.assertRaises(cdv.RequirementsError):
                    cdv.parse_requirements_text(CURRENT_EIGHT + line + "\n")


class OrdinaryPinsAreNotWeakenedTest(unittest.TestCase):
    def test_ranges_and_unpinned_are_rejected(self):
        for line in ("httpx>=0.28", "httpx~=0.28.1", "httpx<1", "httpx", "httpx==0.28.*,<1",
                     "httpx @ https://example.com/httpx.whl", "-e .", "--index-url https://x"):
            with self.subTest(line):
                with self.assertRaises(cdv.RequirementsError):
                    cdv.parse_requirement_line(line)

    def test_invalid_line_fails_instead_of_being_skipped(self):
        with self.assertRaises(cdv.RequirementsError) as ctx:
            cdv.parse_requirements_text(CURRENT_EIGHT + "fastapi>=0.1\n")
        self.assertIn("fastapi>=0.1", str(ctx.exception))

    def test_duplicates_are_rejected_including_normalized_names(self):
        for extra in (VALID + "\n" + VALID, "python_dotenv==1.2.3", "HTTPX==0.28.1", "Google.ADK==2.9.2"):
            with self.subTest(extra):
                with self.assertRaises(cdv.RequirementsError):
                    cdv.parse_requirements_text(CURRENT_EIGHT + extra + "\n")

    def test_comments_and_blank_lines_still_ignored(self):
        parsed = cdv.parse_requirements_text("# comment\n\n" + CURRENT_EIGHT)
        self.assertEqual(len(parsed), 8)

    def test_parse_pins_compat_helper_raises_on_invalid_file(self):
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "requirements.txt"
            path.write_text(CURRENT_EIGHT + "bogus line here\n")
            with self.assertRaises(cdv.RequirementsError):
                cdv.parse_pins(path)


class _FakeDist:
    def __init__(self, direct_url):
        self._direct_url = direct_url

    def read_text(self, name):
        return self._direct_url if name == "direct_url.json" else None


def _metadata(direct_url=None, installed=True):
    md = mock.Mock()
    md.PackageNotFoundError = type("PackageNotFoundError", (Exception,), {})
    if installed:
        md.distribution.return_value = _FakeDist(direct_url)
    else:
        md.distribution.side_effect = md.PackageNotFoundError("dynasty-core")
    return md


def _direct_url(url=f"{REPO}.git", vcs="git", commit=SHA, requested=SHA):
    return json.dumps({"url": url, "vcs_info": {"vcs": vcs, "commit_id": commit, "requested_revision": requested}})


class GitInstalledSourceCheckTest(unittest.TestCase):
    def _check(self, md):
        return cdv.check_git_install("dynasty-core", SHA, REPO, metadata=md)

    def test_matching_repository_and_commit_is_ok(self):
        status, _ = self._check(_metadata(_direct_url()))
        self.assertEqual(status, "OK")

    def test_benign_url_spelling_differences_are_ok(self):
        for url in (REPO, f"{REPO}/", f"{REPO}.git", f"{REPO}.git/"):
            with self.subTest(url):
                self.assertEqual(self._check(_metadata(_direct_url(url=url)))[0], "OK")

    def test_wrong_commit_is_mismatch(self):
        status, message = self._check(_metadata(_direct_url(commit="f" * 40, requested="main")))
        self.assertEqual(status, "MISMATCH")
        self.assertIn("requested 'main'", message)

    def test_missing_direct_url_is_mismatch(self):
        self.assertEqual(self._check(_metadata(None))[0], "MISMATCH")

    def test_malformed_direct_url_is_mismatch(self):
        for raw in ("{not json", json.dumps({"url": REPO}), json.dumps({"vcs_info": {"vcs": "git"}}), "[]"):
            with self.subTest(raw):
                self.assertEqual(self._check(_metadata(raw))[0], "MISMATCH")

    def test_non_git_vcs_is_mismatch(self):
        self.assertEqual(self._check(_metadata(_direct_url(vcs="hg")))[0], "MISMATCH")

    def test_wrong_repository_is_mismatch_even_with_matching_commit(self):
        for url in ("https://github.com/someone-else/dynasty-core.git",
                    "https://github.com/mcroney531-ctrl/other-repo.git",
                    "https://token@github.com/mcroney531-ctrl/dynasty-core.git"):
            with self.subTest(url):
                self.assertEqual(self._check(_metadata(_direct_url(url=url)))[0], "MISMATCH")

    def test_package_not_installed_is_missing(self):
        self.assertEqual(self._check(_metadata(installed=False))[0], "MISSING")


class MainDispatchTest(unittest.TestCase):
    def test_git_spec_is_checked_by_source_not_version(self):
        specs = cdv.parse_requirements_text(CURRENT_EIGHT + VALID + "\n")
        with mock.patch.object(cdv, "parse_requirements", return_value=specs), \
             mock.patch.object(cdv, "check_version_install", return_value=("OK", "x")) as by_version, \
             mock.patch.object(cdv, "check_git_install", return_value=("OK", "git")) as by_git, \
             mock.patch("builtins.print"):
            self.assertEqual(cdv.main(), 0)
        by_git.assert_called_once_with("dynasty-core", SHA, REPO)
        self.assertEqual(by_version.call_count, 8)
        self.assertNotIn("dynasty-core", [c.args[0] for c in by_version.call_args_list])

    def test_invalid_file_makes_main_fail(self):
        with mock.patch.object(cdv, "parse_requirements", side_effect=cdv.RequirementsError("bad")), \
             mock.patch("builtins.print"):
            self.assertEqual(cdv.main(), 2)


if __name__ == "__main__":
    unittest.main()
