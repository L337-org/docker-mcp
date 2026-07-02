import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_intel_macos_cryptography_pin_not_relaxed():
    """
    Dependabot doesn't get an automatic Copilot review (bots aren't billable for
    premium requests), so a bump here must be caught by a hard, deterministic
    CI failure instead of relying on review. See the pyproject.toml comment:
    cryptography>=49 dropped the macosx_10_9_universal2 wheel, breaking `uvx`
    installs on Intel macOS.
    """
    data = tomllib.loads(PYPROJECT.read_text())
    deps = data["project"]["dependencies"]
    matches = [d for d in deps if d.split(";")[0].strip().startswith("cryptography")]
    assert matches, "the Intel-macOS cryptography pin is missing from pyproject.toml dependencies"

    dep = matches[0]
    assert "platform_system == 'Darwin'" in dep and "platform_machine == 'x86_64'" in dep, (
        f"cryptography dependency is no longer scoped to Intel macOS: {dep!r}"
    )

    m = re.search(r"cryptography\s*<\s*(\d+)", dep)
    assert m, f"cryptography pin must use a '<N' upper bound, got: {dep!r}"
    assert int(m.group(1)) <= 49, (
        f"cryptography upper bound raised to {m.group(1)} — 49.x has no x86_64 macOS wheel; "
        "see the pyproject.toml comment before lifting this cap"
    )
