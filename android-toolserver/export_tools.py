"""Write the tool list for the gateway (mcp-research/mcp_research/gateway/android-tools.json). The gateway lists tools without a
running slot; test_android_toolserver.py fails if the file is out of date. Run: python export_tools.py"""

import json
import os

import android_tools

PATH = os.path.join(os.path.dirname(__file__), "..", "mcp-research", "mcp_research", "gateway", "android-tools.json")


def render() -> str:
    return json.dumps([t.model_dump(mode="json", exclude_none=True) for t in android_tools.build_tools()], indent=1) + "\n"


if __name__ == "__main__":
    open(PATH, "w").write(render())
