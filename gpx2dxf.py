#!/usr/bin/env python3
"""
Conversor GPX -> DXF georreferenciado para waypoints Garmin.

O script prioriza funcionamento real com Python puro:
- Faz parsing de GPX com xml.etree.ElementTree.
- Converte WGS84 para UTM com pyproj quando disponível.
- Usa uma implementação matemática local de UTM como fallback.
- Gera DXF ASCII diretamente, sem depender de ezdxf.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from xml.etree import ElementTree as ET

try:
    from pyproj import Transformer
except ImportError:  # pragma: no cover - fallback deliberado
    Transformer = None

_ezdxf_import_error: Exception | None = None
try:
    import ezdxf
    from ezdxf.addons import Importer
except Exception as _e:  # pragma: no cover - fallback deliberado (ImportError ou outro erro)
    ezdxf = None
    Importer = None
    _ezdxf_import_error = _e


WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)
WGS84_EP2 = WGS84_E2 / (1 - WGS84_E2)
UTM_K0 = 0.9996
DXF_VERSION = "AC1027"
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_LAYER = "POSTE"
ROAD_LAYER = "VIA"
ROAD_LABEL_LAYER = "VIA_NOME"
HIGHLIGHT_LAYER = "POSTE_DESTAQUE"
TEXT_COLOR = 7
TEXT_HEIGHT = 1.8
TEXT_OFFSET_X = 2.5
TEXT_OFFSET_Y = 2.5
COORD_TEXT_OFFSET_Y = -0.8
DEFAULT_BLOCKS_DIRNAME = "Blocos"
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
)
DEFAULT_OSM_MARGIN_METERS = 250.0
OVERPASS_SERVER_TIMEOUT_SECONDS = 30
OVERPASS_CLIENT_TIMEOUT_SECONDS = 45
OVERPASS_TILE_SIZE_METERS = 2000.0
OVERPASS_TILE_TRIGGER_METERS = 2500.0
OVERPASS_MIN_RETRY_TILE_SIZE_METERS = 50.0
OVERPASS_REQUEST_ATTEMPTS = 2
OVERPASS_RETRY_SLEEP_SECONDS = 1.5
ROAD_TEXT_HEIGHT = 1.6
ROAD_EDGE_COLOR = 8
ROAD_LABEL_COLOR = 7
ROAD_LABEL_TRUE_COLOR = 0
HIGHLIGHT_COLOR = 1
HIGHLIGHT_MIN_RADIUS = 4.0
HIGHLIGHT_PADDING = 1.6
HIGHLIGHT_POINT_PADDING = 1.4
POINT_CIRCLE_RADIUS = 2.0  # Raio do circulo quando apenas circulos sao gerados (sem postes)

ROAD_HALF_WIDTH_BY_HIGHWAY = {
    "motorway": 7.0,
    "trunk": 6.0,
    "primary": 5.0,
    "secondary": 4.5,
    "tertiary": 4.0,
    "residential": 3.5,
    "service": 2.8,
    "unclassified": 3.2,
    "living_street": 3.0,
}

KML_NS = "http://www.opengis.net/kml/2.2"
TRAMO_LAYER = "TRAMO"
# Paleta ACI usada para diferenciar arquivos KML no DXF multi-tramo.
# 1=vermelho, 3=verde, 5=azul, 6=magenta, 4=ciano, 2=amarelo,
# 30=laranja, 140=violeta, 11=ciano-claro, 21=verde-claro
TRAMO_COLOR_PALETTE = [1, 3, 5, 6, 4, 2, 30, 140, 11, 21]

SYMBOL_TO_BLOCK = {
    "flag, blue": ("POE", DEFAULT_LAYER, 5, "Poste existente"),
    "flag, red": ("PR", DEFAULT_LAYER, 1, "Poste retirar"),
    "flag, green": ("PI", DEFAULT_LAYER, 3, "Poste implantar"),
    "flag, yellow": ("PS", DEFAULT_LAYER, 2, "Poste substituir"),
}
FALLBACK_BLOCK = ("WPT_GENERICO", DEFAULT_LAYER, 1, "Waypoint sem simbolo Garmin padronizado")
SUPPORTED_SYMBOLS = tuple(SYMBOL_TO_BLOCK)


@dataclass(frozen=True)
class Waypoint:
    name: str
    symbol: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class BlockInfo:
    name: str
    layer: str
    color: int
    description: str


FALLBACK_BLOCK_INFO = BlockInfo(*FALLBACK_BLOCK)
CONFIGURED_BLOCK_INFOS = tuple(
    [BlockInfo(*block) for block in dict.fromkeys(SYMBOL_TO_BLOCK.values())] + [FALLBACK_BLOCK_INFO]
)
SUPPORTED_BLOCK_NAMES = tuple(block.name for block in CONFIGURED_BLOCK_INFOS)
XML_ENCODING_PATTERN = re.compile(r'encoding\s*=\s*["\'](?P<encoding>[^"\']+)["\']', re.IGNORECASE)


@dataclass(frozen=True)
class ExternalBlock:
    name: str
    layer: str
    base_point: tuple[float, float, float]
    records: list[list[tuple[str, str]]]


@dataclass(frozen=True)
class OSMRoad:
    osm_id: int
    name: str
    highway: str
    coordinates: list[tuple[float, float]]


@dataclass(frozen=True)
class TramoSegment:
    name: str
    conductor_type: str
    points_wgs84: list[tuple[float, float]]  # lista de (lat, lon)


class OverpassRequestError(RuntimeError):
    def __init__(self, message: str, *, last_error: Exception | None = None):
        super().__init__(message)
        self.last_error = last_error


def _detect_xml_encoding(raw_bytes: bytes) -> str:
    header = raw_bytes[:256].decode("ascii", errors="ignore")
    match = XML_ENCODING_PATTERN.search(header)
    if match is not None:
        return match.group("encoding")
    return "utf-8-sig"


def _read_xml_text(filepath: str | Path) -> str:
    raw_bytes = Path(filepath).read_bytes()
    encoding = _detect_xml_encoding(raw_bytes)

    try:
        return raw_bytes.decode(encoding)
    except LookupError:
        return raw_bytes.decode("utf-8-sig", errors="replace")
    except UnicodeDecodeError:
        return raw_bytes.decode(encoding, errors="replace")


def _is_valid_xml_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        codepoint in (0x09, 0x0A, 0x0D)
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _find_first_invalid_xml_char(text: str) -> tuple[int, int, str] | None:
    line_number = 1
    column_number = 0

    for char in text:
        if not _is_valid_xml_char(char):
            return line_number, column_number, char
        if char == "\n":
            line_number += 1
            column_number = 0
        else:
            column_number += 1

    return None


def _sanitize_xml_text(text: str) -> str:
    sanitized_chars: list[str] = []
    for char in text:
        sanitized_chars.append(char if _is_valid_xml_char(char) else "\uFFFD")
    return "".join(sanitized_chars)


def _line_column_to_offset(text: str, line_number: int, column_number: int) -> int | None:
    if line_number < 1 or column_number < 0:
        return None

    current_line = 1
    current_column = 0

    for index, char in enumerate(text):
        if current_line == line_number and current_column == column_number:
            return index
        if char == "\n":
            current_line += 1
            current_column = 0
        else:
            current_column += 1

    if current_line == line_number and current_column == column_number:
        return len(text)
    return None


def _format_xml_char(char: str) -> str:
    if char == "\n":
        label = "\\n"
    elif char == "\r":
        label = "\\r"
    elif char == "\t":
        label = "\\t"
    elif char.isprintable():
        label = char
    else:
        label = repr(char)[1:-1]
    return f"'{label}' (U+{ord(char):04X})"


def _build_xml_context_snippet(text: str, offset: int | None, radius: int = 60) -> str:
    if offset is None:
        return ""

    start = max(0, offset - radius)
    end = min(len(text), offset + radius)
    snippet = text[start:end].replace("\r", "\\r").replace("\n", "\\n")
    return snippet.strip()


def _build_gpx_parse_error(filepath: str | Path, xml_text: str, error: ET.ParseError) -> ValueError:
    line_number, column_number = getattr(error, "position", (None, None))
    details = [
        f"Arquivo GPX invalido: XML malformado em linha {line_number}, coluna {column_number}.",
    ]

    offset = None
    if line_number is not None and column_number is not None:
        offset = _line_column_to_offset(xml_text, line_number, column_number)

    if offset is not None and offset < len(xml_text):
        offending_char = xml_text[offset]
        details.append(f"Caractere na posicao indicada: {_format_xml_char(offending_char)}.")
        if offending_char == "&":
            details.append("Ha um '&' solto no texto; em XML ele precisa ser escrito como '&amp;'.")
        elif offending_char == "<":
            details.append("Ha um '<' solto no texto; em XML ele precisa ser escrito como '&lt;'.")
        elif not _is_valid_xml_char(offending_char):
            details.append("Esse caractere nao e permitido em XML 1.0.")

    context = _build_xml_context_snippet(xml_text, offset)
    if context:
        details.append(f"Trecho proximo ao erro: {context}")

    details.append("Causas comuns: caractere de controle invisivel, '&' sem escape, '<' dentro de texto ou arquivo salvo com codificacao inconsistente.")
    return ValueError(" ".join(details))


def normalize_symbol(symbol: str | None) -> str:
    return (symbol or "").strip().lower()


def get_block_info(symbol: str | None) -> BlockInfo:
    block = SYMBOL_TO_BLOCK.get(normalize_symbol(symbol))
    if block is None:
        return FALLBACK_BLOCK_INFO
    return BlockInfo(*block)


def detect_utm_zone(latitude: float, longitude: float) -> tuple[int, int]:
    """
    Retorna (zona, epsg) a partir de coordenadas WGS84.
    """
    if not (-80.0 <= latitude <= 84.0):
        raise ValueError("Latitude fora da faixa UTM suportada (-80 a 84 graus).")
    if not (-180.0 <= longitude <= 180.0):
        raise ValueError("Longitude invalida.")

    zone = int((longitude + 180) // 6) + 1

    # Ajustes especiais da especificacao UTM.
    if 56.0 <= latitude < 64.0 and 3.0 <= longitude < 12.0:
        zone = 32
    if 72.0 <= latitude < 84.0:
        if 0.0 <= longitude < 9.0:
            zone = 31
        elif 9.0 <= longitude < 21.0:
            zone = 33
        elif 21.0 <= longitude < 33.0:
            zone = 35
        elif 33.0 <= longitude < 42.0:
            zone = 37

    epsg = (32600 if latitude >= 0 else 32700) + zone
    return zone, epsg


def _manual_wgs84_to_utm(latitude: float, longitude: float, zone: int) -> tuple[float, float]:
    lat_rad = math.radians(latitude)
    lon_rad = math.radians(longitude)
    lon_origin = math.radians((zone - 1) * 6 - 180 + 3)

    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)
    tan_lat = math.tan(lat_rad)

    n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
    t = tan_lat * tan_lat
    c = WGS84_EP2 * cos_lat * cos_lat
    a = cos_lat * (lon_rad - lon_origin)

    m = WGS84_A * (
        (1 - WGS84_E2 / 4 - 3 * WGS84_E2**2 / 64 - 5 * WGS84_E2**3 / 256) * lat_rad
        - (3 * WGS84_E2 / 8 + 3 * WGS84_E2**2 / 32 + 45 * WGS84_E2**3 / 1024)
        * math.sin(2 * lat_rad)
        + (15 * WGS84_E2**2 / 256 + 45 * WGS84_E2**3 / 1024) * math.sin(4 * lat_rad)
        - (35 * WGS84_E2**3 / 3072) * math.sin(6 * lat_rad)
    )

    easting = UTM_K0 * n * (
        a
        + (1 - t + c) * a**3 / 6
        + (5 - 18 * t + t**2 + 72 * c - 58 * WGS84_EP2) * a**5 / 120
    ) + 500000.0

    northing = UTM_K0 * (
        m
        + n
        * tan_lat
        * (
            a**2 / 2
            + (5 - t + 9 * c + 4 * c**2) * a**4 / 24
            + (61 - 58 * t + t**2 + 600 * c - 330 * WGS84_EP2) * a**6 / 720
        )
    )

    if latitude < 0:
        northing += 10000000.0

    return easting, northing


def convert_coords(latitude: float, longitude: float, epsg_code: int) -> tuple[float, float]:
    if Transformer is not None:
        transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg_code}", always_xy=True)
        easting, northing = transformer.transform(longitude, latitude)
        return float(easting), float(northing)

    zone = epsg_code % 100
    return _manual_wgs84_to_utm(latitude, longitude, zone)


def parse_gpx(filepath: str | Path) -> list[Waypoint]:
    xml_text = _sanitize_xml_text(_read_xml_text(filepath))

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise _build_gpx_parse_error(filepath, xml_text, error) from error
    namespace = {"gpx": "http://www.topografix.com/GPX/1/1"}
    waypoints: list[Waypoint] = []

    for waypoint in root.findall("gpx:wpt", namespace):
        latitude = waypoint.get("lat")
        longitude = waypoint.get("lon")
        if latitude is None or longitude is None:
            continue

        name = waypoint.findtext("gpx:name", default="", namespaces=namespace).strip()
        symbol = waypoint.findtext("gpx:sym", default="", namespaces=namespace).strip()

        if not name:
            name = f"WPT_{len(waypoints) + 1:03d}"

        waypoints.append(
            Waypoint(
                name=name,
                symbol=symbol,
                latitude=float(latitude),
                longitude=float(longitude),
            )
        )

    if not waypoints:
        raise ValueError(f"Nenhum waypoint foi encontrado em {filepath}.")

    return waypoints


def _parse_kml_coordinates(coords_text: str) -> list[tuple[float, float]]:
    """Converte texto de coordenadas KML em lista de (lat, lon).

    Suporta formato com espaco entre tuplas ("lon,lat,alt lon,lat,alt")
    e formato todo-virgula ("lon,lat,alt,lon,lat,alt") usado neste arquivo.
    """
    raw = coords_text.strip().replace("\n", " ").replace("\t", " ")
    parts = raw.split()
    if len(parts) > 1 and "," in parts[0]:
        # Formato padrao KML: tuplas separadas por espaco
        points: list[tuple[float, float]] = []
        for part in parts:
            components = part.split(",")
            if len(components) >= 2:
                lon, lat = float(components[0]), float(components[1])
                points.append((lat, lon))
        return points
    # Formato todo-virgula: lon,lat,alt,lon,lat,alt,...
    values = [v for v in raw.replace(" ", ",").split(",") if v.strip()]
    points = []
    for i in range(0, len(values) - 2, 3):
        lon, lat = float(values[i]), float(values[i + 1])
        points.append((lat, lon))
    return points


def parse_kml_tramos(filepath: str | Path) -> list[TramoSegment]:
    """Le um KML e extrai todos os Placemarks com LineString como TramoSegment."""
    xml_text = _sanitize_xml_text(_read_xml_text(filepath))

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise _build_gpx_parse_error(filepath, xml_text, error) from error

    ns_tag = f"{{{KML_NS}}}"
    segments: list[TramoSegment] = []

    for placemark in root.findall(f".//{ns_tag}Placemark"):
        linestring = placemark.find(f"{ns_tag}LineString")
        if linestring is None:
            continue

        coords_elem = linestring.find(f"{ns_tag}coordinates")
        if coords_elem is None or not coords_elem.text:
            continue

        points = _parse_kml_coordinates(coords_elem.text)
        if len(points) < 2:
            continue

        name_elem = placemark.find(f"{ns_tag}name")
        name = name_elem.text.strip() if name_elem is not None and name_elem.text else ""

        conductor_type = ""
        for data_elem in placemark.findall(f".//{ns_tag}Data"):
            if data_elem.get("name") == "Tipo Condutor":
                value_elem = data_elem.find(f"{ns_tag}value")
                if value_elem is not None and value_elem.text:
                    conductor_type = value_elem.text.strip()
                break

        segments.append(TramoSegment(name=name, conductor_type=conductor_type, points_wgs84=points))

    if not segments:
        raise ValueError(
            f"Nenhum segmento de tramo (LineString) encontrado em {filepath}. "
            "Verifique se o arquivo KML contem Placemarks com geometria LineString."
        )

    return segments


def _dxf_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _pair(code: int, value: object) -> list[str]:
    return [str(code), _dxf_value(value)]


def _sanitize_text(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def format_utm_label(easting: float, northing: float) -> str:
    easting_int = int(round(easting))
    northing_int = int(round(northing))
    return f"({easting_int:07d},{northing_int:07d})"


def _estimate_text_width(text: str, text_height: float) -> float:
    # Aproximacao suficiente para compor o destaque visual em CAD.
    return max(1.0, len(text) * text_height * 0.62)


def compute_highlight_circle(
    point_x: float,
    point_y: float,
    name_label: str,
    utm_label: str,
) -> tuple[float, float, float]:
    name_x = point_x + TEXT_OFFSET_X
    name_y = point_y + TEXT_OFFSET_Y
    utm_x = point_x + TEXT_OFFSET_X
    utm_y = point_y + TEXT_OFFSET_Y + COORD_TEXT_OFFSET_Y - TEXT_HEIGHT

    name_width = _estimate_text_width(name_label, TEXT_HEIGHT)
    utm_width = _estimate_text_width(utm_label, TEXT_HEIGHT)

    min_x = min(point_x - HIGHLIGHT_POINT_PADDING, name_x - 0.8, utm_x - 0.8)
    max_x = max(point_x + HIGHLIGHT_POINT_PADDING, name_x + name_width + 0.8, utm_x + utm_width + 0.8)
    min_y = min(point_y - HIGHLIGHT_POINT_PADDING, utm_y - 0.8)
    max_y = max(point_y + HIGHLIGHT_POINT_PADDING, name_y + TEXT_HEIGHT + 0.8)

    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0

    corners = (
        (min_x, min_y),
        (min_x, max_y),
        (max_x, min_y),
        (max_x, max_y),
        (point_x, point_y),
    )
    radius = max(math.dist((center_x, center_y), corner) for corner in corners) + HIGHLIGHT_PADDING
    return center_x, center_y, max(HIGHLIGHT_MIN_RADIUS, radius)


def _resolve_overpass_urls(overpass_url: str | None) -> list[str]:
    if overpass_url is None:
        return list(DEFAULT_OVERPASS_URLS)

    urls = [url.strip() for url in str(overpass_url).split(",") if url.strip()]
    if not urls:
        return list(DEFAULT_OVERPASS_URLS)

    ordered: list[str] = []
    for url in [*urls, *DEFAULT_OVERPASS_URLS]:
        if url not in ordered:
            ordered.append(url)
    return ordered


def _bbox_from_waypoints(waypoints: list[Waypoint], margin_meters: float) -> tuple[float, float, float, float]:
    min_lat = min(waypoint.latitude for waypoint in waypoints)
    max_lat = max(waypoint.latitude for waypoint in waypoints)
    min_lon = min(waypoint.longitude for waypoint in waypoints)
    max_lon = max(waypoint.longitude for waypoint in waypoints)

    avg_lat = sum(waypoint.latitude for waypoint in waypoints) / len(waypoints)
    lat_margin = margin_meters / 111320.0
    cos_lat = max(0.1, math.cos(math.radians(avg_lat)))
    lon_margin = margin_meters / (111320.0 * cos_lat)
    return (
        min_lat - lat_margin,
        min_lon - lon_margin,
        max_lat + lat_margin,
        max_lon + lon_margin,
    )


def _bbox_size_meters(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    south, west, north, east = bbox
    avg_lat = (south + north) / 2.0
    height_m = max(0.0, north - south) * 111320.0
    width_m = max(0.0, east - west) * 111320.0 * max(0.1, math.cos(math.radians(avg_lat)))
    return width_m, height_m


def _distance_meters(point_a: Waypoint, point_b: Waypoint) -> float:
    avg_lat = (point_a.latitude + point_b.latitude) / 2.0
    delta_y = (point_b.latitude - point_a.latitude) * 111320.0
    delta_x = (point_b.longitude - point_a.longitude) * 111320.0 * max(0.1, math.cos(math.radians(avg_lat)))
    return math.hypot(delta_x, delta_y)


def _group_waypoints_for_osm(waypoints: list[Waypoint]) -> list[list[Waypoint]]:
    if not waypoints:
        return []

    remaining_indexes = set(range(len(waypoints)))
    groups: list[list[Waypoint]] = []

    while remaining_indexes:
        start_index = remaining_indexes.pop()
        queue = [start_index]
        group_indexes = [start_index]

        while queue:
            current_index = queue.pop()
            current_waypoint = waypoints[current_index]
            neighbor_indexes = [
                candidate_index
                for candidate_index in remaining_indexes
                if _distance_meters(current_waypoint, waypoints[candidate_index]) <= OVERPASS_TILE_SIZE_METERS
            ]
            for candidate_index in neighbor_indexes:
                remaining_indexes.remove(candidate_index)
                queue.append(candidate_index)
                group_indexes.append(candidate_index)

        groups.append([waypoints[index] for index in sorted(group_indexes)])

    return groups


def _split_bbox(bbox: tuple[float, float, float, float], tile_size_meters: float) -> list[tuple[float, float, float, float]]:
    width_m, height_m = _bbox_size_meters(bbox)
    south, west, north, east = bbox
    columns = max(1, math.ceil(width_m / tile_size_meters))
    rows = max(1, math.ceil(height_m / tile_size_meters))

    lat_step = (north - south) / rows if rows > 0 else 0.0
    lon_step = (east - west) / columns if columns > 0 else 0.0
    tiles: list[tuple[float, float, float, float]] = []

    for row_index in range(rows):
        tile_south = south + (lat_step * row_index)
        tile_north = north if row_index == rows - 1 else south + (lat_step * (row_index + 1))
        for column_index in range(columns):
            tile_west = west + (lon_step * column_index)
            tile_east = east if column_index == columns - 1 else west + (lon_step * (column_index + 1))
            tiles.append((tile_south, tile_west, tile_north, tile_east))

    return tiles


def _iter_overpass_bboxes(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    width_m, height_m = _bbox_size_meters(bbox)
    if max(width_m, height_m) <= OVERPASS_TILE_TRIGGER_METERS:
        return [bbox]
    return _split_bbox(bbox, OVERPASS_TILE_SIZE_METERS)


def _build_overpass_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    bbox_str = f"({south:.7f},{west:.7f},{north:.7f},{east:.7f})"
    return f"""
[out:json][timeout:{OVERPASS_SERVER_TIMEOUT_SECONDS}];
(
  way["highway"]{bbox_str};
);
(._;>;);
out body;
""".strip()


def _fetch_overpass_json(query: str, overpass_url: str) -> dict[str, object]:
    payload = urlparse.urlencode({"data": query}).encode("utf-8")
    last_error: Exception | None = None
    urls = _resolve_overpass_urls(overpass_url)

    for attempt in range(OVERPASS_REQUEST_ATTEMPTS):
        for url in urls:
            request = urlrequest.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                    "User-Agent": "gpx2dxf/1.0",
                },
                method="POST",
            )
            try:
                with urlrequest.urlopen(request, timeout=OVERPASS_CLIENT_TIMEOUT_SECONDS) as response:
                    response_data = response.read().decode("utf-8")
                return json.loads(response_data)
            except json.JSONDecodeError as exc:
                raise RuntimeError("A resposta da API Overpass nao esta em JSON valido.") from exc
            except (urlerror.HTTPError, urlerror.URLError, TimeoutError, socket.timeout) as exc:
                last_error = exc

        if attempt < OVERPASS_REQUEST_ATTEMPTS - 1:
            time.sleep(OVERPASS_RETRY_SLEEP_SECONDS * (attempt + 1))

    raise OverpassRequestError(
        "Falha ao consultar o OpenStreetMap via Overpass. "
        f"Ultimo erro: {last_error}",
        last_error=last_error,
    )


def _fetch_overpass_elements(
    bbox: tuple[float, float, float, float],
    overpass_url: str,
) -> list[dict[str, object]]:
    try:
        parsed = _fetch_overpass_json(_build_overpass_query(bbox), overpass_url)
        return [element for element in parsed.get("elements", []) if isinstance(element, dict)]
    except OverpassRequestError:
        width_m, height_m = _bbox_size_meters(bbox)
        if max(width_m, height_m) <= OVERPASS_MIN_RETRY_TILE_SIZE_METERS:
            raise

        next_tile_size = max(OVERPASS_MIN_RETRY_TILE_SIZE_METERS, max(width_m, height_m) / 2.0)
        elements_by_id: dict[tuple[str, int], dict[str, object]] = {}
        for sub_bbox in _split_bbox(bbox, next_tile_size):
            for element in _fetch_overpass_elements(sub_bbox, overpass_url):
                element_type = str(element.get("type", "")).strip()
                element_id = element.get("id")
                if not element_type or element_id is None:
                    continue
                elements_by_id[(element_type, int(element_id))] = element
        return list(elements_by_id.values())


def fetch_osm_features(
    waypoints: list[Waypoint],
    margin_meters: float = DEFAULT_OSM_MARGIN_METERS,
    overpass_url: str = DEFAULT_OVERPASS_URL,
) -> tuple[list[OSMRoad], tuple[float, float, float, float]]:
    bbox = _bbox_from_waypoints(waypoints, margin_meters)
    elements_by_id: dict[tuple[str, int], dict[str, object]] = {}

    for waypoint_group in _group_waypoints_for_osm(waypoints):
        group_bbox = _bbox_from_waypoints(waypoint_group, margin_meters)
        for tile_bbox in _iter_overpass_bboxes(group_bbox):
            for element in _fetch_overpass_elements(tile_bbox, overpass_url):
                element_type = str(element.get("type", "")).strip()
                element_id = element.get("id")
                if not element_type or element_id is None:
                    continue
                elements_by_id[(element_type, int(element_id))] = element

    node_coords: dict[int, tuple[float, float]] = {}
    roads: list[OSMRoad] = []

    for element in elements_by_id.values():
        if element.get("type") == "node" and "lat" in element and "lon" in element:
            node_coords[int(element["id"])] = (float(element["lat"]), float(element["lon"]))

    for element in elements_by_id.values():
        element_type = element.get("type")
        tags = element.get("tags", {}) or {}
        name = str(tags.get("name", "")).strip()

        if element_type == "way" and "highway" in tags:
            coords: list[tuple[float, float]] = []
            for node_id in element.get("nodes", []):
                coord = node_coords.get(int(node_id))
                if coord is not None:
                    coords.append(coord)
            if len(coords) >= 2:
                roads.append(
                    OSMRoad(
                        osm_id=int(element["id"]),
                        name=name,
                        highway=str(tags.get("highway", "")),
                        coordinates=coords,
                    )
                )

    return roads, bbox


def _road_midpoint_and_angle(points: list[tuple[float, float]]) -> tuple[tuple[float, float], float]:
    if len(points) < 2:
        return points[0], 0.0

    segment_lengths: list[float] = []
    total_length = 0.0
    for start, end in zip(points, points[1:]):
        length = math.dist(start, end)
        segment_lengths.append(length)
        total_length += length

    if total_length == 0.0:
        return points[0], 0.0

    target = total_length / 2.0
    accumulated = 0.0
    for (x1, y1), (x2, y2), segment_length in zip(points, points[1:], segment_lengths):
        if accumulated + segment_length >= target:
            ratio = 0.0 if segment_length == 0 else (target - accumulated) / segment_length
            mid_x = x1 + (x2 - x1) * ratio
            mid_y = y1 + (y2 - y1) * ratio
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
            return (mid_x, mid_y), angle
        accumulated += segment_length

    last_start, last_end = points[-2], points[-1]
    angle = math.degrees(math.atan2(last_end[1] - last_start[1], last_end[0] - last_start[0]))
    return points[-1], angle


def _road_half_width(highway: str) -> float:
    return ROAD_HALF_WIDTH_BY_HIGHWAY.get(highway, 3.5)


def _normalize_vector(dx: float, dy: float) -> tuple[float, float]:
    length = math.hypot(dx, dy)
    if length == 0.0:
        return 0.0, 0.0
    return dx / length, dy / length


def _offset_polyline(points: list[tuple[float, float]], offset: float) -> list[tuple[float, float]]:
    if len(points) < 2:
        return points[:]

    result: list[tuple[float, float]] = []
    for index, (x, y) in enumerate(points):
        if index == 0:
            dx = points[1][0] - x
            dy = points[1][1] - y
            nx, ny = _normalize_vector(-dy, dx)
        elif index == len(points) - 1:
            dx = x - points[index - 1][0]
            dy = y - points[index - 1][1]
            nx, ny = _normalize_vector(-dy, dx)
        else:
            dx1 = x - points[index - 1][0]
            dy1 = y - points[index - 1][1]
            dx2 = points[index + 1][0] - x
            dy2 = points[index + 1][1] - y
            n1x, n1y = _normalize_vector(-dy1, dx1)
            n2x, n2y = _normalize_vector(-dy2, dx2)
            nx, ny = _normalize_vector(n1x + n2x, n1y + n2y)
            if nx == 0.0 and ny == 0.0:
                nx, ny = n2x, n2y

        result.append((x + nx * offset, y + ny * offset))

    return result


def _block_entities(block_name: str, color: int, layer: str) -> list[str]:
    lines: list[str] = []

    def add_entity(entity_type: str, pairs: Iterable[tuple[int, object]]) -> None:
        lines.extend(_pair(0, entity_type))
        for code, value in pairs:
            lines.extend(_pair(code, value))

    common = [(8, layer), (62, color)]

    add_entity(
        "CIRCLE",
        [*common, (10, 0.0), (20, 0.0), (30, 0.0), (40, 1.2)],
    )

    if block_name in {"POE", "PI"}:
        add_entity(
            "LINE",
            [*common, (10, -0.8), (20, 0.0), (30, 0.0), (11, 0.8), (21, 0.0), (31, 0.0)],
        )
        add_entity(
            "LINE",
            [*common, (10, 0.0), (20, -0.8), (30, 0.0), (11, 0.0), (21, 0.8), (31, 0.0)],
        )
    elif block_name in {"PR", "PS"}:
        add_entity(
            "LINE",
            [*common, (10, -0.8), (20, -0.8), (30, 0.0), (11, 0.8), (21, 0.8), (31, 0.0)],
        )
        add_entity(
            "LINE",
            [*common, (10, -0.8), (20, 0.8), (30, 0.0), (11, 0.8), (21, -0.8), (31, 0.0)],
        )

    return lines


def _read_dxf_pairs(filepath: str | Path) -> list[tuple[str, str]]:
    raw_lines = Path(filepath).read_text(encoding="utf-8", errors="ignore").splitlines()
    if len(raw_lines) % 2 != 0:
        raise ValueError(f"Arquivo DXF invalido: quantidade impar de linhas em {filepath}.")
    return [(raw_lines[index].strip(), raw_lines[index + 1].strip()) for index in range(0, len(raw_lines), 2)]


def _extract_section_pairs(pairs: list[tuple[str, str]], section_name: str) -> list[tuple[str, str]]:
    in_section = False
    section_pairs: list[tuple[str, str]] = []
    skip_next = False
    for index, (code, value) in enumerate(pairs):
        if skip_next:
            skip_next = False
            continue
        if code == "0" and value == "SECTION":
            if index + 1 < len(pairs) and pairs[index + 1] == ("2", section_name):
                in_section = True
                skip_next = True
                continue
        if in_section:
            if code == "0" and value == "ENDSEC":
                return section_pairs
            section_pairs.append((code, value))
    return section_pairs


def _pairs_to_records(pairs: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    records: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for code, value in pairs:
        if code == "0" and current:
            records.append(current)
            current = [(code, value)]
        else:
            current.append((code, value))
    if current:
        records.append(current)
    return records


def _record_type(record: list[tuple[str, str]]) -> str:
    return record[0][1].upper() if record else ""


def _record_value(record: list[tuple[str, str]], group_code: str, default: str = "") -> str:
    for code, value in record:
        if code == group_code:
            return value
    return default


def _record_float(record: list[tuple[str, str]], group_code: str, default: float = 0.0) -> float:
    value = _record_value(record, group_code, "")
    if value == "":
        return default
    return float(value)


def _is_metadata_group(group_code: str) -> bool:
    try:
        code = int(group_code)
    except ValueError:
        return False

    if code in {5, 102, 105, 410}:
        return True
    if 320 <= code <= 369:
        return True
    if 390 <= code <= 399:
        return True
    if 1000 <= code <= 1071:
        return True
    return False


def _sanitize_dxf_record(
    record: list[tuple[str, str]], block_rename_map: dict[str, str]
) -> list[tuple[str, str]]:
    entity_type = _record_type(record)
    sanitized: list[tuple[str, str]] = []
    for code, value in record:
        if _is_metadata_group(code):
            continue
        if entity_type == "INSERT" and code == "2" and value in block_rename_map:
            value = block_rename_map[value]
        if entity_type == "HATCH" and code == "71":
            value = "0"
        if entity_type == "HATCH" and code == "97":
            value = "0"
        sanitized.append((code, value))
    return sanitized


def _flatten_record(record: list[tuple[str, str]]) -> list[str]:
    lines: list[str] = []
    for code, value in record:
        lines.extend(_pair(int(code), value) if code.isdigit() else [code, value])
    return lines


def _extract_layers_from_records(records: list[list[tuple[str, str]]]) -> set[str]:
    layers: set[str] = set()
    for record in records:
        layer_name = _record_value(record, "8", "")
        if layer_name:
            layers.add(layer_name)
    return layers


def _parse_custom_blocks(filepath: str | Path) -> list[ExternalBlock]:
    pairs = _read_dxf_pairs(filepath)
    block_section = _extract_section_pairs(pairs, "BLOCKS")
    block_records = _pairs_to_records(block_section)
    custom_blocks: list[ExternalBlock] = []

    index = 0
    while index < len(block_records):
        record = block_records[index]
        if _record_type(record) != "BLOCK":
            index += 1
            continue

        block_name = _record_value(record, "2", "")
        layer_name = _record_value(record, "8", "0")
        base_point = (
            _record_float(record, "10", 0.0),
            _record_float(record, "20", 0.0),
            _record_float(record, "30", 0.0),
        )

        index += 1
        records: list[list[tuple[str, str]]] = []
        while index < len(block_records) and _record_type(block_records[index]) != "ENDBLK":
            records.append(block_records[index])
            index += 1

        if block_name and not block_name.startswith("*"):
            custom_blocks.append(
                ExternalBlock(
                    name=block_name,
                    layer=layer_name,
                    base_point=base_point,
                    records=records,
                )
            )

        while index < len(block_records) and _record_type(block_records[index]) != "ENDBLK":
            index += 1
        if index < len(block_records):
            index += 1

    return custom_blocks


def _read_top_level_entities(filepath: str | Path) -> list[list[tuple[str, str]]]:
    pairs = _read_dxf_pairs(filepath)
    entity_section = _extract_section_pairs(pairs, "ENTITIES")
    return _pairs_to_records(entity_section)


def _build_block_from_records(
    block_name: str,
    layer_name: str,
    base_point: tuple[float, float, float],
    records: list[list[tuple[str, str]]],
    block_rename_map: dict[str, str],
) -> tuple[list[str], set[str]]:
    lines: list[str] = []
    sanitized_records = [_sanitize_dxf_record(record, block_rename_map) for record in records]
    used_layers = _extract_layers_from_records(sanitized_records)
    if layer_name:
        used_layers.add(layer_name)

    lines.extend(_pair(0, "BLOCK"))
    lines.extend(_pair(100, "AcDbEntity"))
    lines.extend(_pair(8, layer_name or "0"))
    lines.extend(_pair(100, "AcDbBlockBegin"))
    lines.extend(_pair(2, block_name))
    lines.extend(_pair(70, 0))
    lines.extend(_pair(10, base_point[0]))
    lines.extend(_pair(20, base_point[1]))
    lines.extend(_pair(30, base_point[2]))
    lines.extend(_pair(3, block_name))
    lines.extend(_pair(1, ""))

    for record in sanitized_records:
        if record:
            lines.extend(_flatten_record(record))

    lines.extend(_pair(0, "ENDBLK"))
    lines.extend(_pair(100, "AcDbEntity"))
    lines.extend(_pair(8, layer_name or "0"))
    lines.extend(_pair(100, "AcDbBlockEnd"))
    return lines, used_layers


def _create_simple_block_definition(block_name: str, color: int, layer: str) -> tuple[list[str], set[str]]:
    lines: list[str] = []

    lines.extend(_pair(0, "BLOCK"))
    lines.extend(_pair(100, "AcDbEntity"))
    lines.extend(_pair(8, layer))
    lines.extend(_pair(100, "AcDbBlockBegin"))
    lines.extend(_pair(2, block_name))
    lines.extend(_pair(70, 0))
    lines.extend(_pair(10, 0.0))
    lines.extend(_pair(20, 0.0))
    lines.extend(_pair(30, 0.0))
    lines.extend(_pair(3, block_name))
    lines.extend(_pair(1, ""))
    lines.extend(_block_entities(block_name, color, layer))
    lines.extend(_pair(0, "ENDBLK"))
    lines.extend(_pair(100, "AcDbEntity"))
    lines.extend(_pair(8, layer))
    lines.extend(_pair(100, "AcDbBlockEnd"))
    return lines, {layer}


def _resolve_external_block_path(block_name: str, blocks_dir: str | Path | None) -> Path | None:
    base_dir = Path(blocks_dir) if blocks_dir else SCRIPT_DIR / DEFAULT_BLOCKS_DIRNAME
    candidates = [
        base_dir / f"{block_name}.dxf",
        base_dir / f"{block_name.upper()}.dxf",
        base_dir / f"{block_name.lower()}.dxf",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _load_external_block_definition(
    block_name: str, fallback_color: int, fallback_layer: str, blocks_dir: str | Path | None
) -> tuple[list[str], set[str]]:
    block_path = _resolve_external_block_path(block_name, blocks_dir)
    if block_path is None:
        return _create_simple_block_definition(block_name, fallback_color, fallback_layer)

    top_level_records = _read_top_level_entities(block_path)
    custom_blocks = _parse_custom_blocks(block_path)
    block_rename_map = {
        source_block.name: f"{block_name}${source_block.name}"
        for source_block in custom_blocks
    }

    lines: list[str] = []
    used_layers: set[str] = set()

    for source_block in custom_blocks:
        renamed_block_name = block_rename_map[source_block.name]
        nested_lines, nested_layers = _build_block_from_records(
            block_name=renamed_block_name,
            layer_name=source_block.layer,
            base_point=source_block.base_point,
            records=source_block.records,
            block_rename_map=block_rename_map,
        )
        lines.extend(nested_lines)
        used_layers.update(nested_layers)

    main_lines, main_layers = _build_block_from_records(
        block_name=block_name,
        layer_name=fallback_layer,
        base_point=(0.0, 0.0, 0.0),
        records=top_level_records,
        block_rename_map=block_rename_map,
    )
    lines.extend(main_lines)
    used_layers.update(main_layers)
    return lines, used_layers


def create_block_definitions(blocks_dir: str | Path | None = None) -> tuple[list[str], set[str]]:
    lines: list[str] = []
    used_layers: set[str] = set()

    for block in CONFIGURED_BLOCK_INFOS:
        block_lines, block_layers = _load_external_block_definition(
            block_name=block.name,
            fallback_color=block.color,
            fallback_layer=block.layer,
            blocks_dir=blocks_dir,
        )
        lines.extend(block_lines)
        used_layers.update(block_layers)

    return lines, used_layers


def _build_layer_table(layer_names: set[str]) -> list[str]:
    normalized_layers = {"0", *layer_names}
    ordered_layers = ["0"] + sorted(layer for layer in normalized_layers if layer != "0")
    lines: list[str] = []
    lines.extend(_pair(0, "TABLE"))
    lines.extend(_pair(2, "LAYER"))
    lines.extend(_pair(70, len(ordered_layers)))

    for layer_name in ordered_layers:
        lines.extend(_pair(0, "LAYER"))
        lines.extend(_pair(2, layer_name))
        lines.extend(_pair(70, 0))
        lines.extend(_pair(62, 7))
        lines.extend(_pair(6, "CONTINUOUS"))

    lines.extend(_pair(0, "ENDTAB"))
    return lines


def _build_layer_table_with_colors(layer_color_map: dict[str, int]) -> list[str]:
    """Tabela LAYER com cores ACI individuais por camada."""
    normalized: dict[str, int] = {"0": 7, **layer_color_map}
    ordered_layers = ["0"] + sorted(name for name in normalized if name != "0")
    lines: list[str] = []
    lines.extend(_pair(0, "TABLE"))
    lines.extend(_pair(2, "LAYER"))
    lines.extend(_pair(70, len(ordered_layers)))
    for layer_name in ordered_layers:
        lines.extend(_pair(0, "LAYER"))
        lines.extend(_pair(2, layer_name))
        lines.extend(_pair(70, 0))
        lines.extend(_pair(62, normalized[layer_name]))
        lines.extend(_pair(6, "CONTINUOUS"))
    lines.extend(_pair(0, "ENDTAB"))
    return lines


def _build_style_table() -> list[str]:
    lines: list[str] = []
    lines.extend(_pair(0, "TABLE"))
    lines.extend(_pair(2, "STYLE"))
    lines.extend(_pair(70, 1))
    lines.extend(_pair(0, "STYLE"))
    lines.extend(_pair(2, "STANDARD"))
    lines.extend(_pair(70, 0))
    lines.extend(_pair(40, 0.0))
    lines.extend(_pair(41, 1.0))
    lines.extend(_pair(50, 0.0))
    lines.extend(_pair(71, 0))
    lines.extend(_pair(42, 2.5))
    lines.extend(_pair(3, "txt"))
    lines.extend(_pair(4, ""))
    lines.extend(_pair(0, "ENDTAB"))
    return lines


def _build_block_record_table(block_names: list[str]) -> list[str]:
    ordered_names = ["*Model_Space", "*Paper_Space", *block_names]
    lines: list[str] = []
    lines.extend(_pair(0, "TABLE"))
    lines.extend(_pair(2, "BLOCK_RECORD"))
    lines.extend(_pair(70, len(ordered_names)))

    for block_name in ordered_names:
        lines.extend(_pair(0, "BLOCK_RECORD"))
        lines.extend(_pair(100, "AcDbSymbolTableRecord"))
        lines.extend(_pair(100, "AcDbBlockTableRecord"))
        lines.extend(_pair(2, block_name))
        lines.extend(_pair(70, 0))
        lines.extend(_pair(280, 1))
        lines.extend(_pair(281, 0))

    lines.extend(_pair(0, "ENDTAB"))
    return lines


def _build_space_block(block_name: str, paper_space: bool = False) -> list[str]:
    lines: list[str] = []
    lines.extend(_pair(0, "BLOCK"))
    lines.extend(_pair(100, "AcDbEntity"))
    if paper_space:
        lines.extend(_pair(67, 1))
    lines.extend(_pair(8, "0"))
    lines.extend(_pair(100, "AcDbBlockBegin"))
    lines.extend(_pair(2, block_name))
    lines.extend(_pair(70, 0))
    lines.extend(_pair(10, 0.0))
    lines.extend(_pair(20, 0.0))
    lines.extend(_pair(30, 0.0))
    lines.extend(_pair(3, block_name))
    lines.extend(_pair(1, ""))
    lines.extend(_pair(0, "ENDBLK"))
    lines.extend(_pair(100, "AcDbEntity"))
    if paper_space:
        lines.extend(_pair(67, 1))
    lines.extend(_pair(8, "0"))
    lines.extend(_pair(100, "AcDbBlockEnd"))
    return lines


def _collect_block_names_from_definition_lines(lines: list[str]) -> list[str]:
    names: list[str] = []
    for index in range(len(lines) - 3):
        if lines[index] == "0" and lines[index + 1] == "BLOCK" and lines[index + 2] == "8":
            probe = index + 4
            while probe < len(lines) - 1:
                if lines[probe] == "2":
                    name = lines[probe + 1]
                    if name not in names:
                        names.append(name)
                    break
                if lines[probe] == "0":
                    break
                probe += 2
    return names


def _import_block_into_doc(doc, block_name: str, fallback_color: int, fallback_layer: str, blocks_dir: str | Path | None) -> None:
    if block_name in doc.blocks:
        return
    block_path = _resolve_external_block_path(block_name, blocks_dir)
    if block_path is None:
        block_layout = doc.blocks.new(name=block_name)
        if fallback_layer not in doc.layers:
            doc.layers.add(fallback_layer, dxfattribs={"color": 7})

        if block_name in {"POE", "PI"}:
            block_layout.add_circle((0.0, 0.0), radius=1.2, dxfattribs={"layer": fallback_layer, "color": fallback_color})
            block_layout.add_line((-0.8, 0.0), (0.8, 0.0), dxfattribs={"layer": fallback_layer, "color": fallback_color})
            block_layout.add_line((0.0, -0.8), (0.0, 0.8), dxfattribs={"layer": fallback_layer, "color": fallback_color})
        elif block_name in {"PR", "PS"}:
            block_layout.add_circle((0.0, 0.0), radius=1.2, dxfattribs={"layer": fallback_layer, "color": fallback_color})
            block_layout.add_line((-0.8, -0.8), (0.8, 0.8), dxfattribs={"layer": fallback_layer, "color": fallback_color})
            block_layout.add_line((-0.8, 0.8), (0.8, -0.8), dxfattribs={"layer": fallback_layer, "color": fallback_color})
        else:
            block_layout.add_circle((0.0, 0.0), radius=1.2, dxfattribs={"layer": fallback_layer, "color": fallback_color})
        return

    source_doc = ezdxf.readfile(str(block_path))
    importer = Importer(source_doc, doc)
    block_layout = doc.blocks.new(name=block_name)
    importer.import_modelspace(block_layout)
    importer.finalize()


def _add_osm_to_doc(
    doc,
    msp,
    epsg: int,
    roads: list[OSMRoad],
 ) -> dict[str, int]:
    for layer_name, color in (
        (ROAD_LAYER, ROAD_EDGE_COLOR),
        (ROAD_LABEL_LAYER, ROAD_LABEL_COLOR),
    ):
        if layer_name not in doc.layers:
            doc.layers.add(layer_name, dxfattribs={"color": color})

    road_count = 0
    for road in roads:
        projected = [convert_coords(lat, lon, epsg) for lat, lon in road.coordinates]
        if len(projected) < 2:
            continue
        half_width = _road_half_width(road.highway)
        left_edge = _offset_polyline(projected, half_width)
        right_edge = _offset_polyline(projected, -half_width)
        road_attribs = {"layer": ROAD_LAYER, "color": ROAD_EDGE_COLOR, "linetype": "DASHED"}
        msp.add_lwpolyline(left_edge, dxfattribs=road_attribs)
        msp.add_lwpolyline(right_edge, dxfattribs=road_attribs)
        if road.name:
            (mid_x, mid_y), angle = _road_midpoint_and_angle(projected)
            label = msp.add_text(
                _sanitize_text(road.name),
                dxfattribs={
                    "layer": ROAD_LABEL_LAYER,
                    "height": ROAD_TEXT_HEIGHT,
                    "style": "STANDARD",
                    "color": ROAD_LABEL_COLOR,
                    "true_color": ROAD_LABEL_TRUE_COLOR,
                },
            )
            label.dxf.insert = (mid_x, mid_y, 0.0)
            label.dxf.rotation = angle
        road_count += 1

    return {"roads": road_count}


def _create_dxf_with_ezdxf(
    waypoints: list[Waypoint],
    output_path: str | Path,
    source_path: str | Path,
    epsg: int,
    zone: int,
    blocks_dir: str | Path | None,
    with_osm: bool = False,
    osm_margin_meters: float = DEFAULT_OSM_MARGIN_METERS,
    overpass_url: str = DEFAULT_OVERPASS_URL,
    with_postes: bool = True,
) -> dict[str, object]:
    doc = ezdxf.new(setup=True)
    doc.units = 6
    doc.header["$INSUNITS"] = 6
    msp = doc.modelspace()

    for block in CONFIGURED_BLOCK_INFOS:
        if block.layer not in doc.layers:
            doc.layers.add(block.layer, dxfattribs={"color": 7})
        _import_block_into_doc(doc, block.name, block.color, block.layer, blocks_dir)
    if HIGHLIGHT_LAYER not in doc.layers:
        doc.layers.add(HIGHLIGHT_LAYER, dxfattribs={"color": HIGHLIGHT_COLOR})

    projected_points: list[tuple[Waypoint, BlockInfo, float, float]] = []
    for waypoint in waypoints:
        block = get_block_info(waypoint.symbol)
        easting, northing = convert_coords(waypoint.latitude, waypoint.longitude, epsg)
        projected_points.append((waypoint, block, easting, northing))

    counts: dict[str, int] = {block_name: 0 for block_name in SUPPORTED_BLOCK_NAMES}
    for waypoint, block, easting, northing in projected_points:
        counts[block.name] += 1
        if with_postes:
            name_label = _sanitize_text(waypoint.name)
            utm_label = format_utm_label(easting, northing)
            circle_x, circle_y, circle_radius = compute_highlight_circle(
                easting,
                northing,
                name_label,
                utm_label,
            )
            msp.add_circle(
                (circle_x, circle_y),
                radius=circle_radius,
                dxfattribs={"layer": HIGHLIGHT_LAYER, "color": HIGHLIGHT_COLOR},
            )
            msp.add_blockref(
                block.name,
                (easting, northing),
                dxfattribs={"layer": block.layer, "xscale": 1.0, "yscale": 1.0, "rotation": 0.0},
            )
            text = msp.add_text(
                name_label,
                dxfattribs={"layer": block.layer, "height": TEXT_HEIGHT, "style": "STANDARD", "color": TEXT_COLOR},
            )
            text.dxf.insert = (easting + TEXT_OFFSET_X, northing + TEXT_OFFSET_Y, 0.0)

            utm_text = msp.add_text(
                utm_label,
                dxfattribs={"layer": block.layer, "height": TEXT_HEIGHT, "style": "STANDARD", "color": TEXT_COLOR},
            )
            utm_text.dxf.insert = (
                easting + TEXT_OFFSET_X,
                northing + TEXT_OFFSET_Y + COORD_TEXT_OFFSET_Y - TEXT_HEIGHT,
                0.0,
            )
        else:
            msp.add_circle(
                (easting, northing),
                radius=POINT_CIRCLE_RADIUS,
                dxfattribs={"layer": HIGHLIGHT_LAYER, "color": HIGHLIGHT_COLOR},
            )

    osm_summary = {"roads": 0}
    osm_bbox: tuple[float, float, float, float] | None = None
    if with_osm:
        roads, osm_bbox = fetch_osm_features(
            waypoints,
            margin_meters=osm_margin_meters,
            overpass_url=overpass_url,
        )
        osm_summary = _add_osm_to_doc(doc, msp, epsg, roads)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(output_path))

    min_x = min(point[2] for point in projected_points)
    min_y = min(point[3] for point in projected_points)
    max_x = max(point[2] for point in projected_points)
    max_y = max(point[3] for point in projected_points)
    return {
        "output_path": str(output_path),
        "epsg": epsg,
        "utm_zone": zone,
        "counts": counts,
        "total_waypoints": len(projected_points),
        "osm": osm_summary,
        "osm_bbox": osm_bbox,
        "extents": {
            "min_easting": round(min_x, 3),
            "max_easting": round(max_x, 3),
            "min_northing": round(min_y, 3),
            "max_northing": round(max_y, 3),
        },
    }


def create_dxf(
    waypoints: list[Waypoint],
    output_path: str | Path,
    source_path: str | Path,
    blocks_dir: str | Path | None = None,
    with_osm: bool = False,
    osm_margin_meters: float = DEFAULT_OSM_MARGIN_METERS,
    overpass_url: str = DEFAULT_OVERPASS_URL,
    with_postes: bool = True,
) -> dict[str, object]:
    first_lat = waypoints[0].latitude
    first_lon = waypoints[0].longitude
    zone, epsg = detect_utm_zone(first_lat, first_lon)

    projected_points: list[tuple[Waypoint, BlockInfo, float, float]] = []
    for waypoint in waypoints:
        waypoint_zone, _ = detect_utm_zone(waypoint.latitude, waypoint.longitude)
        if waypoint_zone != zone:
            raise ValueError(
                "O arquivo GPX contem waypoints em zonas UTM diferentes. "
                f"Zona base: {zone}; waypoint {waypoint.name!r} na zona {waypoint_zone}."
            )
    if ezdxf is not None and Importer is not None:
        return _create_dxf_with_ezdxf(
            waypoints,
            output_path,
            source_path,
            epsg,
            zone,
            blocks_dir,
            with_osm=with_osm,
            osm_margin_meters=osm_margin_meters,
            overpass_url=overpass_url,
            with_postes=with_postes,
        )
    if with_osm:
        import sys as _sys
        _py = _sys.executable
        _causa = f" Causa: {_ezdxf_import_error}" if _ezdxf_import_error is not None else ""
        raise ValueError(
            f"A opcao --with-osm requer a biblioteca ezdxf instalada.{_causa} "
            f"Python em uso: {_py}. "
            f"Instale com: \"{_py}\" -m pip install ezdxf"
        )

    for waypoint in waypoints:
        block = get_block_info(waypoint.symbol)
        easting, northing = convert_coords(waypoint.latitude, waypoint.longitude, epsg)
        projected_points.append((waypoint, block, easting, northing))

    min_x = min(point[2] for point in projected_points)
    min_y = min(point[3] for point in projected_points)
    max_x = max(point[2] for point in projected_points)
    max_y = max(point[3] for point in projected_points)

    block_definition_lines, block_layers = create_block_definitions(blocks_dir=blocks_dir)
    all_layers = {DEFAULT_LAYER, HIGHLIGHT_LAYER, *block_layers}
    custom_block_names = _collect_block_names_from_definition_lines(block_definition_lines)

    lines: list[str] = []
    lines.extend(_pair(999, "Gerado por gpx2dxf.py"))
    lines.extend(_pair(999, f"Arquivo GPX: {Path(source_path).name}"))
    lines.extend(_pair(999, f"EPSG: {epsg}"))
    lines.extend(_pair(999, f"Zona UTM: {zone}"))
    lines.extend(_pair(999, f"Data: {datetime.now().isoformat(timespec='seconds')}"))

    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "HEADER"))
    lines.extend(_pair(9, "$ACADVER"))
    lines.extend(_pair(1, DXF_VERSION))
    lines.extend(_pair(9, "$INSUNITS"))
    lines.extend(_pair(70, 6))
    lines.extend(_pair(9, "$EXTMIN"))
    lines.extend(_pair(10, min_x))
    lines.extend(_pair(20, min_y))
    lines.extend(_pair(30, 0.0))
    lines.extend(_pair(9, "$EXTMAX"))
    lines.extend(_pair(10, max_x))
    lines.extend(_pair(20, max_y))
    lines.extend(_pair(30, 0.0))
    lines.extend(_pair(0, "ENDSEC"))

    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "CLASSES"))
    lines.extend(_pair(0, "ENDSEC"))

    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "TABLES"))
    lines.extend(_build_layer_table(all_layers))
    lines.extend(_build_style_table())
    lines.extend(_build_block_record_table(custom_block_names))
    lines.extend(_pair(0, "ENDSEC"))

    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "BLOCKS"))
    lines.extend(_build_space_block("*Model_Space"))
    lines.extend(_build_space_block("*Paper_Space", paper_space=True))
    lines.extend(block_definition_lines)
    lines.extend(_pair(0, "ENDSEC"))

    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "ENTITIES"))

    counts: dict[str, int] = {block_name: 0 for block_name in SUPPORTED_BLOCK_NAMES}
    for waypoint, block, easting, northing in projected_points:
        counts[block.name] += 1
        if with_postes:
            name_label = _sanitize_text(waypoint.name)
            utm_label = format_utm_label(easting, northing)
            circle_x, circle_y, circle_radius = compute_highlight_circle(
                easting,
                northing,
                name_label,
                utm_label,
            )
            lines.extend(_pair(0, "CIRCLE"))
            lines.extend(_pair(8, HIGHLIGHT_LAYER))
            lines.extend(_pair(62, HIGHLIGHT_COLOR))
            lines.extend(_pair(10, circle_x))
            lines.extend(_pair(20, circle_y))
            lines.extend(_pair(30, 0.0))
            lines.extend(_pair(40, circle_radius))

            lines.extend(_pair(0, "INSERT"))
            lines.extend(_pair(100, "AcDbEntity"))
            lines.extend(_pair(8, block.layer))
            lines.extend(_pair(100, "AcDbBlockReference"))
            lines.extend(_pair(2, block.name))
            lines.extend(_pair(10, easting))
            lines.extend(_pair(20, northing))
            lines.extend(_pair(30, 0.0))
            lines.extend(_pair(41, 1.0))
            lines.extend(_pair(42, 1.0))
            lines.extend(_pair(43, 1.0))
            lines.extend(_pair(50, 0.0))

            lines.extend(_pair(0, "TEXT"))
            lines.extend(_pair(8, block.layer))
            lines.extend(_pair(62, TEXT_COLOR))
            lines.extend(_pair(10, easting + TEXT_OFFSET_X))
            lines.extend(_pair(20, northing + TEXT_OFFSET_Y))
            lines.extend(_pair(30, 0.0))
            lines.extend(_pair(40, TEXT_HEIGHT))
            lines.extend(_pair(1, name_label))
            lines.extend(_pair(7, "STANDARD"))

            lines.extend(_pair(0, "TEXT"))
            lines.extend(_pair(8, block.layer))
            lines.extend(_pair(62, TEXT_COLOR))
            lines.extend(_pair(10, easting + TEXT_OFFSET_X))
            lines.extend(_pair(20, northing + TEXT_OFFSET_Y + COORD_TEXT_OFFSET_Y - TEXT_HEIGHT))
            lines.extend(_pair(30, 0.0))
            lines.extend(_pair(40, TEXT_HEIGHT))
            lines.extend(_pair(1, utm_label))
            lines.extend(_pair(7, "STANDARD"))
        else:
            lines.extend(_pair(0, "CIRCLE"))
            lines.extend(_pair(8, HIGHLIGHT_LAYER))
            lines.extend(_pair(62, HIGHLIGHT_COLOR))
            lines.extend(_pair(10, easting))
            lines.extend(_pair(20, northing))
            lines.extend(_pair(30, 0.0))
            lines.extend(_pair(40, POINT_CIRCLE_RADIUS))

    lines.extend(_pair(0, "ENDSEC"))
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "OBJECTS"))
    lines.extend(_pair(0, "ENDSEC"))
    lines.extend(_pair(0, "EOF"))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "output_path": str(output_path),
        "epsg": epsg,
        "utm_zone": zone,
        "counts": counts,
        "total_waypoints": len(projected_points),
        "osm": {"roads": 0},
        "osm_bbox": None,
        "extents": {
            "min_easting": round(min_x, 3),
            "max_easting": round(max_x, 3),
            "min_northing": round(min_y, 3),
            "max_northing": round(max_y, 3),
        },
    }


def _build_ltype_table() -> list[str]:
    """Tabela LTYPE minima exigida por DXF R2000+: BYLAYER, BYBLOCK e CONTINUOUS."""
    lines: list[str] = []
    lines.extend(_pair(0, "TABLE"))
    lines.extend(_pair(2, "LTYPE"))
    lines.extend(_pair(70, 3))
    for name, desc in (("BYLAYER", ""), ("BYBLOCK", ""), ("CONTINUOUS", "Solid line")):
        lines.extend(_pair(0, "LTYPE"))
        lines.extend(_pair(100, "AcDbSymbolTableRecord"))
        lines.extend(_pair(100, "AcDbLinetypeTableRecord"))
        lines.extend(_pair(2, name))
        lines.extend(_pair(70, 0))
        lines.extend(_pair(3, desc))
        lines.extend(_pair(72, 65))
        lines.extend(_pair(73, 0))
        lines.extend(_pair(40, 0.0))
    lines.extend(_pair(0, "ENDTAB"))
    return lines


def _build_appid_table() -> list[str]:
    """Tabela APPID minima com a entrada ACAD, exigida por DXF R2000+."""
    lines: list[str] = []
    lines.extend(_pair(0, "TABLE"))
    lines.extend(_pair(2, "APPID"))
    lines.extend(_pair(70, 1))
    lines.extend(_pair(0, "APPID"))
    lines.extend(_pair(100, "AcDbSymbolTableRecord"))
    lines.extend(_pair(100, "AcDbRegAppTableRecord"))
    lines.extend(_pair(2, "ACAD"))
    lines.extend(_pair(70, 0))
    lines.extend(_pair(0, "ENDTAB"))
    return lines


def _create_kml_dxf_with_ezdxf(
    segments: list[TramoSegment],
    output_path: str | Path,
    source_path: str | Path,
    zone: int,
    epsg: int,
    projected: list[list[tuple[float, float]]],
    all_x: list[float],
    all_y: list[float],
) -> dict[str, object]:
    """Gera DXF via ezdxf (DXF valido e completo) a partir dos segmentos KML."""
    doc = ezdxf.new("R2010")
    doc.units = 6  # metros

    if TRAMO_LAYER not in doc.layers:
        doc.layers.add(TRAMO_LAYER, dxfattribs={"color": 1})

    msp = doc.modelspace()
    for proj_pts in projected:
        msp.add_lwpolyline(proj_pts, dxfattribs={"layer": TRAMO_LAYER})

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(output_path))

    return {
        "output_path": str(output_path),
        "epsg": epsg,
        "utm_zone": zone,
        "segments": len(segments),
        "extents": {
            "min_easting": round(min(all_x), 3),
            "max_easting": round(max(all_x), 3),
            "min_northing": round(min(all_y), 3),
            "max_northing": round(max(all_y), 3),
        },
    }


def create_kml_dxf(
    segments: list[TramoSegment],
    output_path: str | Path,
    source_path: str | Path,
) -> dict[str, object]:
    """Gera DXF georreferenciado com os segmentos de tramo de um arquivo KML.

    Usa ezdxf quando disponivel (DXF valido e completo); caso contrario usa
    escritor ASCII manual com todas as tabelas DXF R2000+ necessarias.
    """
    if not segments:
        raise ValueError("Nenhum segmento de tramo para exportar.")

    first_lat, first_lon = segments[0].points_wgs84[0]
    zone, epsg = detect_utm_zone(first_lat, first_lon)

    projected: list[list[tuple[float, float]]] = []
    all_x: list[float] = []
    all_y: list[float] = []

    for seg in segments:
        proj_pts: list[tuple[float, float]] = []
        for lat, lon in seg.points_wgs84:
            e, n = convert_coords(lat, lon, epsg)
            proj_pts.append((e, n))
            all_x.append(e)
            all_y.append(n)
        projected.append(proj_pts)

    if ezdxf is not None:
        return _create_kml_dxf_with_ezdxf(
            segments, output_path, source_path, zone, epsg, projected, all_x, all_y
        )

    # --- Fallback: escritor ASCII manual ---
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)

    lines: list[str] = []
    lines.extend(_pair(999, "Gerado por gpx2dxf.py (modo KML Tramo)"))
    lines.extend(_pair(999, f"Arquivo KML: {Path(source_path).name}"))
    lines.extend(_pair(999, f"EPSG: {epsg}"))
    lines.extend(_pair(999, f"Zona UTM: {zone}"))
    lines.extend(_pair(999, f"Segmentos: {len(segments)}"))

    # HEADER
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "HEADER"))
    lines.extend(_pair(9, "$ACADVER"))
    lines.extend(_pair(1, DXF_VERSION))
    lines.extend(_pair(9, "$INSUNITS"))
    lines.extend(_pair(70, 6))
    lines.extend(_pair(9, "$EXTMIN"))
    lines.extend(_pair(10, min_x))
    lines.extend(_pair(20, min_y))
    lines.extend(_pair(30, 0.0))
    lines.extend(_pair(9, "$EXTMAX"))
    lines.extend(_pair(10, max_x))
    lines.extend(_pair(20, max_y))
    lines.extend(_pair(30, 0.0))
    lines.extend(_pair(0, "ENDSEC"))

    # CLASSES
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "CLASSES"))
    lines.extend(_pair(0, "ENDSEC"))

    # TABLES — ordem exigida pelo DXF R2000+
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "TABLES"))
    lines.extend(_build_ltype_table())
    lines.extend(_build_layer_table({TRAMO_LAYER}))
    lines.extend(_build_style_table())
    lines.extend(_build_appid_table())
    lines.extend(_build_block_record_table([]))
    lines.extend(_pair(0, "ENDSEC"))

    # BLOCKS
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "BLOCKS"))
    lines.extend(_build_space_block("*Model_Space"))
    lines.extend(_build_space_block("*Paper_Space", paper_space=True))
    lines.extend(_pair(0, "ENDSEC"))

    # ENTITIES
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "ENTITIES"))

    for proj_pts in projected:
        lines.extend(_pair(0, "LWPOLYLINE"))
        lines.extend(_pair(100, "AcDbEntity"))
        lines.extend(_pair(8, TRAMO_LAYER))
        lines.extend(_pair(100, "AcDbPolyline"))
        lines.extend(_pair(90, len(proj_pts)))
        lines.extend(_pair(70, 0))
        lines.extend(_pair(43, 0.0))
        for e, n in proj_pts:
            lines.extend(_pair(10, e))
            lines.extend(_pair(20, n))

    lines.extend(_pair(0, "ENDSEC"))
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "OBJECTS"))
    lines.extend(_pair(0, "ENDSEC"))
    lines.extend(_pair(0, "EOF"))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "output_path": str(output_path),
        "epsg": epsg,
        "utm_zone": zone,
        "segments": len(segments),
        "extents": {
            "min_easting": round(min_x, 3),
            "max_easting": round(max_x, 3),
            "min_northing": round(min_y, 3),
            "max_northing": round(max_y, 3),
        },
    }


# ---------------------------------------------------------------------------
# Multi-KML: gera DXF combinando varios arquivos KML com cores distintas
# ---------------------------------------------------------------------------

# Tipo interno: (nome_da_layer, cor_ACI, lista_de_polilinhas_projetadas)
_TramoGroup = tuple[str, int, list[list[tuple[float, float]]]]


def _create_kml_dxf_multi_ezdxf(
    groups: list[_TramoGroup],
    output_path: str | Path,
    source_names: list[str],
    zone: int,
    epsg: int,
    all_x: list[float],
    all_y: list[float],
    total_segments: int,
) -> dict[str, object]:
    """Gera DXF multi-tramo via ezdxf com layer e cor por arquivo KML."""
    doc = ezdxf.new("R2010")
    doc.units = 6  # metros
    msp = doc.modelspace()

    for layer_name, color, proj_group in groups:
        if layer_name not in doc.layers:
            doc.layers.add(layer_name, dxfattribs={"color": color})
        for proj_pts in proj_group:
            msp.add_lwpolyline(proj_pts, dxfattribs={"layer": layer_name, "color": color})

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(output_path))

    return {
        "output_path": str(output_path),
        "epsg": epsg,
        "utm_zone": zone,
        "segments": total_segments,
        "files": len(groups),
        "extents": {
            "min_easting": round(min(all_x), 3),
            "max_easting": round(max(all_x), 3),
            "min_northing": round(min(all_y), 3),
            "max_northing": round(max(all_y), 3),
        },
    }


def _create_kml_dxf_multi_ascii(
    groups: list[_TramoGroup],
    output_path: str | Path,
    source_names: list[str],
    zone: int,
    epsg: int,
    all_x: list[float],
    all_y: list[float],
    total_segments: int,
) -> dict[str, object]:
    """Gera DXF multi-tramo (modo ASCII manual) com layer e cor por arquivo KML."""
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)

    layer_color_map = {layer_name: color for layer_name, color, _ in groups}

    lines: list[str] = []
    lines.extend(_pair(999, "Gerado por gpx2dxf.py (modo KML Tramo - multi)"))
    for name in source_names:
        lines.extend(_pair(999, f"Arquivo KML: {name}"))
    lines.extend(_pair(999, f"EPSG: {epsg}"))
    lines.extend(_pair(999, f"Zona UTM: {zone}"))
    lines.extend(_pair(999, f"Segmentos: {total_segments}"))

    # HEADER
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "HEADER"))
    lines.extend(_pair(9, "$ACADVER"))
    lines.extend(_pair(1, DXF_VERSION))
    lines.extend(_pair(9, "$INSUNITS"))
    lines.extend(_pair(70, 6))
    lines.extend(_pair(9, "$EXTMIN"))
    lines.extend(_pair(10, min_x))
    lines.extend(_pair(20, min_y))
    lines.extend(_pair(30, 0.0))
    lines.extend(_pair(9, "$EXTMAX"))
    lines.extend(_pair(10, max_x))
    lines.extend(_pair(20, max_y))
    lines.extend(_pair(30, 0.0))
    lines.extend(_pair(0, "ENDSEC"))

    # CLASSES
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "CLASSES"))
    lines.extend(_pair(0, "ENDSEC"))

    # TABLES
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "TABLES"))
    lines.extend(_build_ltype_table())
    lines.extend(_build_layer_table_with_colors(layer_color_map))
    lines.extend(_build_style_table())
    lines.extend(_build_appid_table())
    lines.extend(_build_block_record_table([]))
    lines.extend(_pair(0, "ENDSEC"))

    # BLOCKS
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "BLOCKS"))
    lines.extend(_build_space_block("*Model_Space"))
    lines.extend(_build_space_block("*Paper_Space", paper_space=True))
    lines.extend(_pair(0, "ENDSEC"))

    # ENTITIES
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "ENTITIES"))

    for layer_name, color, proj_group in groups:
        for proj_pts in proj_group:
            lines.extend(_pair(0, "LWPOLYLINE"))
            lines.extend(_pair(100, "AcDbEntity"))
            lines.extend(_pair(8, layer_name))
            lines.extend(_pair(62, color))
            lines.extend(_pair(100, "AcDbPolyline"))
            lines.extend(_pair(90, len(proj_pts)))
            lines.extend(_pair(70, 0))
            lines.extend(_pair(43, 0.0))
            for e, n in proj_pts:
                lines.extend(_pair(10, e))
                lines.extend(_pair(20, n))

    lines.extend(_pair(0, "ENDSEC"))
    lines.extend(_pair(0, "SECTION"))
    lines.extend(_pair(2, "OBJECTS"))
    lines.extend(_pair(0, "ENDSEC"))
    lines.extend(_pair(0, "EOF"))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "output_path": str(output_path),
        "epsg": epsg,
        "utm_zone": zone,
        "segments": total_segments,
        "files": len(groups),
        "extents": {
            "min_easting": round(min_x, 3),
            "max_easting": round(max_x, 3),
            "min_northing": round(min_y, 3),
            "max_northing": round(max_y, 3),
        },
    }


def create_kml_dxf_multi(
    files_segments: list[tuple[list[TramoSegment], str | Path]],
    output_path: str | Path,
) -> dict[str, object]:
    """Gera DXF combinando tramos de varios arquivos KML.

    Cada arquivo recebe uma layer propria (TRAMO_01, TRAMO_02, ...) e uma cor
    ACI distinta extraida de TRAMO_COLOR_PALETTE, facilitando a distincao visual
    no CAD.
    """
    if not files_segments:
        raise ValueError("Nenhum arquivo KML para exportar.")

    first_lat, first_lon = files_segments[0][0][0].points_wgs84[0]
    zone, epsg = detect_utm_zone(first_lat, first_lon)

    all_x: list[float] = []
    all_y: list[float] = []
    groups: list[_TramoGroup] = []
    total_segments = 0
    source_names: list[str] = []

    for file_index, (segments, source_path) in enumerate(files_segments):
        color = TRAMO_COLOR_PALETTE[file_index % len(TRAMO_COLOR_PALETTE)]
        layer_name = f"TRAMO_{file_index + 1:02d}"
        source_names.append(Path(source_path).name)
        total_segments += len(segments)
        proj_group: list[list[tuple[float, float]]] = []

        for seg in segments:
            proj_pts: list[tuple[float, float]] = []
            for lat, lon in seg.points_wgs84:
                e, n = convert_coords(lat, lon, epsg)
                proj_pts.append((e, n))
                all_x.append(e)
                all_y.append(n)
            proj_group.append(proj_pts)

        groups.append((layer_name, color, proj_group))

    if ezdxf is not None:
        return _create_kml_dxf_multi_ezdxf(
            groups, output_path, source_names, zone, epsg, all_x, all_y, total_segments
        )

    return _create_kml_dxf_multi_ascii(
        groups, output_path, source_names, zone, epsg, all_x, all_y, total_segments
    )


def create_dxf_multi(
    files_waypoints: list[tuple[list[Waypoint], str | Path]],
    output_path: str | Path,
    blocks_dir: str | Path | None = None,
    with_osm: bool = False,
    osm_margin_meters: float = DEFAULT_OSM_MARGIN_METERS,
    overpass_url: str = DEFAULT_OVERPASS_URL,
    with_postes: bool = True,
) -> dict[str, object]:
    """Gera DXF unificado combinando waypoints de varios arquivos GPX.

    Todos os waypoints sao mesclados em um unico arquivo DXF. A zona UTM
    e determinada pelo primeiro waypoint do primeiro arquivo.
    """
    if not files_waypoints:
        raise ValueError("Nenhum arquivo GPX para exportar.")

    all_waypoints: list[Waypoint] = []
    source_names: list[str] = []
    for waypoints, source_path in files_waypoints:
        all_waypoints.extend(waypoints)
        source_names.append(Path(source_path).name)

    if not all_waypoints:
        raise ValueError("Nenhum waypoint encontrado nos arquivos GPX selecionados.")

    first_source = files_waypoints[0][1]
    result = create_dxf(
        all_waypoints,
        output_path,
        first_source,
        blocks_dir=blocks_dir,
        with_osm=with_osm,
        osm_margin_meters=osm_margin_meters,
        overpass_url=overpass_url,
        with_postes=with_postes,
    )
    result["files"] = len(files_waypoints)
    result["source_names"] = source_names
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Converte waypoints Garmin (GPX) ou tramos de rede eletrica (KML) "
            "em DXF georreferenciado com blocos AutoCAD."
        )
    )
    parser.add_argument(
        "input_gpx",
        nargs="?",
        help="Caminho do arquivo .gpx ou .kml de entrada.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Caminho do arquivo .dxf de saida. Padrao: mesmo nome do GPX.",
    )
    parser.add_argument(
        "--blocks-dir",
        default=str(SCRIPT_DIR / DEFAULT_BLOCKS_DIRNAME),
        help="Pasta com DXFs de blocos reais. Padrao: ./Blocos",
    )
    parser.add_argument(
        "--with-osm",
        action="store_true",
        help="Baixa vias do OpenStreetMap e adiciona ao DXF.",
    )
    parser.add_argument(
        "--osm-margin-m",
        type=float,
        default=DEFAULT_OSM_MARGIN_METERS,
        help=f"Margem do recorte OSM em metros ao redor dos waypoints. Padrao: {DEFAULT_OSM_MARGIN_METERS}.",
    )
    parser.add_argument(
        "--overpass-url",
        default=DEFAULT_OVERPASS_URL,
        help=(
            "URL da API Overpass. Pode informar varias URLs separadas por virgula para fallback automatico. "
            f"Padrao inicial: {DEFAULT_OVERPASS_URL}"
        ),
    )
    parser.add_argument(
        "--no-postes",
        action="store_true",
        help=(
            "Gera apenas circulos vermelhos (raio 2m) no local de cada waypoint, "
            "sem inserir os blocos de postes nem os textos de identificacao."
        ),
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Abre um menu interativo no terminal para selecionar GPX e pasta de blocos.",
    )
    return parser


def _open_file_dialog(*, title: str, initialdir: str, filetypes: list[tuple[str, str]]) -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:  # pragma: no cover - depende do Python do usuario
        raise RuntimeError("Tkinter nao esta disponivel neste Python.") from exc

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askopenfilename(title=title, initialdir=initialdir, filetypes=filetypes)
    finally:
        root.destroy()


def _open_multiple_file_dialog(*, title: str, initialdir: str, filetypes: list[tuple[str, str]]) -> list[str]:
    """Abre dialogo de selecao com suporte a multiplos arquivos."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:  # pragma: no cover - depende do Python do usuario
        raise RuntimeError("Tkinter nao esta disponivel neste Python.") from exc

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        result = filedialog.askopenfilenames(title=title, initialdir=initialdir, filetypes=filetypes)
        return list(result)
    finally:
        root.destroy()


def _open_directory_dialog(*, title: str, initialdir: str) -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:  # pragma: no cover - depende do Python do usuario
        raise RuntimeError("Tkinter nao esta disponivel neste Python.") from exc

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(title=title, initialdir=initialdir, mustexist=True)
    finally:
        root.destroy()


def _prompt_path_fallback(prompt: str) -> str:
    return input(f"{prompt}: ").strip()


def _prompt_osm_margin(current_value: float) -> float:
    while True:
        raw_value = input(f"Informe a margem OSM em metros [{current_value}]: ").strip()
        if not raw_value:
            return current_value
        try:
            margin = float(raw_value.replace(",", "."))
        except ValueError:
            print("Valor invalido. Informe um numero.")
            continue
        if margin <= 0:
            print("A margem OSM deve ser maior que zero.")
            continue
        return margin


def _select_gpx_file(current_path: Path | None) -> Path | None:
    initial_dir = str((current_path.parent if current_path else SCRIPT_DIR).resolve())
    try:
        selected = _open_file_dialog(
            title="Selecionar arquivo GPX ou KML",
            initialdir=initial_dir,
            filetypes=[
                ("Arquivos GPX/KML", "*.gpx *.kml"),
                ("Arquivos GPX", "*.gpx"),
                ("Arquivos KML", "*.kml"),
                ("Todos os arquivos", "*.*"),
            ],
        )
    except RuntimeError:
        selected = _prompt_path_fallback("Informe o caminho do arquivo .gpx ou .kml")
    return Path(selected) if selected else None


def _select_blocks_dir(current_path: Path) -> Path | None:
    initial_dir = str(current_path.resolve())
    try:
        selected = _open_directory_dialog(
            title="Selecionar pasta dos blocos",
            initialdir=initial_dir,
        )
    except RuntimeError:
        selected = _prompt_path_fallback("Informe o caminho da pasta de blocos")
    return Path(selected) if selected else None


def _print_main_menu() -> None:
    print()
    print("=" * 60)
    print("    GeoTools")
    print("=" * 60)
    print()
    print("  1. Geracao via GPX    (waypoints Garmin)")
    print("  2. Extracao de Tramo  (arquivo KML)")
    print("  3. Sair")
    print()


def _print_gpx_menu(
    *,
    input_paths: list[Path],
    output_path: Path | None,
    blocks_dir: Path,
    with_osm: bool,
    osm_margin_meters: float,
    with_postes: bool,
) -> None:
    print()
    print("=" * 60)
    print("    GERACAO VIA GPX  --  waypoints Garmin")
    print("=" * 60)
    if input_paths:
        print(f"  Arquivos GPX ({len(input_paths)}):")
        for i, p in enumerate(input_paths, 1):
            print(f"    {i}. {p.name}")
    else:
        print("  Arquivos GPX  : [nenhum selecionado]")
    print(f"  Saida DXF    : {output_path or '[automatica apos escolher arquivo]'}")
    print(f"  Pasta blocos : {blocks_dir}")
    print(f"  Com OSM      : {'Sim' if with_osm else 'Nao'}")
    print(f"  Margem OSM   : {osm_margin_meters} m")
    print(f"  Postes       : {'Sim (blocos + textos)' if with_postes else 'Nao (apenas circulos 2m)'}")
    print()
    print("  1. Selecionar arquivo(s) GPX")
    print("  2. Limpar lista de arquivos")
    print("  3. Selecionar pasta dos blocos")
    print("  4. Alternar OSM")
    print("  5. Alterar margem OSM")
    print("  6. Alternar postes (blocos / apenas circulos)")
    print("  7. Gerar DXF")
    print("  8. Voltar ao menu principal")
    print()


def _print_kml_menu(
    *,
    input_paths: list[Path],
    output_path: Path | None,
) -> None:
    print()
    print("=" * 60)
    print("    EXTRACAO DE TRAMO  --  arquivo(s) KML")
    print("=" * 60)
    if input_paths:
        print(f"  Arquivos KML ({len(input_paths)}):")
        for i, p in enumerate(input_paths, 1):
            color = TRAMO_COLOR_PALETTE[(i - 1) % len(TRAMO_COLOR_PALETTE)]
            print(f"    {i}. {p.name}  [layer TRAMO_{i:02d}, cor ACI {color}]")
    else:
        print("  Arquivos KML  : [nenhum selecionado]")
    print(f"  Saida DXF    : {output_path or '[automatica apos escolher arquivo]'}")
    print()
    print("  1. Selecionar arquivo(s) KML")
    print("  2. Limpar lista de arquivos")
    print("  3. Gerar DXF")
    print("  4. Voltar ao menu principal")
    print()


def _run_gpx_submenu(
    *,
    blocks_dir: str | Path,
    with_osm: bool,
    osm_margin_meters: float,
    overpass_url: str,
    with_postes: bool = True,
) -> None:
    """Loop do submenu GPX. Retorna ao menu principal quando o usuario escolhe 'Voltar'."""
    selected_inputs: list[Path] = []
    selected_output: Path | None = None
    selected_blocks_dir = Path(blocks_dir)

    while True:
        _print_gpx_menu(
            input_paths=selected_inputs,
            output_path=selected_output,
            blocks_dir=selected_blocks_dir,
            with_osm=with_osm,
            osm_margin_meters=osm_margin_meters,
            with_postes=with_postes,
        )
        choice = input("  Escolha uma opcao: ").strip()

        if choice == "1":
            initial_dir = str((selected_inputs[0].parent if selected_inputs else SCRIPT_DIR).resolve())
            try:
                sels = _open_multiple_file_dialog(
                    title="Selecionar arquivo(s) GPX",
                    initialdir=initial_dir,
                    filetypes=[("Arquivos GPX", "*.gpx"), ("Todos os arquivos", "*.*")],
                )
            except RuntimeError:
                raw = _prompt_path_fallback(
                    "  Informe os caminhos dos arquivos .gpx (separados por virgula)"
                )
                sels = [s.strip() for s in raw.split(",") if s.strip()]
            if sels:
                selected_inputs = [Path(s) for s in sels]
                if len(selected_inputs) == 1:
                    selected_output = selected_inputs[0].with_suffix(".dxf")
                else:
                    selected_output = selected_inputs[0].parent / "waypoints_combinados.dxf"

        elif choice == "2":
            selected_inputs = []
            selected_output = None
            print("  Lista de arquivos limpa.")

        elif choice == "3":
            sel = _select_blocks_dir(selected_blocks_dir)
            if sel is not None:
                selected_blocks_dir = sel

        elif choice == "4":
            with_osm = not with_osm

        elif choice == "5":
            osm_margin_meters = _prompt_osm_margin(osm_margin_meters)

        elif choice == "6":
            with_postes = not with_postes
            estado = "Sim (blocos + textos)" if with_postes else "Nao (apenas circulos 2m)"
            print(f"  Postes: {estado}")

        elif choice == "7":
            if not selected_inputs:
                print("  Selecione ao menos um arquivo GPX antes de gerar o DXF.")
                continue
            missing = [p for p in selected_inputs if not p.exists()]
            if missing:
                for p in missing:
                    print(f"  Arquivo nao encontrado: {p}")
                continue
            try:
                if len(selected_inputs) == 1:
                    _run_conversion(
                        input_path=selected_inputs[0],
                        output_path=selected_output or selected_inputs[0].with_suffix(".dxf"),
                        blocks_dir=selected_blocks_dir,
                        with_osm=with_osm,
                        osm_margin_meters=osm_margin_meters,
                        overpass_url=overpass_url,
                        with_postes=with_postes,
                    )
                else:
                    files_waypoints: list[tuple[list[Waypoint], Path]] = []
                    for gpx_path in selected_inputs:
                        wps = parse_gpx(gpx_path)
                        files_waypoints.append((wps, gpx_path))
                    out_path = selected_output or selected_inputs[0].parent / "waypoints_combinados.dxf"
                    result = create_dxf_multi(
                        files_waypoints,
                        out_path,
                        blocks_dir=selected_blocks_dir,
                        with_osm=with_osm,
                        osm_margin_meters=osm_margin_meters,
                        overpass_url=overpass_url,
                        with_postes=with_postes,
                    )
                    counts = result["counts"]
                    print()
                    print("Conversao concluida.")
                    print(f"DXF gerado    : {result['output_path']}")
                    print(f"Arquivos GPX  : {result['files']}")
                    print(f"Waypoints     : {result['total_waypoints']}")
                    print(f"EPSG UTM      : {result['epsg']}")
                    counts_text = ", ".join(f"{bn}={counts[bn]}" for bn in SUPPORTED_BLOCK_NAMES)
                    print(f"Blocos        : {counts_text}")
                    osm_info = result.get("osm", {"roads": 0})
                    if with_osm:
                        print(f"OSM           : vias={osm_info['roads']}")
                    extents = result["extents"]
                    print(
                        "Extensao UTM  : "
                        f"E[{extents['min_easting']}, {extents['max_easting']}] "
                        f"N[{extents['min_northing']}, {extents['max_northing']}]"
                    )
            except Exception as exc:
                print(f"  Erro: {exc}")

        elif choice == "8":
            return

        else:
            print("  Opcao invalida.")


def _run_kml_submenu() -> None:
    """Loop do submenu KML. Retorna ao menu principal quando o usuario escolhe 'Voltar'."""
    selected_inputs: list[Path] = []
    selected_output: Path | None = None

    while True:
        _print_kml_menu(
            input_paths=selected_inputs,
            output_path=selected_output,
        )
        choice = input("  Escolha uma opcao: ").strip()

        if choice == "1":
            initial_dir = str((selected_inputs[0].parent if selected_inputs else SCRIPT_DIR).resolve())
            try:
                sels = _open_multiple_file_dialog(
                    title="Selecionar arquivo(s) KML (tramos de rede)",
                    initialdir=initial_dir,
                    filetypes=[("Arquivos KML", "*.kml"), ("Todos os arquivos", "*.*")],
                )
            except RuntimeError:
                raw = _prompt_path_fallback(
                    "  Informe os caminhos dos arquivos .kml (separados por virgula)"
                )
                sels = [s.strip() for s in raw.split(",") if s.strip()]
            if sels:
                selected_inputs = [Path(s) for s in sels]
                if len(selected_inputs) == 1:
                    selected_output = selected_inputs[0].with_suffix(".dxf")
                else:
                    selected_output = selected_inputs[0].parent / "tramos_combinados.dxf"

        elif choice == "2":
            selected_inputs = []
            selected_output = None
            print("  Lista de arquivos limpa.")

        elif choice == "3":
            if not selected_inputs:
                print("  Selecione ao menos um arquivo KML antes de gerar o DXF.")
                continue
            missing = [p for p in selected_inputs if not p.exists()]
            if missing:
                for p in missing:
                    print(f"  Arquivo nao encontrado: {p}")
                continue
            try:
                files_segments: list[tuple[list[TramoSegment], Path]] = []
                for kml_path in selected_inputs:
                    segs = parse_kml_tramos(kml_path)
                    files_segments.append((segs, kml_path))
                out_path = selected_output or selected_inputs[0].with_suffix(".dxf")
                result = create_kml_dxf_multi(files_segments, out_path)
                print()
                print("Conversao concluida.")
                print(f"DXF gerado    : {result['output_path']}")
                print(f"Arquivos KML  : {result['files']}")
                print(f"Segmentos     : {result['segments']}")
                print(f"EPSG UTM      : {result['epsg']}")
                extents = result["extents"]
                print(
                    "Extensao UTM: "
                    f"E[{extents['min_easting']}, {extents['max_easting']}] "
                    f"N[{extents['min_northing']}, {extents['max_northing']}]"
                )
            except Exception as exc:
                print(f"  Erro: {exc}")

        elif choice == "4":
            return

        else:
            print("  Opcao invalida.")


def _run_conversion(
    *,
    input_path: Path,
    output_path: Path,
    blocks_dir: Path,
    with_osm: bool,
    osm_margin_meters: float,
    overpass_url: str,
    with_postes: bool = True,
) -> int:
    if input_path.suffix.lower() == ".kml":
        segments = parse_kml_tramos(input_path)
        result = create_kml_dxf(segments, output_path, input_path)
        print()
        print("Conversao concluida.")
        print(f"DXF gerado : {result['output_path']}")
        print(f"Segmentos  : {result['segments']}")
        print(f"EPSG UTM   : {result['epsg']}")
        extents = result["extents"]
        print(
            "Extensao UTM: "
            f"E[{extents['min_easting']}, {extents['max_easting']}] "
            f"N[{extents['min_northing']}, {extents['max_northing']}]"
        )
        return 0

    waypoints = parse_gpx(input_path)
    result = create_dxf(
        waypoints,
        output_path,
        input_path,
        blocks_dir=blocks_dir,
        with_osm=with_osm,
        osm_margin_meters=osm_margin_meters,
        overpass_url=overpass_url,
        with_postes=with_postes,
    )

    counts = result["counts"]
    print()
    print("Conversao concluida.")
    print(f"DXF gerado: {result['output_path']}")
    print(f"Waypoints: {result['total_waypoints']}")
    print(f"EPSG UTM: {result['epsg']}")
    counts_text = ", ".join(f"{block_name}={counts[block_name]}" for block_name in SUPPORTED_BLOCK_NAMES)
    print(f"Blocos: {counts_text}")
    osm = result.get("osm", {"roads": 0})
    if with_osm:
        print(f"OSM: vias={osm['roads']}")
    extents = result["extents"]
    print(
        "Extensao UTM: "
        f"E[{extents['min_easting']}, {extents['max_easting']}] "
        f"N[{extents['min_northing']}, {extents['max_northing']}]"
    )
    return 0


def run_interactive_cli(*, blocks_dir: str | Path, with_osm: bool, osm_margin_meters: float, overpass_url: str, with_postes: bool = True) -> int:
    """Menu principal hierarquico. Delega para submenus GPX e KML."""
    while True:
        _print_main_menu()
        choice = input("  Escolha uma opcao: ").strip()

        if choice == "1":
            _run_gpx_submenu(
                blocks_dir=blocks_dir,
                with_osm=with_osm,
                osm_margin_meters=osm_margin_meters,
                overpass_url=overpass_url,
                with_postes=with_postes,
            )

        elif choice == "2":
            _run_kml_submenu()

        elif choice == "3":
            print("  Encerrado.")
            return 0

        else:
            print("  Opcao invalida.")


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    with_postes = not args.no_postes

    if args.interactive or args.input_gpx is None:
        return run_interactive_cli(
            blocks_dir=args.blocks_dir,
            with_osm=args.with_osm,
            osm_margin_meters=args.osm_margin_m,
            overpass_url=args.overpass_url,
            with_postes=with_postes,
        )

    input_path = Path(args.input_gpx)
    if not input_path.exists():
        parser.error(f"Arquivo GPX nao encontrado: {input_path}")

    output_path = Path(args.output) if args.output else input_path.with_suffix(".dxf")

    try:
        return _run_conversion(
            input_path=input_path,
            output_path=output_path,
            blocks_dir=Path(args.blocks_dir),
            with_osm=args.with_osm,
            osm_margin_meters=args.osm_margin_m,
            overpass_url=args.overpass_url,
            with_postes=with_postes,
        )
    except Exception as exc:  # pragma: no cover - saida CLI
        print(f"Erro: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
