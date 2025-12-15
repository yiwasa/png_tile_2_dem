# -*- coding: utf-8 -*-
"""
/***************************************************************************
 PngTile2Dem
                                 A QGIS Plugin
 Download DEM from PNG tiles and convert to GeoTIFF
                              -------------------
        begin                : 2025-01-01
        copyright            :
        email                :
 ***************************************************************************/
"""

def classFactory(iface):
    from .png_tile_2_dem import PngTile2Dem
    return PngTile2Dem(iface)
