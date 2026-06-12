#!/usr/bin/env python3
"""Map-data cache job: county/zone GeoJSON + OSM tile pyramid for the home
area, stored under MAP_CACHE_DIR so alert maps render fully offline.

Run at container start (backgrounded by run.sh); exits when done. Skips
work when the cache is fresher than MAP_REFRESH_DAYS. All failures are
non-fatal — the renderer degrades gracefully without tiles/geometry.
"""
import json
import math
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, '/app/scripts')
import config

TILE_SIZE = 256
TILE_DELAY = 0.5          # ≥2 req/s is impolite per OSM tile usage policy


def cache_dir() -> Path:
    return Path(config.env('MAP_CACHE_DIR', '/alerts/mapdata'))


def _session():
    s = requests.Session()
    s.headers.update({'User-Agent': config.env(
        'API_USER_AGENT', 'nws-alert-dashboard')})
    return s


def _bbox_of_geometry(geometry):
    lons, lats = [], []

    def walk(coords):
        if isinstance(coords[0], (int, float)):
            lons.append(coords[0])
            lats.append(coords[1])
        else:
            for c in coords:
                walk(c)
    if geometry and geometry.get('coordinates'):
        walk(geometry['coordinates'])
    if not lons:
        return None
    return min(lons), min(lats), max(lons), max(lats)


def _bbox_union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _bbox_buffer(bbox, km):
    dlat = km / 111.0
    dlon = km / (111.0 * max(0.2, math.cos(math.radians((bbox[1] + bbox[3]) / 2))))
    return bbox[0] - dlon, bbox[1] - dlat, bbox[2] + dlon, bbox[3] + dlat


def _bbox_intersects(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def fetch_zone(sess, zone_type, zone_id, zdir):
    out = zdir / f'{zone_id}.geojson'
    if out.exists():
        try:
            return _bbox_of_geometry(json.load(open(out)).get('geometry'))
        except (OSError, ValueError):
            pass
    url = f"{config.env('API_BASE_URL', 'https://api.weather.gov').rstrip('/')}" \
          f'/zones/{zone_type}/{zone_id}'
    r = sess.get(url, headers={'Accept': 'application/geo+json'}, timeout=30)
    r.raise_for_status()
    data = r.json()
    out.write_text(json.dumps({'id': zone_id,
                               'name': data.get('properties', {}).get('name'),
                               'geometry': data.get('geometry')}))
    return _bbox_of_geometry(data.get('geometry'))


def cache_zones(sess) -> tuple:
    """Fetch home + surrounding zone geometries. Returns (bbox, index dict)."""
    zdir = cache_dir() / 'zones'
    zdir.mkdir(parents=True, exist_ok=True)
    index = {}

    home_codes = config.env_list('MAP_SAME_CODES') or sorted(config.filter_same_codes())
    home_ugcs = [u for c in home_codes
                 for u in [config.fips_to_county_ugc(config.normalize_same(c))] if u]
    zone_ugcs = sorted(set(config.env_list('API_ZONES')) | config.filter_zones())

    home_bbox = None
    for ugc in home_ugcs:
        try:
            b = fetch_zone(sess, 'county', ugc, zdir)
            home_bbox = _bbox_union(home_bbox, b)
            index[ugc] = {'type': 'county', 'home': True, 'bbox': b}
            print(f'map_cache: cached county {ugc}', flush=True)
        except Exception as e:
            print(f'map_cache: county {ugc} failed: {e}', flush=True)

    for ugc in zone_ugcs:
        if ugc in index or len(ugc) != 6:
            continue
        ztype = 'county' if ugc[2] == 'C' else 'forecast'
        try:
            b = fetch_zone(sess, ztype, ugc, zdir)
            home_bbox = _bbox_union(home_bbox, b)
            index[ugc] = {'type': ztype, 'home': True, 'bbox': b}
            print(f'map_cache: cached {ztype} zone {ugc}', flush=True)
        except Exception as e:
            print(f'map_cache: zone {ugc} failed: {e}', flush=True)

    if home_bbox is None:
        return None, index

    buffer_km = config.env_int('MAP_BUFFER_KM', 120)
    buffered = _bbox_buffer(home_bbox, buffer_km)

    # Surrounding counties: the zones list endpoint never returns geometry
    # (include_geometry is ignored), so fetch each county individually and
    # keep only those whose bbox intersects the buffered home area.
    states = sorted({config.FIPS_TO_STATE_ABBR.get(config.normalize_same(c)[1:3])
                     for c in home_codes} - {None})
    base = config.env('API_BASE_URL', 'https://api.weather.gov').rstrip('/')
    for state in states:
        try:
            r = sess.get(f'{base}/zones',
                         params={'area': state, 'type': 'county'},
                         headers={'Accept': 'application/geo+json'}, timeout=60)
            r.raise_for_status()
            zone_ids = [f.get('properties', {}).get('id')
                        for f in r.json().get('features', [])]
        except Exception as e:
            print(f'map_cache: state {state} county list failed: {e}', flush=True)
            continue
        added = 0
        for zid in zone_ids:
            if not zid or zid in index:
                continue
            try:
                b = fetch_zone(sess, 'county', zid, zdir)
            except Exception:
                continue
            if not b or not _bbox_intersects(b, buffered):
                (zdir / f'{zid}.geojson').unlink(missing_ok=True)
                continue
            index[zid] = {'type': 'county', 'home': False, 'bbox': b}
            added += 1
            time.sleep(0.3)
        print(f'map_cache: cached {added} surrounding counties in {state}', flush=True)

    (zdir / 'index.json').write_text(json.dumps(index))
    return buffered, index


def _tile_range(bbox, zoom):
    def t(lon, lat):
        lat = max(-85.05, min(85.05, lat))
        n = 2 ** zoom
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        return max(0, min(n - 1, x)), max(0, min(n - 1, y))
    x0, y1 = t(bbox[0], bbox[1])
    x1, y0 = t(bbox[2], bbox[3])
    return x0, y0, x1, y1


def cache_tiles(sess, bbox):
    url_tpl = config.env('MAP_TILE_URL',
                         'https://tile.openstreetmap.org/{z}/{x}/{y}.png')
    zmin = config.env_int('MAP_ZOOM_MIN', 6)
    zmax = config.env_int('MAP_ZOOM_MAX', 11)
    troot = cache_dir() / 'tiles'

    total = fetched = failed = 0
    for z in range(zmin, zmax + 1):
        x0, y0, x1, y1 = _tile_range(bbox, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                total += 1
                out = troot / str(z) / str(x) / f'{y}.png'
                if out.exists():
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                try:
                    r = sess.get(url_tpl.format(z=z, x=x, y=y), timeout=20)
                    r.raise_for_status()
                    out.write_bytes(r.content)
                    fetched += 1
                except Exception as e:
                    failed += 1
                    if failed <= 3:
                        print(f'map_cache: tile {z}/{x}/{y} failed: {e}', flush=True)
                time.sleep(TILE_DELAY)
    print(f'map_cache: tiles total={total} fetched={fetched} failed={failed}',
          flush=True)


def main():
    if not config.env_bool('MAP_ENABLED', True):
        print('map_cache: MAP_ENABLED=false — skipping', flush=True)
        return

    meta_path = cache_dir() / 'meta.json'
    refresh_days = config.env_int('MAP_REFRESH_DAYS', 30)
    try:
        meta = json.load(open(meta_path))
        age_days = (time.time() - meta.get('created', 0)) / 86400
        if age_days < refresh_days and meta.get('complete'):
            print(f'map_cache: cache is {age_days:.0f}d old (< {refresh_days}d) — fresh',
                  flush=True)
            return
    except (OSError, ValueError):
        pass

    cache_dir().mkdir(parents=True, exist_ok=True)
    sess = _session()
    try:
        bbox, index = cache_zones(sess)
    except Exception as e:
        print(f'map_cache: zone caching failed: {e}', flush=True)
        return

    if bbox is None:
        print('map_cache: no home zones resolved — nothing to cache', flush=True)
        return

    try:
        cache_tiles(sess, bbox)
        complete = True
    except Exception as e:
        print(f'map_cache: tile caching aborted: {e}', flush=True)
        complete = False

    meta_path.write_text(json.dumps({
        'created': time.time(), 'bbox': bbox, 'zones': len(index),
        'zoom_min': config.env_int('MAP_ZOOM_MIN', 6),
        'zoom_max': config.env_int('MAP_ZOOM_MAX', 11),
        'complete': complete,
    }))
    print('map_cache: done', flush=True)


if __name__ == '__main__':
    main()
