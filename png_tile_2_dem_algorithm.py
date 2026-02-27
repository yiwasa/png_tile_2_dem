# -*- coding: utf-8 -*-
"""
PngTile2DemAlgorithm (Multi-Source Integrated Version)
並列処理でWebタイルをダウンロードし、優先順位に従って合成、
任意のCRSでGeoTIFFを出力するQGISプラグイン。
"""

import os
import math
import tempfile
import shutil
import requests
import numpy as np
from PIL import Image
from io import BytesIO
from PIL import ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterExtent,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingException,
    QgsRasterLayer,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform
)

from osgeo import gdal, osr
gdal.SetConfigOption("GDAL_NUM_THREADS", "1")
gdal.UseExceptions()

from threading import Lock
progress_lock = Lock()

# ==============================================================================
# ヘルパー関数
# ==============================================================================

def lonlat_to_tile(lon, lat, zoom):
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n)
    return xtile, ytile

def tile_bounds_mercator(x, y, z):
    n = 2.0 ** z
    lon_left = x / n * 360.0 - 180.0
    lon_right = (x + 1) / n * 360.0 - 180.0
    lat_top = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_bottom = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))

    def latlon_to_merc(lon, lat):
        R = 6378137.0
        x_m = R * math.radians(lon)
        lat = max(min(lat, 89.9999), -89.9999)
        y_m = R * math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))
        return x_m, y_m

    minx, maxy = latlon_to_merc(lon_left, lat_top)
    maxx, miny = latlon_to_merc(lon_right, lat_bottom)
    return minx, miny, maxx, maxy

# ==============================================================================
# デコード処理
# ==============================================================================

def decode_gsi_png(img_arr):
    """国土地理院形式のデコード"""
    r = img_arr[:, :, 0].astype(np.int32)
    g = img_arr[:, :, 1].astype(np.int32)
    b = img_arr[:, :, 2].astype(np.int32)
    x = (r << 16) + (g << 8) + b
    height = np.empty_like(x, dtype=np.float32)
    mask_low = x < (1 << 23)
    height[mask_low] = x[mask_low] * 0.01
    height[x == (1 << 23)] = np.nan
    mask_high = x > (1 << 23)
    height[mask_high] = (x[mask_high] - (1 << 24)) * 0.01
    return height

def decode_qmap_rgb(img_arr):
    """Q地図形式のデコード"""
    r = img_arr[:, :, 0].astype(np.float32)
    g = img_arr[:, :, 1].astype(np.float32)
    b = img_arr[:, :, 2].astype(np.float32)
    height = (r * 256 * 256 + g * 256 + b) * 0.01
    height[(r == 128) & (g == 0) & (b == 0)] = np.nan
    return height

def decode_gsj_png(img_arr):
    """産総研形式のデコード (AlphaチャンネルによるNoData)"""
    r = img_arr[:, :, 0].astype(np.float32)
    g = img_arr[:, :, 1].astype(np.float32)
    b = img_arr[:, :, 2].astype(np.float32)
    a = img_arr[:, :, 3]
    height = (r * 256 * 256 + g * 256 + b) * 0.01
    height[a == 0] = np.nan
    return height

# ==============================================================================
# タイル処理ロジック (並列実行される)
# ==============================================================================

def process_single_tile_composite(args):
    bx, by, BASE_Z, primary_key, active_sources, tmpdir, nodata = args
    session = requests.Session()
    session.headers.update({"User-Agent": "QGIS-PngTile2Dem-Integrated"})
    
    tile_size = 256
    composite_dem = np.full((tile_size, tile_size), np.nan, dtype=np.float32)

    def fetch_and_decode(src_key, x, y, z):
        source = next(s for s in active_sources if s["key"] == src_key)
        url = source["url"].format(z=z, x=x, y=y)
        try:
            r = session.get(url, timeout=10)
            if r.status_code != 200: return None
            img = Image.open(BytesIO(r.content))
            img.load()
            img_arr = np.array(img.convert("RGBA" if source["xy_order"] == "yx" else "RGB"))
            
            if src_key == "qmap": return decode_qmap_rgb(img_arr)
            elif source["xy_order"] == "yx": return decode_gsj_png(img_arr)
            else: return decode_gsi_png(img_arr)
        except: return None

    def get_scaled_dem(src_key, target_bx, target_by, target_z):
            """改良版：マスクを使用した正規化バイリニア補間"""
            source = next(s for s in active_sources if s["key"] == src_key)
            src_z = source["zoom"]
            
            if src_z == target_z:
                return fetch_and_decode(src_key, target_bx, target_by, target_z)
            
            elif src_z > target_z:
                # --- 高解像度ソースを縮小して結合する場合 ---
                shift = src_z - target_z
                scale = 1 << shift
                sub_tile_res = tile_size // scale
                full_res_dem = np.full((tile_size, tile_size), np.nan, dtype=np.float32)
                
                any_data = False
                for dx in range(scale):
                    for dy in range(scale):
                        sub_dem = fetch_and_decode(src_key, (target_bx << shift) + dx, (target_by << shift) + dy, src_z)
                        if sub_dem is not None:
                            any_data = True
                            # ★ここから修正：マスクを用いた正規化
                            mask = (~np.isnan(sub_dem)).astype(np.float32)
                            data_only = np.nan_to_num(sub_dem, nan=0.0)
                            
                            # 標高とマスクの両方をバイリニアでリサイズ
                            res_val = np.array(Image.fromarray(data_only, mode="F").resize((sub_tile_res, sub_tile_res), Image.BILINEAR))
                            res_mask = np.array(Image.fromarray(mask, mode="F").resize((sub_tile_res, sub_tile_res), Image.BILINEAR))
                            
                            # マスクで割ることで境界の値を補正（0除算回避）
                            with np.errstate(divide='ignore', invalid='ignore'):
                                res_dem = np.where(res_mask > 0.01, res_val / res_mask, np.nan)
                            
                            full_res_dem[dy*sub_tile_res:(dy+1)*sub_tile_res, dx*sub_tile_res:(dx+1)*sub_tile_res] = res_dem
                return full_res_dem if any_data else None
                
            else:
                # --- 低解像度ソースを拡大して切り出す場合 ---
                shift = target_z - src_z
                scale = 1 << shift
                src_x, src_y = target_bx >> shift, target_by >> shift
                parent_dem = fetch_and_decode(src_key, src_x, src_y, src_z)
                if parent_dem is None: return None
                
                # ★ここから修正：マスクを用いた正規化
                mask = (~np.isnan(parent_dem)).astype(np.float32)
                data_only = np.nan_to_num(parent_dem, nan=0.0)
                
                # 拡大リサイズ
                big_val = np.array(Image.fromarray(data_only, mode="F").resize((tile_size * scale, tile_size * scale), Image.BILINEAR))
                big_mask = np.array(Image.fromarray(mask, mode="F").resize((tile_size * scale, tile_size * scale), Image.BILINEAR))
                
                with np.errstate(divide='ignore', invalid='ignore'):
                    big_dem = np.where(big_mask > 0.01, big_val / big_mask, np.nan)
                
                dx, dy = target_bx & (scale - 1), target_by & (scale - 1)
                return big_dem[dy*tile_size:(dy+1)*tile_size, dx*tile_size:(dx+1)*tile_size]

    # --- 合成ステップ ---
    # 1. プライマリ
    res = get_scaled_dem(primary_key, bx, by, BASE_Z)
    if res is not None: composite_dem[:] = res
    
    # 2. Q地図補完 (プライマリがQ地図でない場合)
    if primary_key != "qmap" and np.isnan(composite_dem).any():
        res = get_scaled_dem("qmap", bx, by, BASE_Z)
        if res is not None:
            mask = np.isnan(composite_dem)
            composite_dem[mask] = res[mask]
            
    # 3. フォールバック
    fallbacks = ["fallback_dem5a", "fallback_dem5b", "fallback_dem5c", "fallback_dem10b"]
    for fb in fallbacks:
        if not np.isnan(composite_dem).any(): break
        res = get_scaled_dem(fb, bx, by, BASE_Z)
        if res is not None:
            mask = np.isnan(composite_dem)
            composite_dem[mask] = res[mask]

    # 出力
    h_filled = np.where(np.isnan(composite_dem), nodata, composite_dem).astype(np.float32)
    minx, miny, maxx, maxy = tile_bounds_mercator(bx, by, BASE_Z)
    out_path = os.path.join(tmpdir, f"tile_{bx}_{by}.tif")
    
    try:
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(out_path, tile_size, tile_size, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((minx, (maxx-minx)/tile_size, 0, maxy, 0, -(maxy-miny)/tile_size))
        srs = osr.SpatialReference(); srs.ImportFromEPSG(3857)
        ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(h_filled)
        ds.GetRasterBand(1).SetNoDataValue(nodata)
        ds = None
        return out_path, True
    except:
        return None, False

# ==============================================================================
# QGIS アルゴリズム クラス
# ==============================================================================

class PngTile2DemAlgorithm(QgsProcessingAlgorithm):
    INPUT_EXTENT = "INPUT_EXTENT"
    PRIMARY_DEM = "PRIMARY_DEM"
    OUTPUT_CRS = "OUTPUT_CRS"
    OUTPUT_TIF = "OUTPUT_TIF"

    TILE_SOURCES = [
        {"key": "qmap", "name": "基盤地図情報1ｍメッシュ【Q地図】", "zoom": 17, "url": "https://mapdata.qchizu.xyz/03_dem/52_gsi/all_2025/1_02/{z}/{x}/{y}.webp", "xy_order": "xy"},
        {"key": "qmap", "name": "基盤地図情報1ｍメッシュ【地理院】", "zoom": 17, "url": "https://cyberjapandata.gsi.go.jp/xyz/dem1a_png/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "miyagi", "name": "宮城県0.5mメッシュ【林野庁】", "zoom": 18, "url": "https://forestgeo.info/opendata/4_miyagi/dem_2023/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "tochigi", "name": "2021〜2022年栃木県0.5mメッシュ【産総研】", "zoom": 18, "url": "https://tiles.gsj.jp/tiles/elev/tochigi/{z}/{y}/{x}.png", "xy_order": "yx"},
        {"key": "tokyo", "name": "2022〜2023年度東京都0.25mメッシュ【産総研】", "zoom": 19, "url": "https://tiles.gsj.jp/tiles/elev/tokyo/{z}/{y}/{x}.png", "xy_order": "yx"},
        {"key": "kanagawa", "name": "2019〜2022年度神奈川県0.5mメッシュ【産総研】", "zoom": 18, "url": "https://tiles.gsj.jp/tiles/elev/kanagawa/{z}/{y}/{x}.png", "xy_order": "yx"},
        {"key": "toyama", "name": "2021年富山県0.5mメッシュ【林野庁】", "zoom": 18, "url": "https://forestgeo.info/opendata/16_toyama/dem_2021/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "noto2024", "name": "2024年石川県能登0.5mメッシュ【Q地図】", "zoom": 18, "url": "https://mapdata.qchizu2.xyz/03_dem/59_rinya/noto_2024/0pt5_01/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "noto2020w", "name": "2020年度石川県能登西部0.5mメッシュ【Q地図】", "zoom": 17, "url": "https://mapdata.qchizu.xyz/94dem/17p/ishikawa_f_02_g/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "noto2022e", "name": "2022年度石川県能登東部0.5mメッシュ【Q地図】", "zoom": 17, "url": "https://mapdata.qchizu.xyz/94dem/17p/ishikawa_f_01_g/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "yamanashi", "name": "2024年山梨県0.5mメッシュ【林野庁】", "zoom": 18, "url": "https://forestgeo.info/opendata/19_yamanashi/dem_2024/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "shizuoka", "name": "静岡県0.5mメッシュ【産総研】", "zoom": 18, "url": "https://tiles.gsj.jp/tiles/elev/shizuoka/{z}/{y}/{x}.png", "xy_order": "yx"},
        {"key": "aichi", "name": "愛知県（尾張西三河森林計画区）0.5mメッシュ【林野庁】", "zoom": 18, "url": "https://rinya-tiles.geospatial.jp/dem_078_2025/{z}/{x}/{y}.png", "xy_order": "xy"}, 
        {"key": "shiga", "name": "滋賀県0.5mメッシュ【林野庁】", "zoom": 18, "url": "https://forestgeo.info/opendata/25_shiga/dem_2023/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "kyoto", "name": "2019〜2023年京都府0.5mメッシュ【林野庁】", "zoom": 18, "url": "https://forestgeo.info/opendata/26_kyoto/dem_2024/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "hyogo", "name": "2021〜2022年度兵庫県0.5mメッシュ【産総研】", "zoom": 18, "url": "https://tiles.gsj.jp/tiles/elev/hyogodem/{z}/{y}/{x}.png", "xy_order": "yx"},
        {"key": "tottori", "name": "2018〜2023年度鳥取県0.5mメッシュ【鳥取県】", "zoom": 18, "url": "https://rinya-tottori.geospatial.jp/tile/rinya/2024/gridPNG_tottori/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "okayama", "name": "岡山県0.5mメッシュ【林野庁】", "zoom": 18, "url": "https://forestgeo.info/opendata/33_okayama/dem_2024/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "ehime", "name": "2019年愛媛県0.5mメッシュ【林野庁】", "zoom": 18, "url": "https://forestgeo.info/opendata/38_ehime/dem_2019/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "kouchi", "name": "2018年度高知県0.5mメッシュ【産総研】", "zoom": 18, "url": "https://tiles.gsj.jp/tiles/elev/kochi/{z}/{y}/{x}.png", "xy_order": "yx"},
        {"key": "fallback_dem5a", "zoom": 15, "url": "https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "fallback_dem5b", "zoom": 15, "url": "https://cyberjapandata.gsi.go.jp/xyz/dem5b_png/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "fallback_dem5c", "zoom": 15, "url": "https://cyberjapandata.gsi.go.jp/xyz/dem5c_png/{z}/{x}/{y}.png", "xy_order": "xy"},
        {"key": "fallback_dem10b", "zoom": 14, "url": "https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png", "xy_order": "xy"},
    ]

    def name(self): return "png_tile_2_dem_integrated"
    def displayName(self): return "PngTile2Dem (Multi-Source Integrated)"
    def group(self): return "DEM Tools"
    def groupId(self): return "dem_tools"
    def shortHelpString(self):
        # ウィンドウの右パネル（ヘルプ）に表示されるテキスト（HTML対応）
        return """
        <h2>DEM取得の手順</h2>
        <p>DEMの整備範囲を確認するには、<b><a href="https://maps.qchizu.xyz/">全国Q地図</a></b> を開き、「4.地形」レイヤを参照してください。</p>
        <p>「愛知県（尾張西三河森林計画区）」の整備範囲は<b><a href="https://www.geospatial.jp/ckan/dataset/owarinishimikawa_078/resource/c27000f2-7a52-4a6d-93af-227fb4d23a01">こちら</a></b> をご確認ください。</p>
        <p>範囲を選択する際は、このツールに戻り「Extraction extent」の右側の「...」ボタンから「キャンバス上で描画」などを選択してください。</p>
        """

    def helpUrl(self):
        # ウィンドウ下部の「ヘルプ」ボタンをクリックした時の遷移先
        return "https://maps.qchizu.xyz/"
    def createInstance(self): return PngTile2DemAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterExtent(self.INPUT_EXTENT, "Extraction extent"))
        
        display_names = [s["name"] for s in self.TILE_SOURCES if not s["key"].startswith("fallback_")]
        self.addParameter(QgsProcessingParameterEnum(self.PRIMARY_DEM, "Primary DEM source", options=display_names, defaultValue=0))
        
        self.addParameter(QgsProcessingParameterCrs(self.OUTPUT_CRS, "Output CRS", defaultValue="EPSG:4326"))
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT_TIF, "Output GeoTIFF"))

    def checkParameterValues(self, parameters, context):
        extent = self.parameterAsExtent(parameters, self.INPUT_EXTENT, context)
        if extent.isNull():
            return True, ""

        # タイル数計算のための座標変換
        source_crs = context.project().crs()
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if not source_crs.isValid():
            return True, ""
            
        xform = QgsCoordinateTransform(source_crs, target_crs, context.transformContext())
        try:
            p_min = xform.transform(extent.xMinimum(), extent.yMinimum())
            p_max = xform.transform(extent.xMaximum(), extent.yMaximum())
        except:
            return True, ""

        # ★修正: 選択されたソースに合わせて見積もり計算用のズームレベルを取得
        primary_idx = self.parameterAsEnum(parameters, self.PRIMARY_DEM, context)
        display_sources = [s for s in self.TILE_SOURCES if not s["key"].startswith("fallback_")]
        BASE_Z = display_sources[primary_idx]["zoom"]

        tx0, ty_max = lonlat_to_tile(p_min.x(), p_max.y(), BASE_Z)
        tx1, ty_min = lonlat_to_tile(p_max.x(), p_min.y(), BASE_Z)
        tx_start, tx_end = min(tx0, tx1), max(tx0, tx1)
        ty_start, ty_end = min(ty_min, ty_max), max(ty_min, ty_max)

        n_tiles = (tx_end - tx_start + 1) * (ty_end - ty_start + 1)
        
        # 推定時間の計算 (1タイル0.08秒)
        est_sec = n_tiles * 0.08
        if est_sec < 60:
            time_str = f"{int(est_sec)}秒"
        else:
            time_str = f"{int(est_sec // 60)}分{int(est_sec % 60)}秒"

        # 警告しきい値 (例: 5000枚)
        if n_tiles > 5000:
            return True, f"【警告】タイル数が多すぎます ({n_tiles}枚)。推定時間: 約{time_str}。範囲を狭めることを推奨します。"
        elif n_tiles > 0:
            return True, f"推定タイル数: {n_tiles}枚 / 推定処理時間: 約{time_str}"

        return super().checkParameterValues(parameters, context)

    def processAlgorithm(self, parameters, context, feedback):
        extent = self.parameterAsExtent(parameters, self.INPUT_EXTENT, context)
        primary_idx = self.parameterAsEnum(parameters, self.PRIMARY_DEM, context)
        output_tif = self.parameterAsOutputLayer(parameters, self.OUTPUT_TIF, context)
        output_crs = self.parameterAsCrs(parameters, self.OUTPUT_CRS, context)

        display_sources = [s for s in self.TILE_SOURCES if not s["key"].startswith("fallback_")]
        primary_source = display_sources[primary_idx]     
        primary_key = primary_source["key"]          

        # 範囲変換
        epsg4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        xform = QgsCoordinateTransform(context.project().crs(), epsg4326, QgsProject.instance())
        p_min = xform.transform(extent.xMinimum(), extent.yMinimum())
        p_max = xform.transform(extent.xMaximum(), extent.yMaximum())

        BASE_Z = primary_source["zoom"]            
        tx0, ty_max = lonlat_to_tile(p_min.x(), p_max.y(), BASE_Z)
        tx1, ty_min = lonlat_to_tile(p_max.x(), p_min.y(), BASE_Z)
        tx_start, tx_end = min(tx0, tx1), max(tx0, tx1)
        ty_start, ty_end = min(ty_min, ty_max), max(ty_min, ty_max)

        n_tiles = (tx_end - tx_start + 1) * (ty_end - ty_start + 1)

        # ==========================================================
        # ★ 推定時間の計算と表示
        # ==========================================================
        # 1タイルあたり0.08秒と仮定（PC性能やネット回線に依存）
        sec_per_tile = 0.08 
        estimated_seconds = n_tiles * sec_per_tile
        
        if estimated_seconds < 60:
            time_str = f"{int(estimated_seconds)} 秒"
        else:
            time_str = f"{int(estimated_seconds // 60)} 分 {int(estimated_seconds % 60)} 秒"

        feedback.pushInfo(f"--- 処理見積もり ---")
        feedback.pushInfo(f"総タイル数 (Zoom {BASE_Z}): {n_tiles} 枚")
        feedback.pushInfo(f"推定処理時間: 約 {time_str}")
        feedback.pushInfo(f"※通信速度やPC性能により前後します。")
        feedback.pushInfo(f"--------------------")

        if n_tiles > 30000: raise QgsProcessingException(f"タイル数が多すぎます ({n_tiles}枚)。範囲を狭めてください。")

        tmpdir = tempfile.mkdtemp(prefix="pngtile_composite_")
        nodata = -9999.0

        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            tasks = []
            for x in range(tx_start, tx_end + 1):
                for y in range(ty_start, ty_end + 1):
                    tasks.append((x, y, BASE_Z, primary_key, self.TILE_SOURCES, tmpdir, nodata))

            max_workers = min(16, (os.cpu_count() or 4) * 2)
            temp_files = []
            completed = 0
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(process_single_tile_composite, t) for t in tasks]
                for future in as_completed(futures):
                    if feedback.isCanceled(): break
                    out, success = future.result()
                    if success: temp_files.append(out)
                    completed += 1
                    feedback.setProgress(int(completed / n_tiles * 80))

            if not temp_files: raise QgsProcessingException("No tiles were downloaded.")

            # VRT作成 & Warp
            feedback.pushInfo("Mosaicking and Reprojecting...")
            vrt_path = os.path.join(tmpdir, "mosaic.vrt")
            gdal.BuildVRT(vrt_path, temp_files)

            # ★追加: 指定範囲に合わせて正確に切り取るための計算
            out_xform = QgsCoordinateTransform(context.project().crs(), output_crs, QgsProject.instance())
            out_rect = out_xform.transformBoundingBox(extent)

            warp_opts = gdal.WarpOptions(
                dstSRS=output_crs.authid(),
                format="GTiff",
                resampleAlg=gdal.GRA_Bilinear,
                dstNodata=nodata,
                # ★追加: 出力範囲をユーザー指定範囲(minX, minY, maxX, maxY)に固定
                outputBounds=(out_rect.xMinimum(), out_rect.yMinimum(), out_rect.xMaximum(), out_rect.yMaximum()),
                creationOptions=["COMPRESS=DEFLATE", "TILED=YES"]
            )
            gdal.Warp(output_tif, vrt_path, options=warp_opts)

            # レイヤ追加
            layer = QgsRasterLayer(output_tif, os.path.basename(output_tif))
            QgsProject.instance().addMapLayer(layer)

            return {self.OUTPUT_TIF: output_tif}

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
