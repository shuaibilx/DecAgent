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


def test_guest_trial_configuration_is_opt_in_and_strict(monkeypatch):
    import pytest

    from decagent.config import Settings

    assert Settings().guest_trial_enabled is False
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("DECAGENT_GUEST_TRIAL_ENABLED", "true")
    monkeypatch.delenv("DECAGENT_GUEST_RATE_LIMIT_HMAC_KEY", raising=False)
    with pytest.raises(ValueError, match="DECAGENT_GUEST_RATE_LIMIT_HMAC_KEY"):
        Settings.from_env()
    monkeypatch.setenv("DECAGENT_GUEST_RATE_LIMIT_HMAC_KEY", "ab" * 32)
    assert Settings.from_env().guest_trial_enabled is True
    monkeypatch.setenv("DECAGENT_GUEST_TRIAL_ENABLED", "yes")
    with pytest.raises(ValueError, match="must be true or false"):
        Settings.from_env()
