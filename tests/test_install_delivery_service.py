import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "scripts" / "install_delivery_service.py"


def load():
    spec = importlib.util.spec_from_file_location("install_delivery_service", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_controller_runs_under_the_interpreter_that_installed_it():
    # A hard-coded ROOT/.venv breaks in worktrees and on the corrupted .venv.
    args = load().build_config()["ProgramArguments"]
    assert args[0] == sys.executable
    assert args[1].endswith("controller.py")
