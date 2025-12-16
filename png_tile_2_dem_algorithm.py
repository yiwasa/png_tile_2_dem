
# -*- coding: utf-8 -*-
"""
PngTile2DemAlgorithm
Download WebP DEM tiles, decode RGB->height, mosaic and reproject (output CRS selectable)
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


def rgb_to_height_from_array(arr):
    R = arr[:, :, 0].astype(np.int64)
    G = arr[:, :, 1].astype(np.int64)
    B = arr[:, :, 2].astype(np.int64)

    x = (R << 16) + (G << 8) + B
    u = 0.01
    h = np.full(x.shape, np.nan, dtype=np.float32)

    mask_lt = (x < 2 ** 23)
    if mask_lt.any():
        h[mask_lt] = (x[mask_lt] * u).astype(np.float32)

    mask_gt = (x > 2 ** 23)
    if mask_gt.any():
        h[mask_gt] = ((x[mask_gt] - 2 ** 24) * u).astype(np.float32)

    na_mask = (R == 128) & (G == 0) & (B == 0)
    h[na_mask] = np.nan

    return h

def process_single_tile(args):
    x, y, Z, tmpdir, nodata = args
    session = requests.Session()
    session.headers.update({"User-Agent": "png_tile_2_dem/1.0"})

    # -----------------------------
    # Q地図は Z=17 を使用（そのまま x,y を使う）
    # GSI PNG DEM は Z=17 には存在しない → Z=15 を使用
    # したがって x,y を Z=17→15 にスケール変換する必要がある
    # -----------------------------
    # --- DEMごとの最大ズーム ---
    Z_QMAP = Z          # Q地図（17）
    Z_GSI_5M = 15       # dem5A/B/C
    Z_GSI_10M = 14      # dem10B

    # --- タイル座標変換 ---
    shift_5m = Z_QMAP - Z_GSI_5M   # 17→15 = 2
    shift_10m = Z_QMAP - Z_GSI_10M # 17→14 = 3

    x_5m = x >> shift_5m
    y_5m = y >> shift_5m

    x_10m = x >> shift_10m
    y_10m = y >> shift_10m

    # --- 優先順 URL ---
    candidates = [
        # 1) Q地図
        f"https://mapdata.qchizu.xyz/03_dem/52_gsi/all_2025/1_02/{Z_QMAP}/{x}/{y}.webp",

        # 2) dem5 系
        f"https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{Z_GSI_5M}/{x_5m}/{y_5m}.png",
        f"https://cyberjapandata.gsi.go.jp/xyz/dem5b_png/{Z_GSI_5M}/{x_5m}/{y_5m}.png",
        f"https://cyberjapandata.gsi.go.jp/xyz/dem5c_png/{Z_GSI_5M}/{x_5m}/{y_5m}.png",

        # 3) 最後の保険：dem10B（Z=14）
        f"https://cyberjapandata.gsi.go.jp/xyz/dem_png/{Z_GSI_10M}/{x_10m}/{y_10m}.png",
    ]

    # ================================
    # 1) ベース：dem10B を必ず取得
    # ================================
    h_base = None
    try:
        url_10b = f"https://cyberjapandata.gsi.go.jp/xyz/dem_png/{Z_GSI_10M}/{x_10m}/{y_10m}.png"
        r = session.get(url_10b, timeout=12)
        if r.status_code == 200:
            img = Image.open(BytesIO(r.content)).convert("RGBA")
            arr = np.asarray(img)
            h0 = rgb_to_height_from_array(arr[:, :, :3])

            scale = 2 ** (Z - Z_GSI_10M)  # 17-14=3 → 8
            img_f = Image.fromarray(np.where(np.isnan(h0), nodata, h0).astype(np.float32), mode="F")
            img_big = img_f.resize((256 * scale, 256 * scale), Image.BILINEAR)
            h_big = np.array(img_big, dtype=np.float32)

            chunk_x = x - (x_10m << (Z - Z_GSI_10M))
            chunk_y = y - (y_10m << (Z - Z_GSI_10M))
            xs = 256 * chunk_x
            ys = 256 * chunk_y

            h_base = h_big[ys:ys+256, xs:xs+256]

    except Exception:
        pass

    if h_base is None:
        return None, False
    
    # ================================
    # 2) 上位 DEM でピクセル単位に上書き
    # ================================
    overlay_urls = [
        f"https://cyberjapandata.gsi.go.jp/xyz/dem5c_png/{Z_GSI_5M}/{x_5m}/{y_5m}.png",
        f"https://cyberjapandata.gsi.go.jp/xyz/dem5b_png/{Z_GSI_5M}/{x_5m}/{y_5m}.png",
        f"https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{Z_GSI_5M}/{x_5m}/{y_5m}.png",
        f"https://mapdata.qchizu.xyz/03_dem/52_gsi/all_2025/1_02/{Z_QMAP}/{x}/{y}.webp",
    ]

    for url in overlay_urls:
        try:
            r = session.get(url, timeout=12)
            if r.status_code != 200:
                continue

            img = Image.open(BytesIO(r.content)).convert("RGBA")
            arr = np.asarray(img)
            h0 = rgb_to_height_from_array(arr[:, :, :3])

            # --- Q地図（Z=17）はそのまま使う ---
            if "mapdata.qchizu.xyz" in url:
                h_overlay = h0

            # --- dem5 系（Z=15）は Z17 に拡大して切り出す ---
            else:
                scale = 2 ** (Z - Z_GSI_5M)  # 4
                img_f = Image.fromarray(np.where(np.isnan(h0), nodata, h0).astype(np.float32), mode="F")
                img_big = img_f.resize((256 * scale, 256 * scale), Image.BILINEAR)
                h_big = np.array(img_big, dtype=np.float32)

                chunk_x = x - (x_5m << (Z - Z_GSI_5M))
                chunk_y = y - (y_5m << (Z - Z_GSI_5M))
                xs = 256 * chunk_x
                ys = 256 * chunk_y

                h_overlay = h_big[ys:ys+256, xs:xs+256]

            # ★ Q地図・dem5 共通の上書き処理 ★
            mask = (~np.isnan(h_overlay)) & (h_overlay > -100)
            h_base[mask] = h_overlay[mask]

        except Exception:
            continue

    h = h_base
    used_url = "COMPOSITE"    

    # nodata 最終処理
    h = np.where(np.isnan(h), nodata, h).astype(np.float32)

    # prepare final array (fill NaN -> nodata)
    h_filled = np.where(np.isnan(h), nodata, h).astype(np.float32)

    # write GeoTIFF in WebMercator (EPSG:3857) with correct geotransform
    try:
        minx, miny, maxx, maxy = tile_bounds_mercator(x, y, Z)
        pixel_size_x = (maxx - minx) / 256.0
        pixel_size_y = (maxy - miny) / 256.0

        out = os.path.join(tmpdir, f"tile_{x}_{y}.tif")
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(out, 256, 256, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((minx, pixel_size_x, 0, maxy, 0, -pixel_size_y))
        srs = osr.SpatialReference(); srs.ImportFromEPSG(3857)
        ds.SetProjection(srs.ExportToWkt())
        band = ds.GetRasterBand(1)
        band.WriteArray(h_filled)
        band.SetNoDataValue(nodata)
        band.FlushCache()
        ds = None

        # write .src for debugging/tracking
        src_txt = out + ".src"
        try:
            with open(src_txt, "w", encoding="utf-8") as fp:
                fp.write(used_url + "\n")
        except Exception:
            # non-fatal if writing .src fails
            pass
        
        try:
            session.close()
        except Exception:
            pass

        return out, True

    except Exception:
        return None, False


class PngTile2DemAlgorithm(QgsProcessingAlgorithm):

    INPUT_EXTENT = "INPUT_EXTENT"
    OUTPUT_CRS = "OUTPUT_CRS"
    OUTPUT_TIF = "OUTPUT_TIF"

    def name(self):
        return "png_tile_2_dem"

    def displayName(self):
        return "PngTile2Dem (WebP DEM tiles → GeoTIFF)"

    def group(self):
        return "DEM Tools"

    def groupId(self):
        return "dem_tools"

    def createInstance(self):
        return PngTile2DemAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterExtent(self.INPUT_EXTENT, "Extraction extent (any CRS)"))
        self.addParameter(QgsProcessingParameterCrs(self.OUTPUT_CRS, "Output CRS", defaultValue="EPSG:4326"))
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT_TIF, "Output DEM (GeoTIFF)"))

    def processAlgorithm(self, parameters, context, feedback):
        extent = self.parameterAsExtent(parameters, self.INPUT_EXTENT, context)
        output_tif = self.parameterAsOutputLayer(parameters, self.OUTPUT_TIF, context)
        output_crs = self.parameterAsCrs(parameters, self.OUTPUT_CRS, context)

        if extent.isEmpty():
            raise QgsProcessingException("Extent is empty.")

        input_crs = context.project().crs()
        epsg4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        xform = QgsCoordinateTransform(input_crs, epsg4326, QgsProject.instance())

        # Convert extent corners to WGS84 lon/lat
        p1 = xform.transform(extent.xMinimum(), extent.yMinimum())
        p2 = xform.transform(extent.xMaximum(), extent.yMaximum())

        lon_min, lat_min = p1.x(), p1.y()
        lon_max, lat_max = p2.x(), p2.y()

        Z = 17
        tx_min, ty_max = lonlat_to_tile(lon_min, lat_max, Z)
        tx_max, ty_min = lonlat_to_tile(lon_max, lat_min, Z)

        tx0, tx1 = min(tx_min, tx_max), max(tx_min, tx_max)
        ty0, ty1 = min(ty_min, ty_max), max(ty_min, ty_max)

        if tx1 < tx0 or ty1 < ty0:
            raise QgsProcessingException("Invalid geographic extent → tile index collapsed. Check CRS conversion.")

        n_tiles = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
        feedback.pushInfo(f"Tiles to download: {n_tiles}")

        # 時間推定（仮：1タイル平均0.15秒 + 並列処理あり）
        avg_time_per_tile = 0.15  # 例：150ms と仮定
        max_workers = min(16, os.cpu_count() * 2)
        estimated_time = (n_tiles * avg_time_per_tile) / max_workers

        feedback.pushInfo(
            f"Estimated processing time: "
            f"{estimated_time:.1f} sec (using {max_workers} threads)"
        )
        if estimated_time > 60:
            feedback.pushInfo(f"≈ {estimated_time/60:.1f} minutes")
            
        if n_tiles <= 0:
            raise QgsProcessingException("No tiles within extent → check CRS and zoom.")

        feedback.pushInfo(f"Tiles X: {tx0}..{tx1}, Y: {ty0}..{ty1} (total {n_tiles})")

        tmpdir = tempfile.mkdtemp(prefix="pngtile2dem_")
        temp_files = []

        try:
            from concurrent.futures import ThreadPoolExecutor

            tasks = []
            nodata = -9999.0

            session = requests.Session()
            session.headers.update({"User-Agent": "png_tile_2_dem/1.0"})
            for x in range(tx0, tx1 + 1):
                for y in range(ty0, ty1 + 1):
                    tasks.append((x, y, Z, tmpdir, nodata))

            # 並列処理（CPU コア数の 2 倍くらいが高速）
            max_workers = min(16, os.cpu_count() * 2)

            feedback.pushInfo(f"Using {max_workers} threads for tile download/processing.")

            temp_files = []
            completed = 0
            total = len(tasks)

            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(process_single_tile, t) for t in tasks]

                for future in as_completed(futures):
                    out, success = future.result()

                    with progress_lock:
                        completed += 1
                        progress = int(completed / total * 100)
                        feedback.setProgress(progress)

                    if success and out:
                        temp_files.append(out)
                    else:
                        feedback.pushInfo(f"Tile failed: {future}")

            if not temp_files:
                raise QgsProcessingException("No tiles created.")

            feedback.pushInfo("Building VRT mosaic ...")
            vrt_path = os.path.splitext(output_tif)[0] + ".vrt"

            vrt = gdal.BuildVRT(vrt_path, temp_files)
            if vrt is None:
                raise QgsProcessingException("Failed to build VRT.")
            vrt = None  # 必ず一度閉じる

            # ---- Warp の進捗コールバック ----
            def warp_progress(pct, msg, user_data):
                feedback = user_data
                feedback.setProgress(int(pct * 100))
                return 1

            feedback.pushInfo("Warping (reprojecting) to final GeoTIFF ...")

            src_ds = gdal.Open(vrt_path, gdal.GA_ReadOnly)
            if src_ds is None:
                raise QgsProcessingException("Failed to open VRT.")

            warp_opts = gdal.WarpOptions(
                srcNodata=nodata,
                dstNodata=nodata,
                dstSRS=output_crs.authid(),
                format="GTiff",
                resampleAlg=gdal.GRA_Bilinear,
                creationOptions=[
                    "TILED=YES",
                    "COMPRESS=DEFLATE",
                    "BLOCKXSIZE=256",
                    "BLOCKYSIZE=256"
                ],
                callback=warp_progress,
                callback_data=feedback
            )

            dst_ds = gdal.Warp(output_tif, src_ds, options=warp_opts)
            if dst_ds is None:
                raise QgsProcessingException("Warp failed.")

            dst_ds.BuildOverviews("AVERAGE", [2, 4, 8, 16, 32])

            dst_ds = None
            src_ds = None

            # 既存同名レイヤがあれば削除
            for lyr in QgsProject.instance().mapLayers().values():
                if lyr.source() == output_tif:
                    QgsProject.instance().removeMapLayer(lyr.id())

            layer = QgsRasterLayer(output_tif, os.path.basename(output_tif))
            layer.setCrs(output_crs)
            layer.triggerRepaint()
            QgsProject.instance().addMapLayer(layer)

            return {self.OUTPUT_TIF: output_tif}

        finally:
            try:
                session.close()
            except Exception:
                pass

            shutil.rmtree(tmpdir, ignore_errors=True)
