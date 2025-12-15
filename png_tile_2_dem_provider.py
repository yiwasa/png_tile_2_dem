from qgis.core import QgsProcessingProvider
from .png_tile_2_dem_algorithm import PngTile2DemAlgorithm

class PngTile2DemProvider(QgsProcessingProvider):

    def loadAlgorithms(self):
        self.addAlgorithm(PngTile2DemAlgorithm())

    def id(self):
        return "png_tile_2_dem"

    def name(self):
        return "PNG Tile → DEM Tools"
