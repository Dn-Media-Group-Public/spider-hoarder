import os


def pytest_ignore_collect(collection_path, config):
    """test_android.py sets BACKEND=android_clear (and the slot env) before importing the gateway, which would leak into every other test of the
    same run: only collect it when this directory is named on the command line (`pytest backends/android-sdk/tests`)."""
    return not any(os.path.abspath(a.split("::")[0]).startswith(os.path.dirname(__file__)) for a in config.args)


import pytest


@pytest.fixture(scope="session", autouse=True)
def _parser_service():
    """Tool outputs are parsed by the parser service (docs/plans/parser-wrapper.md): a real wrapper in a thread, on the test store."""
    from spider_hoarder_sdk import testing
    testing.start_parser()
