"""
download_maplibre.py
下载 maplibre-gl.js，由 build.bat 调用
"""
import sys
import ssl
import urllib.request

VERSION = "4.7.1"  # 可根据需要修改版本号

URLS = [
    f"https://cdn.jsdelivr.net/npm/maplibre-gl@{VERSION}/dist/maplibre-gl.js",
    f"https://unpkg.com/maplibre-gl@{VERSION}/dist/maplibre-gl.js",
]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode    = ssl.CERT_NONE

opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ctx)
)

for url in URLS:
    try:
        print(f"  Trying {url}")
        with opener.open(url, timeout=30) as r:
            data = r.read()
        with open("maplibre-gl.js", "wb") as f:
            f.write(data)
        print(f"  Downloaded OK ({len(data)//1024} KB)")
        sys.exit(0)
    except Exception as e:
        print(f"  Failed: {e}")

print("  All downloads failed.")
sys.exit(1)
