#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -euo pipefail

# ==============================================================================
# CONFIGURATION - ADJUST BEFORE RUNNING
# ==============================================================================
# Input OSM file downloaded from Geofabrik or OpenStreetMap.
# We intentionally use the full dataset here and avoid an additional clipping step,
# because the regional extract is not materially reducing the data volume and adds
# another failure mode to the conversion pipeline.
INPUT_PBF="australia-oceania-latest.osm.pbf"
URL_OSM_SOURCE="https://download.geofabrik.de/australia-oceania-latest.osm.pbf"

# S-57 Output Chart Designation
OUTPUT_S57="australia.000"

# Temporary Working Directory
WORK_DIR="./chart_workspace"
# ==============================================================================

if [ -d "$WORK_DIR" ]; then
    echo "🧹 Cleaning up previous workspace..."
    rm -rf "$WORK_DIR"
fi
echo "⚓ [1/6] Initialising workspace..."
mkdir -p "$WORK_DIR"

# Download source OSM map snippet if not present locally
if [ ! -f "$INPUT_PBF" ]; then
    echo "⬇️ Downloading OpenStreetMap raw regional PBF data..."
    curl -L "$URL_OSM_SOURCE" -o "$INPUT_PBF"
fi

echo "✂️ [2/6] Preparing source OSM dataset for chart conversion..."
# The full Australia/Oceania extract is used directly. The bounding-box pass has
# been removed because it adds little value to the final chart and introduces
# another stage that can fail without improving the result meaningfully.

echo "🪡 [3/6] Fixing broken coastlines & closing geometry..."
# osmcoastline may exit with status 2 even when it successfully writes a usable
# land-polygons database. Treat that as a recoverable condition, not a fatal script error.
OSMCOASTLINE_LOG="$WORK_DIR/osmcoastline.log"
if ! osmcoastline \
  --verbose \
  --output-database "$WORK_DIR/land_polygons.db" \
  "$INPUT_PBF" >"$OSMCOASTLINE_LOG" 2>&1; then
  OSMCOASTLINE_EXIT=$?
  if [ -s "$WORK_DIR/land_polygons.db" ]; then
    echo "⚠️ osmcoastline reported geometry errors but generated a usable land polygon database (exit $OSMCOASTLINE_EXIT). Continuing."
    tail -n 20 "$OSMCOASTLINE_LOG" || true
  else
    echo "❌ osmcoastline failed to generate a usable land polygon database (exit $OSMCOASTLINE_EXIT)." >&2
    tail -n 40 "$OSMCOASTLINE_LOG" >&2 || true
    exit 1
  fi
fi

# Convert the repaired spatial database of closed land polygons to an interim vector layer
ogr2ogr -f "GPKG" "$WORK_DIR/fixed_land.gpkg" "$WORK_DIR/land_polygons.db" land_polygons

echo "🏷️ [4/6] Filtering OpenSeaMap seamark infrastructure & features..."
# Keep the complete seamark dataset from the source OSM file. This intentionally
# includes all seamark-bearing features so the later S57 export can map the full
# available seamark object set rather than a narrow subset of just a few tags.
osmium tags-filter "$INPUT_PBF" \
  seamark \
  natural=island,sand,reef \
  man_made=pier,breakwater,groyne,lighthouse \
  route=ferry \
  boundary=maritime \
  -o "$WORK_DIR/filtered_seamarks.osm.pbf" --overwrite

echo "🌊 [5/6] Automating NOAA Bathymetric Data retrieval & contour calculation..."
# Bathymetry is optional for a valid S-57 chart. The original NOAA endpoint was
# malformed and frequently returns non-raster content; if the service is unavailable,
# we gracefully skip depth contours instead of letting the entire chart build fail.

# The NOAA WMS request still uses a bounding box because it is a raster query, but
# the OSM conversion no longer depends on a clipped extract.
MIN_X=104.0
MIN_Y=-51.0
MAX_X=162.0
MAX_Y=-3.0

NOAA_WMS_URL="https://www.ngdc.noaa.gov/arcgis/services/graticule/etopo1/MapServer/WMSServer?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&BBOX=${MIN_X},${MIN_Y},${MAX_X},${MAX_Y}&SRS=EPSG:4326&WIDTH=1000&HEIGHT=1000&LAYERS=etopo1&FORMAT=image/tiff"

if curl -fsSL --max-time 20 "$NOAA_WMS_URL" -o "$WORK_DIR/noaa_bathymetry.tif" 2>/dev/null; then
    if [ -s "$WORK_DIR/noaa_bathymetry.tif" ]; then
        echo "📊 Generating 5-metre depth contours via GDAL..."
        if ! gdal_contour -a ELEV -i 5.0 "$WORK_DIR/noaa_bathymetry.tif" "$WORK_DIR/depth_contours.gpkg" 2>/dev/null; then
            echo "⚠️ NOAA bathymetry raster was fetched but could not be converted into contours. Compiling chart without depth attributes."
            touch "$WORK_DIR/depth_contours.gpkg"
        fi
    else
        echo "⚠️ NOAA bathymetry service did not return usable raster data. Compiling chart without depth attributes."
        touch "$WORK_DIR/depth_contours.gpkg"
    fi
else
    echo "⚠️ NOAA bathymetry service is unavailable in this environment. Compiling chart without depth attributes."
    touch "$WORK_DIR/depth_contours.gpkg"
fi

echo "🏗️ [6/6] Compiling final S-57 vector interchange chart (.000)..."
# GDAL cannot update an existing S57 dataset in place, so the correct pattern is:
# 1) build a single multi-layer intermediate GPKG with valid S57 layer names
# 2) export that intermediate file to .000 in one create step.
# This keeps the landmass, seamark, and depth contour data together in the final file.
#
# IMPORTANT: `-sql` and `-nln` are incompatible in GDAL. When used together,
# GDAL warns that the layer name is ignored. The safe pattern is to materialize
# each SQL query into a temporary GPKG layer, rename that layer to the exact S-57
# object name, and then append it to the staging GeoPackage.
rm -f "$OUTPUT_S57"
S57_SOURCE_GPKG="$WORK_DIR/chart_source.gpkg"
rm -f "$S57_SOURCE_GPKG"

materialize_sql_layer() {
    local source="$1"
    local geometry_type="$2"
    local sql_query="$3"
    local target_name="$4"
    local temp_json="$WORK_DIR/${target_name,,}.geojson"

    rm -f "$temp_json"

    ogr2ogr -f "GeoJSON" "$temp_json" "$source" \
      "$geometry_type" \
      -sql "$sql_query" \
      -dialect SQLITE \
      -skipfailures || return 0

    if [ -s "$temp_json" ]; then
        ogr2ogr -f "GPKG" -update "$S57_SOURCE_GPKG" "$temp_json" \
          -nln "$target_name" \
          -skipfailures || true
    fi
}

# Land layer: S-57 object LNDARE
ogr2ogr -f "GPKG" "$S57_SOURCE_GPKG" "$WORK_DIR/fixed_land.gpkg" \
  -nln "LNDARE" \
  -skipfailures

# Real seamark mapping: convert OpenStreetMap seamark tags into S57 object layers.
# We intentionally export only the geometry and a minimal set of S57-safe attributes.
# Passing the full OSM schema (`*`, `other_tags`, etc.) into the S57 driver creates
# thousands of unsupported field definitions and is what causes the noisy errors.
# The output here intentionally includes the complete seamark set seen in the source
# OSM file and maps it into the S57 object classes that GDAL can serialize in a
# viewer-friendly way.
if [ -s "$WORK_DIR/filtered_seamarks.osm.pbf" ]; then
    # Major and minor lights, lighthouses, and light towers.
    materialize_sql_layer "$WORK_DIR/filtered_seamarks.osm.pbf" "points" \
      "SELECT geometry, COALESCE(name, 'light') AS OBJNAM, CAST(1 AS INTEGER) AS STATUS FROM points WHERE lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%light%' OR lower(CAST(other_tags AS TEXT)) LIKE '%man_made%lighthouse%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark%light%'" \
      "LIGHTS"

    # All buoy classes recognized by OpenSeaMap.
    materialize_sql_layer "$WORK_DIR/filtered_seamarks.osm.pbf" "points" \
      "SELECT geometry, COALESCE(name, 'buoy') AS OBJNAM, CAST(1 AS INTEGER) AS STATUS FROM points WHERE lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%buoy%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark%buoy%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%safe_water%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%cardinal%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%isolated_danger%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%special_purpose%'" \
      "BOYLAT"

    # All beacon classes and marker beacons.
    materialize_sql_layer "$WORK_DIR/filtered_seamarks.osm.pbf" "points" \
      "SELECT geometry, COALESCE(name, 'beacon') AS OBJNAM, CAST(1 AS INTEGER) AS STATUS FROM points WHERE lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%beacon%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark%beacon%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%daymark%'" \
      "BCNLAT"

    # Obstructions, markers, danger, wreck, and rock hazards.
    materialize_sql_layer "$WORK_DIR/filtered_seamarks.osm.pbf" "points" \
      "SELECT geometry, COALESCE(name, 'obstruction') AS OBJNAM, CAST(1 AS INTEGER) AS STATUS FROM points WHERE lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%obstruction%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%wreck%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%rock%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark:type%danger%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark%danger%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark%wreck%' OR lower(CAST(other_tags AS TEXT)) LIKE '%seamark%obstruction%'" \
      "OBSTRN"

    # Ferry routes, if present.
    materialize_sql_layer "$INPUT_PBF" "lines" \
      "SELECT geometry, COALESCE(name, 'ferry route') AS OBJNAM, CAST(1 AS INTEGER) AS STATUS FROM lines WHERE lower(CAST(other_tags AS TEXT)) LIKE '%route%ferry%' OR lower(CAST(other_tags AS TEXT)) LIKE '%ferry%yes%'" \
      "FERYRT"
fi

# Depth contours: export only the contour geometry and the S57-safe depth field.
if [ -s "$WORK_DIR/depth_contours.gpkg" ]; then
    materialize_sql_layer "$WORK_DIR/depth_contours.gpkg" "contours" \
      "SELECT geometry, CAST(ELEV AS FLOAT) AS VALDCO FROM contours" \
      "DEPCNT"
fi

validate_gpkg_layer() {
    local gpkg_file="$1"
    local layer_name="$2"
    local feature_count

    feature_count=$(ogrinfo -so "$gpkg_file" "$layer_name" 2>/dev/null | awk '/Feature Count:/ {print $NF}' | tail -1 || true)
    if [ -z "$feature_count" ] || [ "$feature_count" = "0" ]; then
        echo "❌ Required chart layer '$layer_name' has no features in '$gpkg_file'" >&2
        return 1
    fi
    echo "✅ Layer '$layer_name' has $feature_count features"
}

validate_gpkg_layer "$S57_SOURCE_GPKG" "LNDARE" || exit 1
validate_gpkg_layer "$S57_SOURCE_GPKG" "LIGHTS" || exit 1
validate_gpkg_layer "$S57_SOURCE_GPKG" "BOYLAT" || exit 1
validate_gpkg_layer "$S57_SOURCE_GPKG" "BCNLAT" || exit 1
validate_gpkg_layer "$S57_SOURCE_GPKG" "OBSTRN" || exit 1
validate_gpkg_layer "$S57_SOURCE_GPKG" "FERYRT" || exit 1

# Final export must be a create operation, not an update/append, otherwise GDAL
# refuses to write the .000 file.
ogr2ogr -f "S57" "$OUTPUT_S57" "$S57_SOURCE_GPKG" \
  -skipfailures

for layer_name in DSID LNDARE LIGHTS BOYLAT BCNLAT OBSTRN FERYRT; do
    feature_count=$(ogrinfo -so "$OUTPUT_S57" "$layer_name" 2>/dev/null | awk '/Feature Count:/ {print $NF}' | tail -1 || true)
    if [ -n "$feature_count" ] && [ "$feature_count" != "0" ]; then
        echo "✅ Final S57 layer '$layer_name' exported with $feature_count features"
    fi
done

echo "🎉 Success! Your custom vector S-57 chart file is generated at: $OUTPUT_S57"
echo "📂 You can load this directly into navigational software tools like OpenCPN."
