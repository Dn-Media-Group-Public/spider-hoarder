import os


def pytest_ignore_collect(collection_path, config):
    """test_android.py sets MODE=android before importing the gateway, which would leak into every other test of the
    same run: only collect it when this directory is named on the command line (`pytest tests/android`)."""
    return not any(os.path.abspath(a.split("::")[0]).startswith(os.path.dirname(__file__)) for a in config.args)
