from setuptools import find_packages, setup

setup(
    name="mcp-research",
    version="0.0.0",
    packages=find_packages(),
    package_data={"": ["*.sh", "*.js", "*.json", "*.json.upstream", "cmp_blocklist.txt"]},
    install_requires=[
        "uvicorn[standard]",
        "mcp==1.29.1",
        "httpx",
        "PyJWT[crypto]",
        "requests[socks]",
        "trafilatura",
        "markitdown[all]",
        "fsspec",
        "python-multipart",
        "lxml_html_clean",
        "markdown-it-py",
        "linkify-it-py",
        "playwright==1.63.0",
        "msgpack",
        "cbor2",
        "bson",
        "py7zr",
        "rarfile",
        "blackboxprotobuf",
    ],
)
