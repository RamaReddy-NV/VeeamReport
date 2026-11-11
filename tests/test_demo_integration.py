import json
import subprocess
import sys


def test_demo_report_generation(tmp_path):
    output_dir = tmp_path / "out"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vdr",
            "--config",
            "config.yaml",
            "--demo",
            "--out",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"\nSTDOUT:\n{result.stdout}"
        f"\nSTDERR:\n{result.stderr}"
    )

    html_file = output_dir / "report.html"
    json_file = output_dir / "report.json"

    assert html_file.exists(), "report.html not generated"
    assert json_file.exists(), "report.json not generated"

    assert html_file.stat().st_size > 0
    assert json_file.stat().st_size > 0

    html_content = html_file.read_text(
        encoding="utf-8"
    )

    assert "<html" in html_content.lower()

    data = json.loads(
        json_file.read_text(
            encoding="utf-8"
        )
    )

    assert isinstance(data, dict)

    assert "title" in data
    assert "date_str" in data
    assert "summary" in data

    assert isinstance(data["summary"], list)

    if data["summary"]:
        summary = data["summary"][0]

        assert "status" in summary
        assert "tool" in summary
        assert "jobs" in summary