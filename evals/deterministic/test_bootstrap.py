import subprocess
import sys


def test_entrypoint_has_no_configuration_or_network_requirement():
    result = subprocess.run(
        [sys.executable, "-m", "decagent", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "migrate" in result.stdout


def test_configuration_rejects_old_runtime_and_remote_http():
    import pytest

    from decagent.config import Settings

    with pytest.raises(ValueError, match="HTTPS"):
        Settings(public_url="http://example.com")
    with pytest.raises(ValueError, match="separate"):
        Settings(data_dir=".waku")
    with pytest.raises(ValueError, match="separate"):
        Settings(data_dir=".WAKU")
