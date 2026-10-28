"""Run synthetic failure/control checks in a disposable directory; no reviewer needed."""
import json
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import heldout_safe
import heldout_unsafe
import safe_config
import unsafe_config


def main() -> None:
    results = {}
    original = '{"theme": "old"}'
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "settings.json"
        for module in (unsafe_config, safe_config):
            path.write_text(original, encoding="utf-8")
            with patch.object(Path, "write_text", side_effect=OSError("synthetic write failure")):
                try:
                    module.replace_config(path, '{"theme": "new"}')
                except OSError:
                    pass
                else:
                    raise AssertionError("injected write failure did not occur")
            preserved = path.exists() and path.read_text(encoding="utf-8") == original
            results[module.__name__ + "_preserves_after_io_failure"] = preserved
            assert preserved == (module is safe_config)

        for module in (heldout_unsafe, heldout_safe):
            path.write_text(original, encoding="utf-8")
            try:
                module.store_settings(path, {"not_serializable": object()})
            except TypeError:
                pass
            else:
                raise AssertionError("unserializable input did not fail")
            preserved = path.read_text(encoding="utf-8") == original
            results[module.__name__ + "_preserves_after_serialization_failure"] = preserved
            assert preserved == (module is heldout_safe)

        path.write_text(original, encoding="utf-8")
        try:
            safe_config.replace_config(path, "invalid JSON")
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError("invalid JSON was accepted")
        assert path.read_text(encoding="utf-8") == original
        path.chmod(0o600)
        safe_config.replace_config(path, '{"theme": "new"}')
        assert safe_config.read_config(path) == {"theme": "new"}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        heldout_safe.store_settings(path, {"theme": "latest"})
        assert safe_config.read_config(path) == {"theme": "latest"}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        results["valid_updates_succeed"] = True
        results["existing_private_permissions_preserved"] = True
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
