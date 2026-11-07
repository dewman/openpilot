"""BP overrides must accept the calls their current base classes make."""
import importlib
import inspect
from pathlib import Path


def test_mici_onroad_overrides_accept_base_required_arguments():
  root = Path(__file__).resolve().parents[2]
  checked = 0
  for path in (root / 'openpilot/selfdrive/ui/bp/mici/onroad').glob('*.py'):
    if path.name == '__init__.py':
      continue
    module = importlib.import_module('.'.join(path.relative_to(root).with_suffix('').parts))
    for cls in vars(module).values():
      if not inspect.isclass(cls) or cls.__module__ != module.__name__:
        continue
      for name, override in cls.__dict__.items():
        if name.startswith('__') or not inspect.isfunction(override):
          continue
        parent = next((base.__dict__[name] for base in cls.__mro__[1:] if name in base.__dict__), None)
        if not inspect.isfunction(parent):
          continue
        signature = inspect.signature(parent)
        if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in signature.parameters.values()):
          continue
        args, kwargs = [], {}
        for param in signature.parameters.values():
          if param.default is not param.empty:
            continue
          if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            args.append(object())
          elif param.kind == param.KEYWORD_ONLY:
            kwargs[param.name] = object()
        try:
          inspect.signature(override).bind(*args, **kwargs)
        except TypeError as exc:
          raise AssertionError(f'{cls.__name__}.{name} rejects base call {signature}: {exc}') from exc
        checked += 1
  assert checked > 15
