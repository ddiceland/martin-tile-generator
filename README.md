## 功能
这是一个把Martin样式切成WMTS瓦片数据的工具，源代码在core里，打包的工具在package里。核心逻辑为：
1. 按 --bbox、--zoom 算出 Web Mercator 瓦片坐标；
2. 用 Playwright 启动无头 Chromium，把 MapLibre 注入页面；
3. 多进程并发渲染：页面只初始化一次，之后用 jumpTo() 切相机，比每张瓦片重载页面快很多；
4. Buffer 渲染：画布比瓦片大一圈（默认 128px），截图后再裁中心，避免标注被切断；
5. 定期重建 Page、崩溃重试、看门狗杀卡死进程；
6. 输出目录里写 WMTSCapabilities.xml。

## 文件
| | |
|-|-|
| `core/martinStyleToWMTS.py` | 切片生成的核心代码 |
| `core/download_maplibre.py` | 用来下载离线用的maplibre-gl.js |
| `core/maplibre-gl.js` | 已经下载好的MapLibre GL JS文件，版本为4.7.1 |
| `core/tile-renderer.spec` | pyinstaller配置文件，用来打包成package中的exe工具 |
| `package/browsers` | 无头浏览器 |
| `package/tile-renderer.exe` | 打包好的瓦片生成程序 |
| `package/maplibre-gl.js` | 已经下载好的MapLibre GL JS文件，版本为4.7.1 |


## 参数
```text
--style            Martin的样式地址
--zoom             层级缩放范围0~20，默认0,12
--bbox             切图范围，lon_min,lat_min,lon_max,lat_max
--output           输出目录，默认./tiles
--format           瓦片输出格式PNG/JPG，默认PNG
--jpg-quality      JPG质量1~100，默认85，仅当 --format jpg 时有效
--tile-size        瓦片输出尺寸，默认256x256
--buffer           扩展画布buffer，解决文字切断问题，默认128
--workers          并发进程数
--timeout          单张瓦片渲染超时秒数，默认30
--skip-existing    跳过已存在的瓦片
```

## 例子
```text
tile-renderer.exe ^
    --style "http://127.0.0.1:8080/styles/map_style_a.json" ^
    --zoom  1,14 ^
    --bbox  108.77507680,34.23640827,109.04684737,34.45557809 ^
    --output ./vector ^
	--format png ^
	--workers 4 ^
	--timeout 60 ^
	--skip-existing
```
