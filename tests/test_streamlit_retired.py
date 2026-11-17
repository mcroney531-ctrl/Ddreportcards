"""
The Streamlit report-card UI is retired; the FastAPI service is the product.

app.py was a separate Streamlit Community Cloud deployment, now deleted, and
nothing else imported it. Render runs `bash scripts/render_build.sh` then
`uvicorn api:app`, takes Python from .python-version, and installs
requirements.lock.txt -- so app.py, runtime.txt (a Streamlit Cloud file) and
the streamlit pin only made Render install seven unused packages.

These checks pin the retirement and the boundary it must not cross: the API's
build, start command, Python source and dynasty-core pin are unchanged.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STREAMLIT_ONLY = ("altair", "itsdangerous", "narwhals", "pyarrow", "pydeck", "streamlit", "toml")
CORE_SHA = "cef3c3d2b7120825110235eb2c30b4f8dd9a0247"


def _names(path):
    names = set()
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(re.split(r"[\[=@ ]", line, maxsplit=1)[0].lower().replace("_", "-"))
    return names


class StreamlitUiRetiredTest(unittest.TestCase):
    def test_streamlit_files_are_gone(self):
        for name in ("app.py", "runtime.txt"):
            with self.subTest(name):
                self.assertFalse((ROOT / name).exists())

    def test_streamlit_is_not_a_dependency(self):
        self.assertNotIn("streamlit", _names("requirements.txt"))
        locked = _names("requirements.lock.txt")
        for package in STREAMLIT_ONLY:
            with self.subTest(package):
                self.assertNotIn(package, locked)

    def test_no_runtime_code_imports_streamlit(self):
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in ("tests", "docs") or any(p.startswith(".") for p in rel.parts):
                continue
            if "site-packages" in rel.parts or "__pycache__" in rel.parts:
                continue
            with self.subTest(str(rel)):
                self.assertIsNone(re.search(r"^\s*(import|from)\s+streamlit\b",
                                            path.read_text(encoding="utf-8"), re.M))

    def test_docs_do_not_describe_a_live_streamlit_app(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("streamlit run", readme)
        self.assertIn("uvicorn api:app", readme)
        self.assertNotIn("app.py", (ROOT / "config" / "dynasty_config.py").read_text(encoding="utf-8"))


class RenderBoundaryUnchangedTest(unittest.TestCase):
    def test_build_start_and_python_source(self):
        script = (ROOT / "scripts" / "render_build.sh").read_text(encoding="utf-8")
        self.assertIn('pip install "pip==26.3"', script)
        self.assertIn("pip install -r requirements.lock.txt", script)
        self.assertIn("pip check", script)
        render = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertRegex(render, r"buildCommand:\s*bash scripts/render_build\.sh")
        self.assertRegex(render, r"startCommand:\s*uvicorn api:app")
        self.assertEqual((ROOT / ".python-version").read_text(encoding="utf-8").strip(), "3.12")

    def test_api_dependencies_and_core_pin_remain_locked(self):
        locked = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
        for needle in ("fastapi==", "uvicorn==", "google-adk==", "litellm==", "anthropic==", "pandas==",
                       f"dynasty-core @ git+https://github.com/mcroney531-ctrl/dynasty-core.git@{CORE_SHA}"):
            with self.subTest(needle):
                self.assertIn(needle, locked)


if __name__ == "__main__":
    unittest.main()
