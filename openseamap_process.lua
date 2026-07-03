-- openseamap_process.lua
-- Tilemaker process file for Australian nautical chart data

-- Pre-filter: tilemaker only passes nodes that carry at least one of these keys.
-- This dramatically reduces processing time.
node_keys = { "seamark:type", "natural", "harbour", "leisure", "man_made" }

function init_function() end
function exit_function() end

-- ============================================================
-- NODE PROCESSING
-- ============================================================
function node_function(node)
    local seamark_type = node:Find("seamark:type")
    local harbour      = node:Find("harbour")
    local leisure      = node:Find("leisure")

    -- Seamarks: buoys, lights, hazards, wrecks, beacons, rocks, etc.
    if seamark_type ~= "" then
        node:Layer("seamarks", false)
        node:Attribute("type",            seamark_type)
        node:Attribute("name",            node:Find("name"))
        -- Lights
        node:Attribute("light_colour",    node:Find("seamark:light:colour"))
        node:Attribute("light_character", node:Find("seamark:light:character"))
        node:Attribute("light_period",    node:Find("seamark:light:period"))
        node:Attribute("light_range",     node:Find("seamark:light:range"))
        node:Attribute("light_height",    node:Find("seamark:light:height"))
        node:Attribute("light_group",     node:Find("seamark:light:group"))
        node:Attribute("light_sequence",  node:Find("seamark:light:sequence"))
        -- Lateral / cardinal buoys and beacons
        node:Attribute("buoy_colour",     node:Find("seamark:buoy_lateral:colour"))
        node:Attribute("buoy_shape",      node:Find("seamark:buoy_lateral:shape"))
        node:Attribute("cardinal_colour", node:Find("seamark:buoy_cardinal:colour"))
        node:Attribute("cardinal_shape",  node:Find("seamark:buoy_cardinal:shape"))
        node:Attribute("beacon_colour",   node:Find("seamark:beacon_lateral:colour"))
        node:Attribute("topmark_shape",   node:Find("seamark:topmark:shape"))
        node:Attribute("topmark_colour",  node:Find("seamark:topmark:colour"))
        -- Hazard details
        node:Attribute("rock_level",      node:Find("seamark:rock:water_level"))
        node:Attribute("wreck_cat",       node:Find("seamark:wreck:category"))
        node:Attribute("hazard_cat",      node:Find("seamark:hazard:category"))
        node:Attribute("obstruction_cat", node:Find("seamark:obstruction:category"))
        -- General
        node:Attribute("status",          node:Find("seamark:status"))
        node:Attribute("information",     node:Find("seamark:information"))
        node:Attribute("category",        node:Find("seamark:category"))
        node:Attribute("depth",           node:Find("seamark:sounding:depth"))
        return
    end

    -- Ports, harbours, and marinas
    if harbour ~= "" or leisure == "marina" then
        node:Layer("harbours", false)
        node:Attribute("name",    node:Find("name"))
        node:Attribute("harbour", harbour)
        node:Attribute("leisure", leisure)
    end
end

-- ============================================================
-- WAY PROCESSING
-- ============================================================
function way_function(way)
    local seamark_type = way:Find("seamark:type")
    local natural      = way:Find("natural")
    local waterway     = way:Find("waterway")
    local harbour      = way:Find("harbour")
    local leisure      = way:Find("leisure")
    local man_made     = way:Find("man_made")
    local depth        = way:Find("depth")

    -- Coastline
    if natural == "coastline" then
        way:Layer("coastline", false)
        return
    end

    -- Water bodies: lakes, bays, reservoirs
    if natural == "water" or natural == "bay" then
        way:Layer("water", true)
        way:Attribute("type", natural)
        return
    end

    -- Waterways: rivers, canals, navigation channels, docks, basins
    if waterway ~= "" then
        local area_waterways = { dock=true, riverbank=true, basin=true }
        if area_waterways[waterway] then
            way:Layer("water", true)
        else
            way:Layer("waterways", false)
        end
        way:Attribute("class", waterway)
        way:Attribute("name",  way:Find("name"))
        return
    end

    -- Depth contour lines
    if depth ~= "" then
        way:Layer("depth_contours", false)
        way:Attribute("depth", depth)
        return
    end

    -- Seamark areas: anchorages, fairways, traffic zones, restricted areas
    if seamark_type ~= "" then
        local area_types = {
            anchorage=true, fairway=true, separation_zone=true,
            restricted_area=true, marine_farm=true, cable_area=true,
            pipeline_area=true, dredged_area=true, precautionary_area=true,
        }
        way:Layer("seamarks", area_types[seamark_type] == true)
        way:Attribute("type", seamark_type)
        way:Attribute("name", way:Find("name"))
        return
    end

    -- Ports, harbours, and marinas as areas
    if harbour ~= "" or leisure == "marina" then
        way:Layer("harbours", true)
        way:Attribute("name",    way:Find("name"))
        way:Attribute("harbour", harbour)
        way:Attribute("leisure", leisure)
        return
    end

    -- Marine infrastructure: piers, breakwaters, jetties, groynes
    if man_made == "pier" or man_made == "breakwater"
    or man_made == "jetty" or man_made == "groyne" then
        way:Layer("infrastructure", true)
        way:Attribute("class", man_made)
        way:Attribute("name",  way:Find("name"))
    end
end