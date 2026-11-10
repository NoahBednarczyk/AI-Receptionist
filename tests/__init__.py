"""The older tests script untagged model text, so they run with the <say> channel off (the rollback setting).
The channel tests (test_say_channel.py) turn it on for themselves."""
from receptionist import config as _config

_load = _config.load_config


def _legacy_load(*args, **kwargs):
    cfg = _load(*args, **kwargs)
    cfg.setdefault("agent", {})["speech_channel"] = False
    return cfg


_config.load_config = _legacy_load
