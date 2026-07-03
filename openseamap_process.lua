-- openseamap_process.lua
function node_keys(node)
    -- Look for any OpenSeaMap seamark tags
    for k, v in pairs(node) do
        if k:sub(1,8) == "seamark:" then
            Layer("seamarks", false)
            Attribute("type", node["seamark:type"] or "unknown")
            Attribute("name", node["name"] or "")
            -- Copy all specific seamark attributes so your chart plotter can read them
            for seamark_key, seamark_val in pairs(node) do
                if seamark_key:sub(1,8) == "seamark:" then
                    Attribute(seamark_key, seamark_val)
                end
            end
            return
        end
    end
end

function way_keys(way)
    -- Same logic for ways (like continuous reef edges or marine boundaries)
    for k, v in pairs(way) do
        if k:sub(1,8) == "seamark:" then
            Layer("seamarks", true)
            Attribute("type", way["seamark:type"] or "unknown")
            Attribute("name", way["name"] or "")
            return
        end
    end
end