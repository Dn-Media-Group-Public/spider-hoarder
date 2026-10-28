from setuptools import find_packages, setup

# Dependencies are installed from requirements.txt first (Docker layer caching), so none are declared here.
setup(
    name="mcp-research",
    version="0.0.0",
    packages=find_packages(),
    package_data={"": ["*.sh", "*.js", "*.json", "*.json.upstream", "cmp_blocklist.txt"]},
)
