# PngTile2Dem (QGIS Plugin)

![Plugin overview](images/demo.gif)

## English

PngTile2Dem is a QGIS plugin that downloads numeric elevation WebP tiles provided by the National Land Information Division (GSI Japan, through Zenkoku Q Chizu), decodes RGB values into real elevation values, mosaics the tiles, and exports a DEM GeoTIFF in any CRS.

This plugin is optimized for:
- Fast parallel tile downloads
- Accurate RGB → elevation decoding
- Efficient VRT-based mosaicking
- Reliable GeoTIFF output with overviews for smooth display
- Output CRS selection (EPSG:4326, EPSG:3857, Japan Plane Rectangular CS 1–19, etc.)

---

## Features

- ✔ Download WebP numeric DEM tiles (Zoom level 17)
- ✔ Decode RGB-coded elevation values
- ✔ Mosaic tiles using GDAL VRT
- ✔ Reproject to any CRS
- ✔ GeoTIFF output with tiling, compression, overviews
- ✔ Multithreaded processing
- ✔ Estimated processing time before execution

Tile Source:  
Zenkoku Q Chizu
https://mapdata.qchizu.xyz/03_dem/52_gsi/all_2025/1_02/{z}/{x}/{y}.webp

DEM5A
https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png

DEM5B
https://cyberjapandata.gsi.go.jp/xyz/dem5b_png/{z}/{x}/{y}.png

DEM5C
https://cyberjapandata.gsi.go.jp/xyz/dem5c_png/{z}/{x}/{y}.png

DEM10B
https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png

---

## Installation

1. Download the ZIP file of this repository.
2. Open QGIS → **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Select the ZIP file and install the plugin.
4. The plugin appears under **Processing Toolbox → DEM Tools → PngTile2Dem**.

---

## Usage

1. Define an extraction extent in the map canvas.
2. Select output CRS.
3. Choose output GeoTIFF file.
4. Run the tool.

The plugin will:
- Download necessary tiles
- Build a VRT
- Warp to the final CRS
- Save DEM GeoTIFF
- Add the layer automatically

---

## Screenshots

### Plugin execution example
![Plugin demo](images/demo.gif)

*Example: Running the plugin and displaying the generated DEM in QGIS.*

### Plugin image
![Plugin image](images/screenshot_plugin.png)

### Processing tool dialog
![Processing tool dialog](images/screenshot_tool.png)

### Output DEM example
![Output DEM example](images/screenshot_result.png)

---

## Notes

- Recommended maximum area: ≤ ~20,000 tiles  
  (QGIS / GDAL performance may degrade beyond this)
- Downloads are performed in parallel (up to 16 threads)
- Output tiles use DEFLATE compression + internal tiling

---

## License

This plugin is released under the MIT License.  
See `LICENSE` for details.

---

### Acknowledgement

This plugin and its source code were developed and refined with the assistance of
**ChatGPT (OpenAI)**, which was used to support algorithm design, debugging,
code refactoring, and documentation writing.

All final design decisions, testing, and validation were performed by the author.

---

# 日本語 — Japanese

PngTile2Dem は、全国Q地図から提供されている **数値標高 WebP タイル** を任意範囲で取得し、RGB から標高値へ復号化し、モザイクした上で **GeoTIFF（DEM）** として出力する QGIS プラグインです。全国Q地図のタイルが指定した範囲の一部で存在しない場合には国土地理院 DEM5AやDEM5B、DEM5C、DEM10Bで補完されます。
---

## 主な機能

- ✔ 数値標高 WebP タイル（ZL17）の取得  
- ✔ RGB から標高値（実数）への変換 
- ✔ ピクセル単位の欠損補完
- ✔ ズーム差（Z15 → Z17）の自動補正 
- ✔ GDAL VRT による高速モザイク  
- ✔ 任意座標系へ再投影  
- ✔ GeoTIFF のタイル化 + 圧縮 + オーバービュー生成  
- ✔ マルチスレッド高速処理  

タイル提供元：  
全国Q地図タイル
https://mapdata.qchizu.xyz/03_dem/52_gsi/all_2025/1_02/{z}/{x}/{y}.webp

DEM5A
https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png

DEM5B
https://cyberjapandata.gsi.go.jp/xyz/dem5b_png/{z}/{x}/{y}.png

DEM5C
https://cyberjapandata.gsi.go.jp/xyz/dem5c_png/{z}/{x}/{y}.png

DEM10B
https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png

---

## インストール方法

1. ZIP をダウンロード  
2. QGIS → **プラグイン → プラグインの管理とインストール → ZIP からインストール**  
3. ZIP を選択してインストール  
4. 処理ツールボックスの **DEM Tools → PngTile2Dem** に表示されます

---

## 使い方

1. 取得範囲（Extent）を QGIS で指定  
2. 出力座標系（CRS）を選択  
3. 出力 GeoTIFF を指定  
4. 実行  

プラグインは以下を自動で実行：
- 必要なタイルのダウンロード  
- VRT の作成  
- 最終 CRS への Warp  
- GeoTIFF の生成  
- QGIS に自動追加  

---

## スクリーンショット

### 実行デモ
![実行デモ](images/demo.gif)

### プラグイン画面
![プラグイン画面](images/screenshot_plugin.png)

### 処理ツール画面
![処理ツール画面](images/screenshot_tool.png)

### 出力された DEM の表示例
![DEM 表示例](images/screenshot_result.png)

---

## 注意点

- 推奨最大範囲：**20,000 タイル以下**  
- 処理は最大 16 スレッドで並列  
- 出力は TILED + DEFLATE + Overviews で高速表示可能  

---

## ライセンス

MIT ライセンスで公開しています。  
詳細は `LICENSE` を参照してください。

---

### 謝辞

本プラグインおよびソースコードの作成・改良にあたっては、
**OpenAI の ChatGPT** を用い、アルゴリズム設計、デバッグ、
コード整理および README 文書作成の補助を受けました。

最終的な設計判断、検証、動作確認はすべて作者自身が行っています。


