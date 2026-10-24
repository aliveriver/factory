"""Load the plugin under its install name regardless of the clone directory."""

import importlib.util
import sys
from pathlib import Path

plugin_root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_research",
    plugin_root / "__init__.py",
    submodule_search_locations=[str(plugin_root)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
