import stat
import tomllib
from pathlib import Path

from milo import config


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_mask_shows_only_ends():
    assert config.mask("AIzaSyD4k3xAmpLeKeyThatIsLongEnough_x9Q") == "AIza…x9Q"


def test_mask_hides_short_and_missing_keys():
    assert config.mask("abc123") == "•••"
    assert config.mask("") == "–"
    assert config.mask(None) == "–"


def test_load_with_nothing_configured(home):
    cfg = config.load()
    assert cfg.key(config.PLACES) is None
    assert cfg.key(config.YOUTUBE) is None
    assert cfg.problem is None


def test_load_reads_the_config_file(home):
    config.save({"google_places_api_key": "places-from-file-123"})
    cfg = config.load()
    assert cfg.key(config.PLACES) == "places-from-file-123"
    assert cfg.source(config.PLACES) == "file"


def test_env_var_overrides_file(home):
    config.save({"youtube_api_key": "youtube-from-file-123"})
    cfg = config.load(env={"MILO_YOUTUBE_KEY": "youtube-from-env-456"})
    assert cfg.key(config.YOUTUBE) == "youtube-from-env-456"
    assert cfg.source(config.YOUTUBE) == "env"


def test_blank_env_var_does_not_override(home):
    config.save({"youtube_api_key": "youtube-from-file-123"})
    cfg = config.load(env={"MILO_YOUTUBE_KEY": "  "})
    assert cfg.key(config.YOUTUBE) == "youtube-from-file-123"


def test_save_creates_private_dir_and_file(home):
    path = config.save({"google_places_api_key": "k" * 39})
    assert path == home / ".config" / "milo" / "config.toml"
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700


def test_save_tightens_an_existing_loose_file(home):
    path = config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text('youtube_api_key = "old"\n')
    path.chmod(0o644)
    config.save({"google_places_api_key": "new"})
    assert mode(path) == 0o600


def test_save_merges_and_keeps_unknown_settings(home):
    path = config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text('youtube_api_key = "yt"\nfuture_setting = true\n')
    config.save({"google_places_api_key": "gp"})
    data = tomllib.loads(path.read_text())
    assert data == {"youtube_api_key": "yt", "future_setting": True, "google_places_api_key": "gp"}


def test_broken_file_is_reported_not_raised(home):
    path = config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text("this is = = not toml")
    cfg = config.load(env={"MILO_PLACES_KEY": "from-env"})
    assert cfg.problem and "isn't valid TOML" in cfg.problem
    assert cfg.key(config.PLACES) == "from-env"  # env keys still work


def test_save_replaces_a_broken_file(home):
    path = config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text("not [valid")
    config.save({"youtube_api_key": "fresh"})
    assert config.load().key(config.YOUTUBE) == "fresh"


def test_file_mode_and_privacy(home):
    assert config.file_mode(config.config_path()) is None
    assert config.is_private(0o600)
    assert config.is_private(0o400)
    assert not config.is_private(0o644)
