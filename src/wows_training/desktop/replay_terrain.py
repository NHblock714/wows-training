"""Optional, view-aligned relative terrain coloring underneath minimap data."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from ..map_terrain import LAND_HEIGHT, ordered_bounds, verified_terrain
from ..ordinary_replay import CURRENT


def terrain_overlay(report, projection, land, *, full_art=False):
    """Return a transparent, north-up overlay for exactly the displayed viewport."""
    import numpy as np

    if land is None or land.isNull() or (report.get("replay_version") == CURRENT and not full_art):
        return None
    verified = verified_terrain(report)
    bounds = ordered_bounds((projection or {}).get("viewport_bounds_engine") or {})
    if not verified or not bounds:
        return None
    raster, _ = verified
    heights = raster.heights
    if not np.any(heights > LAND_HEIGHT):
        return None
    x0, x1, z0, z1 = raster.bounds
    left, right, bottom, top = bounds
    if not (x0 <= left < right <= x1 and z0 <= bottom < top <= z1):
        return None
    h, w = heights.shape
    # Color is relative to this map's tallest sampled terrain cell.
    relative = np.sqrt(heights / heights.max())
    coordinates = np.floor(5 * relative).astype(np.uint8)
    contours = np.zeros(heights.shape, dtype=bool)
    contours[:, 1:] |= coordinates[:, 1:] != coordinates[:, :-1]
    contours[1:, :] |= coordinates[1:, :] != coordinates[:-1, :]
    pixels = np.empty((h, w, 4), dtype=np.uint8)
    pixels[..., 0] = 66 + 157 * relative
    pixels[..., 1] = 152 + 55 * relative
    pixels[..., 2] = 178 - 55 * relative
    coast = land.convertToFormat(QImage.Format.Format_RGBA8888)
    rgba = np.frombuffer(coast.bits(), dtype=np.uint8).reshape(coast.height(), coast.bytesPerLine())
    tx = ((np.arange(w) + .5) * coast.width() / w).astype(int)
    ty = ((np.arange(h) + .5) * coast.height() / h).astype(int)
    coastline = rgba[ty[:, None], tx[None, :] * 4 + 3] / 255.
    pixels[..., 3] = np.where(heights > LAND_HEIGHT, (78 + 54 * relative + 16 * contours) * coastline, 0)
    backing = pixels.tobytes()
    image = QImage(backing, w, h, w * 4, QImage.Format.Format_RGBA8888)
    crop = QRectF(w*(left-x0)/(x1-x0), h*(z1-top)/(z1-z0),
                  w*(right-left)/(x1-x0), h*(top-bottom)/(z1-z0))
    if crop == QRectF(image.rect()):
        return image.copy()
    viewport = QImage(w, h, QImage.Format.Format_RGBA8888)
    viewport.fill(Qt.GlobalColor.transparent)
    painter = QPainter(viewport)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(QRectF(viewport.rect()), image, crop)
    painter.end()
    return viewport
