# -*- coding: utf-8 -*-
import os
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.core import QgsApplication
from .png_tile_2_dem_provider import PngTile2DemProvider

class PngTile2Dem:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None

    def initGui(self):
        # アイコンのパスを取得
        icon_path = os.path.join(os.path.dirname(__file__), 'icon.png')
        
        # QActionの作成（第1引数にQIconを追加）
        self.action = QAction(
            QIcon(icon_path), 
            "PNG Tile → DEM GeoTIFF", 
            self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)

        # 1. メニューに追加
        self.iface.addPluginToMenu("&PngTile2Dem", self.action)
        
        # 2. プラグインツールバーに追加（ここが修正ポイント！）
        self.iface.addToolBarIcon(self.action)

        self.provider = PngTile2DemProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        if self.action:
            # メニューから削除
            self.iface.removePluginMenu("&PngTile2Dem", self.action)
            # ツールバーから削除（これも忘れずに！）
            self.iface.removeToolBarIcon(self.action)

        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)

    def run(self):
        """Run plugin main function."""
        from qgis import processing
        # 直接アルゴリズムのダイアログを起動
        processing.execAlgorithmDialog("png_tile_2_dem:png_tile_2_dem_integrated")