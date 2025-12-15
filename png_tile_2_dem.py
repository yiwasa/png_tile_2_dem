# -*- coding: utf-8 -*-
from qgis.PyQt.QtWidgets import QAction
from qgis.core import QgsApplication
from .png_tile_2_dem_provider import PngTile2DemProvider
from qgis import processing

class PngTile2Dem:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None

    def initGui(self):
        self.action = QAction("PNG Tile → DEM GeoTIFF", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("&PngTile2Dem", self.action)

        self.provider = PngTile2DemProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("&PngTile2Dem", self.action)

        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)

    def run(self):
        """Run plugin main function."""
        # Show the Processing Toolbox instead of opening a nonexistent function
        try:
            self.iface.showProcessingToolbox()
        except:
            pass

        # Open the execution dialog directly
        from qgis import processing
        processing.execAlgorithmDialog("png_tile_2_dem:png_tile_2_dem")

