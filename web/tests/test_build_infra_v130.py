"""WI-01 Build Infrastructure Tests (v1.3.0)"""

import subprocess
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent


def run(cmd: str, cwd: Path = WEB_DIR) -> subprocess.CompletedProcess:
    """Run a command in the web directory (uses shell for npm/npx .cmd on Windows)."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        shell=True,
    )


def test_build_infra_smoke() -> None:
    """npm install → exit 0; npm run build → exit 0, dist/index.html exists."""
    # Verify dependencies are already installed
    node_modules = WEB_DIR / "node_modules"
    assert node_modules.is_dir(), (
        f"node_modules missing. Run 'npm install' in {WEB_DIR}"
    )

    # Run build
    result = run("npm run build")
    assert result.returncode == 0, (
        f"Build failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    # Verify dist/index.html exists
    dist_index = WEB_DIR / "dist" / "index.html"
    assert dist_index.is_file(), (
        f"dist/index.html not found after build.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    print("PASS: test_build_infra_smoke")


def test_tsconfig_strict() -> None:
    """Verify tsc strict mode catches implicit-any."""
    # Create a temporary file with implicit any
    temp_file = WEB_DIR / "src" / "_strict_test_temp.tsx"
    try:
        temp_file.write_text(
            "function badFn(x) { return x; }\n"
            "export default badFn;\n",
            encoding="utf-8",
        )

        # Run tsc --noEmit (should fail on implicit any)
        result = run("npx tsc --noEmit")
        assert result.returncode != 0, (
            "TypeScript strict mode did NOT catch implicit-any.\n"
            f"Expected non-zero exit, got {result.returncode}.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        # Verify the error message mentions the implicit any
        combined = result.stdout + result.stderr
        assert "implicit" in combined.lower() or "any" in combined.lower() or "7006" in combined, (
            "Expected error about implicit any, but got:\n"
            f"{combined}"
        )

        print("PASS: test_tsconfig_strict")
    finally:
        if temp_file.exists():
            temp_file.unlink()


def main() -> None:
    """Run all WI-01 tests."""
    failures = []

    for name, test_fn in [
        ("test_build_infra_smoke", test_build_infra_smoke),
        ("test_tsconfig_strict", test_tsconfig_strict),
    ]:
        print(f"\n{'='*60}")
        print(f"Running: {name}")
        print(f"{'='*60}")
        try:
            test_fn()
        except AssertionError as e:
            print(f"FAIL: {name}\n  {e}")
            failures.append(name)
        except Exception as e:
            print(f"ERROR: {name}\n  {type(e).__name__}: {e}")
            failures.append(name)

    print(f"\n{'='*60}")
    if failures:
        print(f"FAILURES: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
