"""The older tests script untagged model text, so they run with the <say> channel off (the rollback setting).
The channel tests (test_say_channel.py) turn it on for themselves."""
import atexit
import shutil
import tempfile
from pathlib import Path

from receptionist import config as _config

# Every config a test loads points at one throwaway data folder, so no test can open data/receptionist.sqlite, the spend
# ledger, the outbound state or the clients folder of the real project (W1.5). Tests that want their own folder still
# pass data_dir explicitly.
TEST_DATA = tempfile.mkdtemp(prefix="receptionist-tests-")
atexit.register(shutil.rmtree, TEST_DATA, ignore_errors=True)
REAL_DATA = str(Path(_config.ROOT / "data").resolve())

_load = _config.load_config


def _legacy_load(*args, **kwargs):
    cfg = _load(*args, **kwargs)
    cfg.setdefault("agent", {})["speech_channel"] = False
    if Path(cfg["data_dir"]).resolve() == Path(REAL_DATA):   # not when a test points the config at its own folder
        cfg["data_dir"] = TEST_DATA
        cfg["clients_dir"] = str(Path(TEST_DATA) / "clients")
    return cfg


_config.load_config = _legacy_load
