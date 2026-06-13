#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Offline alert-map PNG renderer (Pillow only, no network).

Composites cached OSM tiles for the viewport, overlays the alert polygon
(or county boundaries as fallback), and writes /alerts/maps/{id}.png.

Renders something useful at every cache level:
  - alert geometry + tiles        → full storm-polygon map
  - no geometry, cached counties  → county-boundary highlight (radio-only)
  - no tiles                      → dark basemap with county outlines
"""
import json
import math
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, '/app/scripts')
import config
import alerts as alertdb

TILE_SIZE = 256

PRIORITY_COLORS = {
    5: (220, 38, 38),    # red
    4: (234, 88, 12),    # orange
    3: (202, 138, 4),    # yellow
    2: (37, 99, 235),    # blue
    1: (100, 116, 139),  # gray
}
BG = (15, 17, 23)


def cache_dir() -> Path:
    return Path(config.env('MAP_CACHE_DIR', '/alerts/mapdata'))


# ── Web mercator helpers ──────────────────────────────────────────────────────

def _project(lon: float, lat: float, zoom: int):
    """lon/lat → global pixel coordinates at zoom."""
    lat = max(-85.05, min(85.05, lat))
    n = TILE_SIZE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def _geom_coords(geometry: dict):
    """Yield rings (lists of [lon, lat]) for Polygon/MultiPolygon GeoJSON."""
    t = geometry.get('type')
    if t == 'Polygon':
        for ring in geometry['coordinates']:
            yield ring
    elif t == 'MultiPolygon':
        for poly in geometry['coordinates']:
            for ring in poly:
                yield ring
    elif t == 'GeometryCollection':
        for g in geometry.get('geometries', []):
            yield from _geom_coords(g)


def _geom_bbox(geoms: list):
    lons, lats = [], []
    for g in geoms:
        for ring in _geom_coords(g):
            for lon, lat in ring:
                lons.append(lon)
                lats.append(lat)
    if not lons:
        return None
    return min(lons), min(lats), max(lons), max(lats)


# ── Zone geometry cache access ────────────────────────────────────────────────

def load_zone_geometry(key: str):
    """Load cached geometry for a UGC id or 6-digit SAME code."""
    if key and key[0].isdigit():
        key = config.fips_to_county_ugc(key) or key
    path = cache_dir() / 'zones' / f'{key}.geojson'
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get('geometry') or data
    except (OSError, ValueError):
        return None


def all_cached_zone_geometries():
    out = []
    zdir = cache_dir() / 'zones'
    if not zdir.is_dir():
        return out
    for p in zdir.glob('*.geojson'):
        if p.name == 'index.json':
            continue
        try:
            with open(p) as f:
                data = json.load(f)
            g = data.get('geometry') or data
            if g and g.get('type'):
                out.append(g)
        except (OSError, ValueError):
            pass
    return out


# ── Rendering ─────────────────────────────────────────────────────────────────

def _choose_zoom(bbox, width, height):
    zmin = config.env_int('MAP_ZOOM_MIN', 6)
    zmax = config.env_int('MAP_ZOOM_MAX', 11)
    pad = 1.30  # 15% padding each side
    for z in range(zmax, zmin - 1, -1):
        x0, y1 = _project(bbox[0], bbox[1], z)
        x1, y0 = _project(bbox[2], bbox[3], z)
        if (x1 - x0) * pad <= width and (y1 - y0) * pad <= height:
            return z
    return zmin


def _paste_tiles(img, zoom, px0, py0):
    """Fill img with cached tiles for the viewport starting at global (px0,py0)."""
    tiles_root = cache_dir() / 'tiles'
    w, h = img.size
    n = 2 ** zoom
    tx0, ty0 = int(px0 // TILE_SIZE), int(py0 // TILE_SIZE)
    tx1, ty1 = int((px0 + w) // TILE_SIZE), int((py0 + h) // TILE_SIZE)
    found = 0
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            if not (0 <= tx < n and 0 <= ty < n):
                continue
            p = tiles_root / str(zoom) / str(tx) / f'{ty}.png'
            if not p.is_file():
                continue
            try:
                tile = Image.open(p).convert('RGB')
            except OSError:
                continue
            img.paste(tile, (int(tx * TILE_SIZE - px0), int(ty * TILE_SIZE - py0)))
            found += 1
    return found


def _draw_geoms(draw_layer, geoms, zoom, px0, py0, fill, outline, width=3):
    draw = ImageDraw.Draw(draw_layer)
    for g in geoms:
        for ring in _geom_coords(g):
            pts = []
            for lon, lat in ring:
                x, y = _project(lon, lat, zoom)
                pts.append((x - px0, y - py0))
            if len(pts) >= 3:
                if fill:
                    draw.polygon(pts, fill=fill)
                draw.line(pts + [pts[0]], fill=outline, width=width)


def _font(size):
    for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_alert_map(alert_row: dict):
    """Render the map PNG for an alert row. Returns filename or None."""
    width  = config.env_int('MAP_WIDTH', 900)
    height = config.env_int('MAP_HEIGHT', 600)

    # Pick the alert geometry: storm polygon, else county boundaries
    alert_geoms = []
    geometry = alert_row.get('geometry')
    if geometry and isinstance(geometry, str):
        try:
            geometry = json.loads(geometry)
        except ValueError:
            geometry = None
    if geometry:
        alert_geoms = [geometry]
    else:
        for code in json.loads(alert_row.get('fips') or '[]'):
            g = load_zone_geometry(code)
            if g:
                alert_geoms.append(g)
        for u in json.loads(alert_row.get('ugc') or '[]'):
            if u[2:3] == 'Z':
                g = load_zone_geometry(u)
                if g:
                    alert_geoms.append(g)

    if not alert_geoms:
        return None

    bbox = _geom_bbox(alert_geoms)
    if not bbox:
        return None
    zoom = _choose_zoom(bbox, width, height)

    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    pcx, pcy = _project(cx, cy, zoom)
    px0, py0 = pcx - width / 2, pcy - height / 2

    img = Image.new('RGB', (width, height), BG)
    tiles = _paste_tiles(img, zoom, px0, py0)

    if tiles == 0:
        # boundary-only basemap so radio-only/offline still gets context
        base_overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        _draw_geoms(base_overlay, all_cached_zone_geometries(), zoom, px0, py0,
                    fill=None, outline=(80, 90, 110, 255), width=1)
        img = Image.alpha_composite(img.convert('RGBA'), base_overlay).convert('RGB')

    # Home county outline for orientation
    home_geoms = []
    for code in config.filter_same_codes():
        g = load_zone_geometry(code)
        if g:
            home_geoms.append(g)

    color = PRIORITY_COLORS.get(int(alert_row.get('priority') or 3),
                                PRIORITY_COLORS[3])
    overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    if home_geoms:
        _draw_geoms(overlay, home_geoms, zoom, px0, py0,
                    fill=None, outline=(148, 163, 184, 220), width=2)
    _draw_geoms(overlay, alert_geoms, zoom, px0, py0,
                fill=color + (90,), outline=color + (255,), width=3)
    img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')

    # Title strip
    draw = ImageDraw.Draw(img)
    title = alert_row.get('event_name') or 'Alert'
    if alert_row.get('is_test'):
        title = f'[TEST] {title}'
    sub = ''
    if alert_row.get('expires_at'):
        import datetime
        sub = 'until ' + datetime.datetime.fromtimestamp(
            alert_row['expires_at']).strftime('%-I:%M %p %b %d')
    strip_h = 46
    draw.rectangle([0, 0, width, strip_h], fill=(15, 17, 23, 255))
    draw.text((12, 6), title, fill=(226, 232, 240), font=_font(20))
    if sub:
        draw.text((12, 28), sub, fill=(148, 163, 184), font=_font(13))
    draw.rectangle([0, 0, width - 1, height - 1], outline=(42, 45, 58), width=1)

    # OSM attribution (required by tile usage policy)
    if tiles:
        attrib = '© OpenStreetMap contributors'
        f = _font(11)
        tw = draw.textlength(attrib, font=f)
        draw.text((width - tw - 6, height - 16), attrib, fill=(148, 163, 184), font=f)

    out_name = f"{alert_row['id']}.png"
    out_path = Path(alertdb.MAPS_DIR) / out_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, 'PNG')
    return out_name
