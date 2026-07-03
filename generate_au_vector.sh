#!/bin/bash
set -e

# --- CONFIGURATION ---
WORKING_DIR="$(pwd)"
OUTPUT_DIR="$WORKING_DIR/openseamap_charts"
OUTPUT_FILE="$OUTPUT_DIR/australia_nautical_vector_$(date +%F).mbtiles"

# Geofabrik Oceania Extract URL
PBF_URL="https://download.geofabrik.de/australia-oceania-latest.osm.pbf"
# Australia Bounding Box (min_lon, min_lat, max_lon, max_lat)
BBOX="112.0,-44.0,154.0,-9.0"

mkdir -p "$WORKING_DIR"
mkdir -p "$OUTPUT_DIR"
cd "$WORKING_DIR"

echo "Step 1: Downloading fresh Oceania dataset..."
wget -O oceania.osm.pbf "$PBF_URL"

echo "Step 2: Clipping dataset strictly to Australia..."
osmium extract --bbox="$BBOX" oceania.osm.pbf -o australia.osm.pbf --overwrite

echo "Step 3: Filtering for nautical chart features..."
osmium tags-filter australia.osm.pbf \
  nwr/seamark:type \
  nwr/natural=coastline \
  nwr/natural=water \
  nwr/natural=bay \
  nwr/waterway \
  nwr/harbour \
  nwr/leisure=marina \
  nwr/man_made=pier \
  nwr/man_made=breakwater \
  nwr/man_made=jetty \
  nwr/man_made=groyne \
  nwr/landuse=basin \
  w/depth \
  -o nautical.osm.pbf --overwrite

echo "Step 4: Compiling Vector MBTiles via Tilemaker..."
tilemaker --input nautical.osm.pbf \
          --output "$OUTPUT_FILE" \
          --process "$WORKING_DIR/openseamap_process.lua" \
          --config "$WORKING_DIR/openseamap_config.json"

echo "Step 5: Cleaning up temporary files..."
rm oceania.osm.pbf australia.osm.pbf nautical.osm.pbf

echo "=========================================================="
echo " SUCCESS! Vector MBTiles generated at:"
echo " $OUTPUT_FILE"
echo " Size: $(du -sh "$OUTPUT_FILE" | awk '{print $1}')"
echo "=========================================================="