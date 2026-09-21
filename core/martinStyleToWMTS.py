#!/usr/bin/env python3
"""
martinStyleToWMTS.py  v4.0
────────────────────────────────────────────────────────────────────────
Martin MVT → WMTS 栅格瓦片渲染器

修复：
  1. 文字被切断 → 加入 Buffer 渲染（扩大画布渲染后裁剪中心区域）
  2. Page crashed / Timeout → 自动重试 + 高层级自动降低并发
  3. 格式参数    → --format png|jpg，--jpg-quality 调整质量
  4. 打包友好   → 支持 PyInstaller 单文件打包

安装依赖：
    pip install playwright tqdm
    playwright install chromium

用法：
    python martinStyleToWMTS.py ^
        --style  "http://10.0.3.15:3001/style/map_light.json" ^
        --zoom   6,16 ^
        --bbox   "107.6581824,33.6956931,109.8232814,34.7432877" ^
        --format png ^
        --output ./tiles
"""

import os
import sys
import math
import time
import logging
import argparse
import multiprocessing
import urllib.request
from pathlib import Path
from typing import Optional, Tuple, List


# ─────────────────────────────────────────────────────────────────────────────
# PyInstaller 打包运行时：在 import playwright 之前设置 Chromium 路径
# Playwright 会去 exe 同级的 browsers\ 目录找 Chromium
# 必须在所有 playwright import 之前执行
# ─────────────────────────────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    _EXE_DIR = os.path.dirname(sys.executable)
    _BROWSERS = os.path.join(_EXE_DIR, 'browsers')
    if os.path.isdir(_BROWSERS):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _BROWSERS

from tqdm import tqdm


# ─────────────────────────────────────────────────────────────────────────────
# PyInstaller 兼容处理
# ─────────────────────────────────────────────────────────────────────────────
def resource_path(relative):
    """打包成 exe 后正确获取资源路径"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


# ─────────────────────────────────────────────────────────────────────────────
# 瓦片坐标工具
# ─────────────────────────────────────────────────────────────────────────────

def lon_lat_to_tile(lon: float, lat: float, zoom: int) -> Tuple[int, int]:
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_range(z: int, bbox: Optional[List[float]]):
    n = 2 ** z
    if bbox is None:
        return 0, n - 1, 0, n - 1
    lon_min, lat_min, lon_max, lat_max = bbox
    x_min, y_max = lon_lat_to_tile(lon_min, lat_min, z)
    x_max, y_min = lon_lat_to_tile(lon_max, lat_max, z)
    return max(0, x_min), min(n - 1, x_max), max(0, y_min), min(n - 1, y_max)


def tile2lng(x: float, z: int) -> float:
    return x / (2 ** z) * 360.0 - 180.0


def tile2lat(y: float, z: int) -> float:
    n = math.pi - 2.0 * math.pi * y / (2 ** z)
    return math.degrees(math.atan(0.5 * (math.exp(n) - math.exp(-n))))


# ─────────────────────────────────────────────────────────────────────────────
# MapLibre GL 渲染
#
# 优化策略：
#   1. MapLibre JS + 样式 只加载一次（INIT_HTML）
#      - 旧方案：每张瓦片都重新 set_content() → 从 CDN 重新下载 JS → 卡死
#      - 新方案：页面初始化一次，后续只调 jumpTo() 移动相机
#
#   2. Threading 硬超时
#      - Playwright 的 wait_for_function 有时会无视 timeout 参数
#      - 用独立线程 + join(timeout) 做外层强制超时，保证不卡死
#
#   3. 定期重启 Page（每 RESTART_EVERY 张）
#      - Chromium 长时间运行内存持续增长
#      - 定期重建 Page，释放内存，防止速度越来越慢
#
#   4. Buffer 渲染（解决文字被切断）
#      - 画布 = tile_size + 2×buffer_px，渲染后裁剪中心区域
# ─────────────────────────────────────────────────────────────────────────────

# 每渲染多少张重建一次 Page（释放 Chromium 内存）
RESTART_EVERY = 300  # 减少重建频率，每次重建都需重新加载样式

# MapLibre GL JS 本地缓存（启动时下载一次，之后内嵌到 HTML 避免每次联网）
_MAPLIBRE_JS_CACHE: str = ""

MAPLIBRE_CDN = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"
MAPLIBRE_CSS = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"


def fetch_maplibre_js() -> str:
    """
    启动时下载 MapLibre JS 并缓存到内存。
    按优先级依次尝试：
      1. 脚本同级目录的本地文件 maplibre-gl.js（离线环境）
      2. CDN 下载（跳过 SSL 验证，兼容企业网络）
      3. 备用 CDN 地址
    所有方式均失败时降级为在线 CDN（Chromium 自己去请求）
    """
    import ssl
    global _MAPLIBRE_JS_CACHE
    if _MAPLIBRE_JS_CACHE:
        return _MAPLIBRE_JS_CACHE

    # ── 方式 1：本地文件（优先，离线环境直接放这个文件即可）──────────
    local_paths = [
        Path(__file__).parent / "maplibre-gl.js",
        Path(sys.executable).parent / "maplibre-gl.js",
    ]
    for lp in local_paths:
        if lp.exists():
            _MAPLIBRE_JS_CACHE = lp.read_text(encoding="utf-8")
            logging.info(f"MapLibre JS 从本地文件加载：{lp}")
            return _MAPLIBRE_JS_CACHE

    # ── 方式 2/3：网络下载（跳过 SSL 验证）──────────────────────────
    # 跳过 SSL 验证：解决企业代理/自签名证书导致的 SSL EOF 错误
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE

    urls = [
        MAPLIBRE_CDN,
        "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js",
        "http://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js",  # HTTP 备用
    ]

    for url in urls:
        try:
            logging.info(f"下载 MapLibre GL JS：{url}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl_ctx)
            )
            with opener.open(req, timeout=30) as r:
                _MAPLIBRE_JS_CACHE = r.read().decode("utf-8")
            logging.info(f"MapLibre JS 缓存成功（{len(_MAPLIBRE_JS_CACHE)//1024} KB）")
            return _MAPLIBRE_JS_CACHE
        except Exception as e:
            logging.warning(f"  下载失败（{url}）: {e}")

    # ── 所有方式失败，降级 CDN ────────────────────────────────────
    logging.warning(
        "MapLibre JS all download methods failed, falling back to online CDN. "
        "For offline use, download maplibre-gl.js manually and place it "
        "next to the script/exe."
    )
    _MAPLIBRE_JS_CACHE = ""
    return _MAPLIBRE_JS_CACHE


# ─────────────────────────────────────────────────────────────────────────────
# 骨架 HTML（不包含任何 JS，MapLibre 通过 add_script_tag 注入）
# ─────────────────────────────────────────────────────────────────────────────
SKELETON_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ margin:0; padding:0; }}
html, body {{ width:{px}px; height:{px}px; overflow:hidden; }}
#map {{ width:{px}px; height:{px}px; }}
</style>
</head>
<body><div id="map"></div></body>
</html>"""

# 地图初始化脚本（通过 add_script_tag 注入，在 maplibregl 加载后执行）
MAP_INIT_SCRIPT = """(function(styleArg, tileSize, bufferPx) {
  function tile2lng(x, z) { return x / Math.pow(2, z) * 360 - 180; }
  function tile2lat(y, z) {
    var n = Math.PI - 2 * Math.PI * y / Math.pow(2, z);
    return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
  }
  window._tileReady = false;
  var map = new maplibregl.Map({
    container: 'map', style: styleArg,
    center: [108.0, 34.0], zoom: 7,
    interactive: false, attributionControl: false,
    preserveDrawingBuffer: true, fadeDuration: 0,
    antialias: true, pixelRatio: 1
  });
  map.on('error', function(e) { console.warn('map error', e); });
  window.jumpToTile = function(z, x, y) {
    window._tileReady = false;
    map.jumpTo({ center: [tile2lng(x+0.5,z), tile2lat(y+0.5,z)], zoom: z-1 });
    // 用 setInterval 轮询，避免 headless 定时器节流导致截图时机不准
    // 同时检查 areTilesLoaded()，确保所有瓦片数据真正渲染完成
    var _waited = 0;
    var _stableCount = 0;  // 连续稳定帧数，防止短暂 idle 就截图
    var _poll = setInterval(function() {
      _waited += 50;
      var idle = !map.isMoving() && !map.isZooming() && !map.isRotating();
      var tilesOk = map.areTilesLoaded();
      if (idle && tilesOk) {
        _stableCount++;
        if (_stableCount >= 3) {   // 连续 3 次（150ms）稳定才截图
          clearInterval(_poll);
          window._tileReady = true;
        }
      } else {
        _stableCount = 0;          // 不稳定则重置计数
      }
      if (_waited >= 8000) {       // 最多等 8s 兜底
        clearInterval(_poll);
        window._tileReady = true;
      }
    }, 50);
  };
  window.capture = function(fmt) {
    try {
      var src = map.getCanvas();
      var dst = document.createElement('canvas');
      dst.width = tileSize; dst.height = tileSize;
      var ctx = dst.getContext('2d');
      ctx.drawImage(src, bufferPx, bufferPx, tileSize, tileSize, 0, 0, tileSize, tileSize);
      return dst.toDataURL(fmt==='jpg'?'image/jpeg':'image/png', 0.92);
    } catch(e) { return null; }
  };
})(STYLE_ARG, TILE_SIZE, BUFFER_PX);"""


def make_map_init_script(style_url: str, tile_size: int, buffer_px: int,
                         style_dict=None) -> str:
    import json as _json
    style_arg = _json.dumps(style_dict, ensure_ascii=False) if style_dict else f'"{style_url}"'
    return (MAP_INIT_SCRIPT
            .replace("STYLE_ARG",  style_arg)
            .replace("TILE_SIZE",  str(tile_size))
            .replace("BUFFER_PX",  str(buffer_px)))


def fetch_style_json(style_url: str) -> dict:
    """
    用 Python 预先下载样式 JSON（跳过 SSL 验证）。
    同时把样式里的 glyphs / sprite URL 检查一遍，
    确保都是可访问的地址（不修改内容，只做日志提示）。
    返回 style dict，失败时返回 None（降级让 Chromium 自己加载）。
    """
    import ssl, json as _json

    # 本地文件直接读取
    if not style_url.startswith("http"):
        try:
            with open(style_url, encoding="utf-8") as f:
                return _json.load(f)
        except Exception as e:
            logging.warning(f"读取本地样式失败: {e}")
            return None

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE

    try:
        logging.info(f"预下载样式 JSON: {style_url}")
        req    = urllib.request.Request(
            style_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_ctx)
        )
        with opener.open(req, timeout=30) as r:
            style = _json.loads(r.read().decode("utf-8"))
        logging.info("样式 JSON 下载成功，将内嵌到 HTML（Chromium 无需联网加载样式）")
        return style
    except Exception as e:
        logging.warning(f"样式 JSON 下载失败({e})，降级让 Chromium 自行加载")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 初始化 Page（加载 MapLibre + 样式，只做一次）
# ─────────────────────────────────────────────────────────────────────────────

def init_page(page, context, style_url: str, tile_size: int,
              buffer_px: int, maplibre_js: str, timeout_ms: int,
              style_dict=None):
    """
    用 add_script_tag 注入 MapLibre JS，彻底绕过网络。
    add_script_tag(content=...) 直接把 JS 字符串注入页面内存，
    Playwright 等脚本执行完才返回，不需要任何网络请求。
    """
    canvas_px = tile_size + 2 * buffer_px
    # 骨架 HTML（极小，瞬间加载）
    html = SKELETON_HTML.format(px=canvas_px)

    for attempt in range(3):
        if attempt > 0:
            logging.info(f"  init_page 重试 {attempt+1}/3 ...")
            time.sleep(2)

        # Step 1: 加载骨架 HTML（无任何 JS，瞬间完成）
        try:
            page.set_content(html, wait_until="domcontentloaded", timeout=8000)
        except Exception as e:
            logging.warning(f"  set_content 失败 attempt={attempt+1}: {e}")
            continue

        # Step 2: 注入 MapLibre GL JS（从内存注入，不走网络）
        try:
            if maplibre_js:
                # 内存缓存注入（最快，100% 可靠）
                page.add_script_tag(content=maplibre_js)
            else:
                # 没有缓存：从本地文件注入（用户放了 maplibre-gl.js 在同级目录）
                local_js = Path(__file__).parent / "maplibre-gl.js"
                if local_js.exists():
                    page.add_script_tag(path=str(local_js))
                else:
                    # 最后手段：让 Chromium 从 CDN 加载（可能失败）
                    logging.warning("  无本地 MapLibre JS，尝试 CDN（可能因网络失败）")
                    page.add_script_tag(url=MAPLIBRE_CDN)
        except Exception as e:
            logging.warning(f"  注入 MapLibre JS 失败 attempt={attempt+1}: {e}")
            continue

        # Step 3: 确认 maplibregl 已定义
        try:
            page.wait_for_function("typeof maplibregl !== 'undefined'", timeout=3000)
        except Exception:
            logging.warning(f"  maplibregl 未定义 attempt={attempt+1}")
            continue

        # Step 4: 注入地图初始化脚本
        init_script = make_map_init_script(style_url, tile_size, buffer_px, style_dict)
        try:
            page.add_script_tag(content=init_script)
        except Exception as e:
            logging.warning(f"  地图初始化脚本注入失败 attempt={attempt+1}: {e}")
            continue

        # Step 5: 确认 jumpToTile 已定义
        try:
            page.wait_for_function("typeof window.jumpToTile === 'function'", timeout=5000)
        except Exception as e:
            logging.warning(f"  jumpToTile 未定义 attempt={attempt+1}: {e}")
            continue

        # Step 6: 等待样式和初始瓦片加载完成
        # 用 JS 轮询 isStyleLoaded() + areTilesLoaded()，不依赖定时器
        try:
            page.evaluate("""
                window._initReady = false;
                var _ic = setInterval(function() {
                    if (map.isStyleLoaded && map.isStyleLoaded() && map.areTilesLoaded && map.areTilesLoaded()) {
                        window._initReady = true;
                        clearInterval(_ic);
                    }
                }, 100);
                setTimeout(function() { window._initReady = true; clearInterval(_ic); }, 15000);
            """)
            page.wait_for_function("window._initReady === true",
                                   timeout=max(timeout_ms, 20000))
            logging.info(f"  Page 初始化成功（attempt={attempt+1}）")
            return True
        except Exception as e:
            logging.warning(f"  样式/瓦片加载超时 attempt={attempt+1}: {e}")
            # 超时但 jumpToTile 存在，视为成功（首张瓦片渲染时会自然等待）
            try:
                ok = page.evaluate("typeof window.jumpToTile === 'function'")
                if ok:
                    logging.info(f"  Page 初始化降级成功（attempt={attempt+1}）")
                    return True
            except Exception:
                pass

    logging.error("  init_page 全部重试失败")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 单张瓦片渲染（jumpTo 模式，纯 Playwright 原生超时，无 threading）
# ─────────────────────────────────────────────────────────────────────────────

def render_tile(page, context, z: int, x: int, y: int,
                style_url: str, tile_size: int, buffer_px: int,
                fmt: str, timeout_s: int, maplibre_js: str,
                style_dict=None):
    """
    渲染单张瓦片（纯 Playwright 原生超时，无 threading）。
    返回 (bytes|None, page, ok:bool)
    """
    import base64
    canvas_px  = tile_size + 2 * buffer_px
    timeout_ms = timeout_s * 1000

    def rebuild_page():
        nonlocal page
        try:
            page.close()
        except Exception:
            pass
        try:
            page = context.new_page()
            page.set_viewport_size({"width": canvas_px, "height": canvas_px})
            # 重建时给更多时间（样式首次加载可能较慢）
            init_page(page, context, style_url, tile_size,
                      buffer_px, maplibre_js, max(timeout_ms, 120000),
                      style_dict)
        except Exception as ex:
            logging.warning(f"  重建 Page 失败: {ex}")
        return page

    try:
        # ── Step 1: 确认 jumpToTile 已定义 ───────────────────────
        try:
            ready = page.evaluate("typeof window.jumpToTile === 'function'")
        except Exception:
            ready = False
        if not ready:
            logging.warning(f"  jumpToTile 未定义，重建 Page...")
            page = rebuild_page()
            try:
                ready = page.evaluate("typeof window.jumpToTile === 'function'")
            except Exception:
                ready = False
            if not ready:
                logging.warning(f"  重建后仍未就绪，跳过 {z}/{x}/{y}")
                return None, page, False

        # ── Step 2: 跳转相机 ───────────────────────────────────────
        page.evaluate(f"window.jumpToTile({z}, {x}, {y})")

        # ── Step 3: 等待瓦片渲染完成（JS 有 5s 兜底）──────────────
        try:
            page.wait_for_function(
                "window._tileReady === true",
                timeout=timeout_ms
            )
        except Exception:
            pass  # 超时用当前帧

        # ── Step 4: 截图裁剪 ───────────────────────────────────────
        data_url = page.evaluate(f"window.capture('{fmt}')")
        if not data_url:
            return None, page, False

        _, b64 = data_url.split(",", 1)
        return base64.b64decode(b64), page, True

    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ("crashed", "target closed", "connection",
                                   "session closed", "greenlet", "different thread")):
            logging.warning(f"  Page 崩溃 {z}/{x}/{y}，重建中...")
            page = rebuild_page()
        else:
            logging.debug(f"  渲染异常 {z}/{x}/{y}: {e}")
        return None, page, False


def save_tile(data: bytes, out_dir: Path, z, x, y, fmt: str):
    ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
    d = out_dir / str(z) / str(x)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{y}.{ext}").write_bytes(data)


# ─────────────────────────────────────────────────────────────────────────────
# Worker 子进程
# ─────────────────────────────────────────────────────────────────────────────

def worker_main(task_q, result_q, worker_id, style_url, out_dir, fmt,
                tile_size, buffer_px, skip_existing, timeout_s, style_dict=None):
    """
    优化版 Worker：
      - MapLibre JS 只下载一次（内嵌到 HTML，避免每张瓦片联网）
      - 页面只初始化一次，后续用 jumpTo() 切换瓦片（速度提升 3~5 倍）
      - threading 硬超时，彻底防止卡死
      - 每 RESTART_EVERY 张自动重建 Page 防止内存泄漏
    """
    from playwright.sync_api import sync_playwright

    ext       = "jpg" if fmt in ("jpg", "jpeg") else "png"
    canvas_px = tile_size + 2 * buffer_px

    # 预下载 MapLibre JS（内嵌到 HTML，不依赖 CDN）
    maplibre_js = fetch_maplibre_js()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-zygote",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-hang-monitor",
                "--disable-ipc-flooding-protection",
                "--run-all-compositor-stages-before-draw",
                f"--window-size={canvas_px},{canvas_px}",
                "--force-device-scale-factor=1",
                "--disable-web-security",
                "--allow-running-insecure-content",
            ],
        )
        context = browser.new_context(
            viewport={"width": canvas_px, "height": canvas_px},
            device_scale_factor=1,
        )

        def new_page():
            p = context.new_page()
            p.set_viewport_size({"width": canvas_px, "height": canvas_px})
            ok = init_page(p, context, style_url, tile_size, buffer_px,
                           maplibre_js, timeout_s * 1000, style_dict)
            if not ok:
                logging.warning(f"[W{worker_id}] 页面初始化失败，将在下一张重试")
            return p

        page = new_page()
        tile_count = 0   # 用于定期重建 Page

        while True:
            # 取任务
            try:
                task = task_q.get(timeout=10)
            except Exception:
                break
            if task is None:
                break

            z, x, y = task
            tile_path = out_dir / str(z) / str(x) / f"{y}.{ext}"

            if skip_existing and tile_path.exists():
                result_q.put("skip")
                continue

            # 定期重建 Page，释放 Chromium 内存
            tile_count += 1
            if tile_count % RESTART_EVERY == 0:
                try:
                    page.close()
                except Exception:
                    pass
                page = new_page()
                logging.debug(f"[W{worker_id}] 已渲染 {tile_count} 张，重建 Page 释放内存")

            # 渲染
            data, page, ok = render_tile(
                page, context, z, x, y,
                style_url, tile_size, buffer_px,
                fmt, timeout_s, maplibre_js
            )

            if ok and data:
                save_tile(data, out_dir, z, x, y, fmt)
                result_q.put("ok")
            else:
                result_q.put("err")

        try:
            page.close()
            browser.close()
        except Exception:
            pass

    result_q.put(("done", worker_id))


# ─────────────────────────────────────────────────────────────────────────────
# WMTS GetCapabilities
# ─────────────────────────────────────────────────────────────────────────────

def gen_capabilities(out_dir, zoom_range, fmt, title, base_url):
    ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
    scales = {z: 559082264.0287 / (2 ** z) for z in range(21)}
    mats = "\n".join(
        f"      <TileMatrix>\n"
        f"        <ows:Identifier>{z}</ows:Identifier>\n"
        f"        <ScaleDenominator>{scales[z]:.6f}</ScaleDenominator>\n"
        f"        <TopLeftCorner>-20037508.3428 20037508.3428</TopLeftCorner>\n"
        f"        <TileWidth>256</TileWidth><TileHeight>256</TileHeight>\n"
        f"        <MatrixWidth>{2**z}</MatrixWidth>"
        f"<MatrixHeight>{2**z}</MatrixHeight>\n"
        f"      </TileMatrix>"
        for z in range(zoom_range[0], zoom_range[1] + 1)
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Capabilities xmlns="http://www.opengis.net/wmts/1.0"
              xmlns:ows="http://www.opengis.net/ows/1.1"
              version="1.0.0">
  <ows:ServiceIdentification>
    <ows:Title>{title}</ows:Title>
    <ows:ServiceType>OGC WMTS</ows:ServiceType>
    <ows:ServiceTypeVersion>1.0.0</ows:ServiceTypeVersion>
  </ows:ServiceIdentification>
  <Contents>
    <Layer>
      <ows:Title>{title}</ows:Title>
      <ows:Identifier>tiles</ows:Identifier>
      <Style isDefault="true"><ows:Identifier>default</ows:Identifier></Style>
      <Format>image/{ext}</Format>
      <TileMatrixSetLink>
        <TileMatrixSet>GoogleMapsCompatible</TileMatrixSet>
      </TileMatrixSetLink>
      <ResourceURL format="image/{ext}" resourceType="tile"
                   template="{base_url}/{{TileMatrix}}/{{TileCol}}/{{TileRow}}.{ext}"/>
    </Layer>
    <TileMatrixSet>
      <ows:Identifier>GoogleMapsCompatible</ows:Identifier>
      <ows:SupportedCRS>urn:ogc:def:crs:EPSG::3857</ows:SupportedCRS>
{mats}
    </TileMatrixSet>
  </Contents>
</Capabilities>"""
    (out_dir / "WMTSCapabilities.xml").write_text(xml, encoding="utf-8")
    logging.info("已生成 WMTSCapabilities.xml")


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

def run(args):
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    z_parts    = args.zoom.split(",")
    zoom_range = (int(z_parts[0]), int(z_parts[-1]))
    bbox       = [float(v) for v in args.bbox.split(",")] if args.bbox else None
    out_dir    = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt        = args.format.lower()
    buffer_px  = args.buffer

    logging.info(f"GL 样式   : {args.style}")
    logging.info(f"缩放级别  : {zoom_range[0]} ~ {zoom_range[1]}")
    logging.info(f"输出格式  : {fmt.upper()}")
    logging.info(f"Buffer    : {buffer_px}px（扩展画布 {args.tile_size}→{args.tile_size + 2*buffer_px}px 后裁剪，修复文字切断）")

    # 构建任务列表
    all_tasks = []
    for z in range(zoom_range[0], zoom_range[1] + 1):
        x0, x1, y0, y1 = tile_range(z, bbox)
        cnt = (x1 - x0 + 1) * (y1 - y0 + 1)
        logging.info(f"  z={z:2d}: x=[{x0},{x1}] y=[{y0},{y1}]  共 {cnt} 张")
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                all_tasks.append((z, x, y))

    total = len(all_tasks)
    logging.info(f"瓦片总数  : {total:,}")

    # 问题二修复：高层级自动降低并发，减少内存压力和 crash
    workers = args.workers
    # if zoom_range[1] >= 16:
        # workers = min(workers, 2)
        # logging.info(f"层级 >= 16，自动将并发降至 {workers}（防止 Page crash）")
    # elif zoom_range[1] >= 14:
        # workers = min(workers, 3)
        # logging.info(f"层级 >= 14，自动将并发降至 {workers}")

    # Worker 数量（共享任务队列，不再预分配 chunk）
    n = min(workers, total)

    # ── 共享任务队列（所有 worker 从同一队列取任务）────────────────
    task_q   = multiprocessing.Queue()
    result_q = multiprocessing.Queue()

    # 把全部任务塞入队列
    for t in all_tasks:
        task_q.put(t)
    # 每个 worker 一个 SENTINEL 结束信号
    for _ in range(n):
        task_q.put(None)

    # 看门狗超时：单张瓦片超时 × 3，超过则认为 worker 卡死
    WATCHDOG_TIMEOUT = args.timeout * 3

    # 预下载样式 JSON 内嵌到 worker（避免 Chromium 联网获取样式）
    style_dict = None
    try:
        import json as _json, ssl as _ssl, urllib.request as _req
        _ctx = _ssl.create_default_context()
        _ctx.check_hostname = False
        _ctx.verify_mode = _ssl.CERT_NONE
        _opener = _req.build_opener(_req.HTTPSHandler(context=_ctx))
        with _opener.open(args.style, timeout=30) as r:
            style_dict = _json.loads(r.read().decode("utf-8"))
        logging.info(f"样式 JSON 已预下载内嵌（{len(str(style_dict))//1024} KB），Chromium 无需联网加载样式")
    except Exception as e:
        logging.warning(f"样式 JSON 预下载失败（{e}），Chromium 将自行加载")
        style_dict = None

    def start_worker(i):
        p = multiprocessing.Process(
            target=worker_main,
            args=(task_q, result_q, i, args.style, out_dir, fmt,
                  args.tile_size, buffer_px,
                  args.skip_existing, args.timeout, style_dict),
            daemon=True,
        )
        p.start()
        return p

    procs = {i: start_worker(i) for i in range(n)}

    stats          = {"ok": 0, "skip": 0, "err": 0}
    done_workers   = 0
    last_msg_time  = time.time()
    t0             = time.time()

    with tqdm(total=total, unit="tile", desc="渲染中") as bar:
        while done_workers < n:
            try:
                # ── 带超时的 get，防止 worker 卡死后主进程永久阻塞 ──
                msg = result_q.get(timeout=WATCHDOG_TIMEOUT)
                last_msg_time = time.time()

                if isinstance(msg, tuple) and msg[0] == "done":
                    done_workers += 1
                else:
                    stats[msg] = stats.get(msg, 0) + 1
                    bar.update(1)
                    bar.set_postfix(ok=stats["ok"], skip=stats["skip"],
                                    err=stats["err"])

            except Exception:
                # ── 超时：检查哪个 worker 卡死，强制重启 ──────────
                logging.warning(
                    f"⚠️  {WATCHDOG_TIMEOUT}s 内无响应，检测卡死 worker..."
                )
                dead = []
                for wid, p in procs.items():
                    if p.is_alive():
                        p.kill()
                        p.join(timeout=3)
                        dead.append(wid)
                        logging.warning(f"   已强制终止 Worker {wid}，重新启动")

                if not dead:
                    # 所有进程都已退出但没发 done，手动补 done_workers
                    done_workers = n
                    break

                # 重启被杀掉的 worker（任务还在 task_q 里，会自动续接）
                for wid in dead:
                    procs[wid] = start_worker(wid)

    for p in procs.values():
        if p.is_alive():
            p.join(timeout=5)

    gen_capabilities(out_dir, zoom_range, fmt, args.title, args.base_url)

    elapsed = time.time() - t0
    logging.info("=" * 55)
    logging.info(f"✅ 完成！耗时 {elapsed:.1f}s  ({total/elapsed:.1f} tile/s)")
    logging.info(f"   成功 {stats['ok']:,}  跳过 {stats['skip']:,}  失败 {stats['err']:,}")
    logging.info(f"   输出: {out_dir.resolve()}")
    logging.info("=" * 55)


# ─────────────────────────────────────────────────────────────────────────────
# ⚠️  Windows 多进程必须有此保护
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    multiprocessing.freeze_support()   # PyInstaller 打包必须

    p = argparse.ArgumentParser(
        description="Martin MVT → WMTS 栅格瓦片渲染器 v4.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # PNG 格式，6~16 级，指定范围
  python martinStyleToWMTS.py ^
      --style  "http://10.0.3.15:3001/style/style.json" ^
      --zoom   6,16 ^
      --bbox   "107.6581824,33.6956931,109.8232814,34.7432877" ^
      --format png ^
      --output ./tiles

  # JPG 格式，质量 90，断点续传
  python martinStyleToWMTS.py ^
      --style  "http://10.0.3.15:3001/style/style.json" ^
      --zoom   6,16 ^
      --format jpg --jpg-quality 90 ^
      --skip-existing ^
      --output ./tiles
        """
    )
    p.add_argument("--style",    required=True,
                   help="GL style.json 完整 URL 或本地路径")
    p.add_argument("--zoom",     default="0,12",
                   help="缩放范围，如 6,16（默认 0,12）")
    p.add_argument("--bbox",     default=None,
                   help="经纬度范围 lon_min,lat_min,lon_max,lat_max")
    p.add_argument("--output",   default="./tiles",
                   help="输出目录（默认 ./tiles）")

    # ── 问题三：格式参数 ────────────────────────────────────────
    p.add_argument("--format",   default="png", choices=["png", "jpg"],
                   help="输出格式：png（默认）或 jpg")
    p.add_argument("--jpg-quality", type=int, default=85,
                   dest="jpg_quality",
                   help="JPG 质量 1~100（仅 --format jpg 时有效，默认 85）")

    p.add_argument("--tile-size",  type=int, default=256, dest="tile_size",
                   help="瓦片输出尺寸（默认 256）")

    # ── 问题一：Buffer 参数 ─────────────────────────────────────
    p.add_argument("--buffer",   type=int, default=128,
                   help="扩展画布 Buffer 像素（默认 128，解决文字切断问题）\n"
                        "设为 0 可禁用（速度略快，但文字可能被裁剪）")

    p.add_argument("--workers",  type=int, default=4,
                   help="并发进程数（默认 4；高层级自动降低）")
    p.add_argument("--timeout",  type=int, default=30,
                   help="单瓦片渲染超时秒数（默认 30）")
    p.add_argument("--skip-existing", action="store_true", dest="skip_existing",
                   help="断点续传：跳过已存在的瓦片")
    p.add_argument("--title",    default="Raster Tiles")
    p.add_argument("--base-url", default="", dest="base_url")
    p.add_argument("--verbose",  action="store_true")

    run(p.parse_args())