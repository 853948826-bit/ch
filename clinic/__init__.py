from importlib.util import find_spec

AI_DEFENSE_INCLUDED = find_spec(f"{__name__}.defense_desktop") is not None
