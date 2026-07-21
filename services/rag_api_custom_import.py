import importlib.util
from pathlib import Path


def load_batch_authorization():
    path = Path(__file__).parent / "rag-api-custom" / "batch_authorization.py"
    spec = importlib.util.spec_from_file_location("batch_authorization", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
