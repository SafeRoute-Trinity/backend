"""
Convert OpenRouteService responses to Mapbox-compatible GeoJSON format.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _decode_polyline(encoded: str, precision: int = 5) -> List[List[float]]:
    """
    Decode an encoded polyline string into a list of [lon, lat] coordinates.

    This follows the Google/ORS polyline encoding algorithm.
    """
    if not encoded:
        return []

    coordinates: List[List[float]] = []
    index = 0
    lat = 0
    lon = 0
    factor = 10**precision

    length = len(encoded)
    while index < length:
        # Decode latitude
        result = 0
        shift = 0
        while True:
            if index >= length:
                break
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        delta_lat = ~(result >> 1) if result & 1 else result >> 1
        lat += delta_lat

        # Decode longitude
        result = 0
        shift = 0
        while True:
            if index >= length:
                break
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        delta_lon = ~(result >> 1) if result & 1 else result >> 1
        lon += delta_lon

        coordinates.append([lon / factor, lat / factor])

    return coordinates


def convert_ors_route_to_mapbox(
    ors_response: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Convert OpenRouteService directions response to Mapbox-compatible GeoJSON.

    Supports two response formats:
    1) GeoJSON-style: {"type": "FeatureCollection", "features": [...]}
    2) Routes-style: {"routes": [...], "bbox": [...], "metadata": {...}}

    Args:
        ors_response: OpenRouteService API response

    Returns:
        Mapbox-compatible GeoJSON FeatureCollection with LineString features
    """
    try:
        features: List[Dict[str, Any]] = []

        # Preferred: already GeoJSON (FeatureCollection)
        if "features" in ors_response:
            logger.debug("Converting OpenRouteService GeoJSON response")
            for idx, feature in enumerate(ors_response["features"]):
                if feature.get("geometry", {}).get("type") != "LineString":
                    continue

                # Extract route properties
                properties = feature.get("properties", {})
                summary = properties.get("summary", {})

                mapbox_feature = {
                    "type": "Feature",
                    "geometry": feature["geometry"],
                    "properties": {
                        "route_index": idx,
                        "is_primary": idx == 0,
                        "distance_m": int(summary.get("distance", 0)),
                        "duration_s": int(summary.get("duration", 0)),
                        # Additional properties for Mapbox
                        "distance": summary.get("distance", 0) / 1000,  # km
                        "duration": summary.get("duration", 0) / 60,  # minutes
                    },
                }
                features.append(mapbox_feature)

        # Fallback: routes-style JSON (no "features" key)
        elif "routes" in ors_response:
            logger.debug("Converting OpenRouteService 'routes' response (non-GeoJSON) to GeoJSON")
            for idx, route in enumerate(ors_response.get("routes", [])):
                geometry = route.get("geometry")
                if not geometry:
                    logger.warning("Route is missing 'geometry' field; skipping this route")
                    continue

                # Decode encoded polyline geometry into coordinates
                coords = _decode_polyline(geometry, precision=5)
                if not coords:
                    logger.warning("Decoded geometry is empty for route; skipping this route")
                    continue

                summary = route.get("summary", {})

                mapbox_feature = {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords,
                    },
                    "properties": {
                        "route_index": idx,
                        "is_primary": idx == 0,
                        "distance_m": int(summary.get("distance", 0)),
                        "duration_s": int(summary.get("duration", 0)),
                        "distance": summary.get("distance", 0) / 1000,  # km
                        "duration": summary.get("duration", 0) / 60,  # minutes
                    },
                }
                features.append(mapbox_feature)
        else:
            logger.error("Invalid OpenRouteService response: missing both 'features' and 'routes'")
            return None

        if not features:
            logger.warning("No valid LineString features found in OpenRouteService response")
            return None

        return {
            "type": "FeatureCollection",
            "features": features,
        }

    except Exception as e:
        logger.error(
            f"Error converting OpenRouteService route to Mapbox format: {e}",
            exc_info=True,
        )
        return None


def convert_ors_isochrone_to_mapbox(
    ors_response: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Convert OpenRouteService isochrones response to Mapbox-compatible GeoJSON.

    Args:
        ors_response: OpenRouteService API response

    Returns:
        Mapbox-compatible GeoJSON FeatureCollection with Polygon features
    """
    try:
        if "features" not in ors_response:
            logger.error("Invalid OpenRouteService response: missing 'features'")
            return None

        features = []
        for feature in ors_response["features"]:
            if feature.get("geometry", {}).get("type") not in [
                "Polygon",
                "MultiPolygon",
            ]:
                continue

            # Extract isochrone properties
            properties = feature.get("properties", {})
            value = properties.get("value", 0)  # Time or distance value

            # Create Mapbox-compatible feature
            mapbox_feature = {
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {
                    "value": value,
                    "range_type": properties.get("range_type", "time"),
                },
            }

            features.append(mapbox_feature)

        if not features:
            logger.warning("No valid Polygon features found in OpenRouteService response")
            return None

        return {
            "type": "FeatureCollection",
            "features": features,
        }

    except Exception as e:
        logger.error(
            f"Error converting OpenRouteService isochrone to Mapbox format: {e}",
            exc_info=True,
        )
        return None
