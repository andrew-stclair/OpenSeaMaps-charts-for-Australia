#!/usr/bin/python3
"""
osm_to_enc.py
Converts a filtered nautical OSM PBF to IHO S-57 ENC format (.000)
by writing the ISO 8211 binary directly — no GDAL write path required.

Pipeline:  OSM PBF → pyosmium → ISO 8211 binary writer → .000

Requires:  sudo apt-get install python3-pyosmium
Usage:     /usr/bin/python3 osm_to_enc.py <input.osm.pbf> <output.000>
"""

import base64
import os
import struct
import sys
from datetime import date

try:
    import osmium
except ImportError:
    sys.exit("ERROR: pyosmium not found.  Run: sudo apt-get install python3-pyosmium")

# ── ISO 8211 / S-57 wire constants ────────────────────────────────────────────

FT   = b"\x1e"    # Field Terminator
UT   = b"\x1f"    # Unit Separator
RT   = b"\x1d"    # Record Terminator

COMF = 10_000_000  # Coordinate Multiplication Factor
SOMF = 10          # Sounding Multiplication Factor

# RCNM (Record Name) codes
RCNM_DS = 10    # Data Set  (DSID / DSPM)
RCNM_VI = 110   # Isolated Node  ← point features
RCNM_VE = 130   # Edge           ← line features
RCNM_VF = 130   # Area ring: chain-node topology uses VE (Edge) for area boundaries
RCNM_FE = 100   # Feature Record

# PRIM (geometry primitive) codes for FRID
PRIM_POINT = 1
PRIM_LINE  = 2
PRIM_AREA  = 3

# ── S-57 catalog codes ────────────────────────────────────────────────────────
# Object codes (OBJL) — from /usr/share/gdal/s57objectclasses.csv
OBJL = {
    "BOYLAT": 17,  "BOYCAR": 14,  "BOYISD": 16,  "BOYSAW": 18,  "BOYSPP": 19,
    "BCNLAT":  7,  "BCNCAR":  5,  "BCNISD":  6,  "BCNSAW":  8,  "BCNSPP":  9,
    "LIGHTS": 75,  "LITVES": 77,  "LITFLT": 76,
    "UWTROC": 153, "WRECKS": 159, "OBSTRN": 86,
    "MORFAC": 84,  "HRBFAC": 64,  "SMCFAC": 128, "PILPNT": 90,  "SOUNDG": 129,
    "COALNE": 30,  "DEPCNT": 43,  "SLCONS": 122,
    "ACHARE":  4,  "FAIRWY": 51,  "TSEZNE": 150, "RESARE": 112,
    "MARFAR": 315, "CBLARE": 20,  "PIPARE": 92,  "PRCARE": 96,
    "DRGARE": 46,  "HRBARE": 63,
}

# Attribute codes (ATTL) — from /usr/share/gdal/s57attributes.csv
ATTL = {
    "COLOUR": 75, "OBJNAM": 116, "BOYSHP":  4, "LITCHR": 107,
    "SIGPER": 142, "HEIGHT": 95, "VALNMR": 178, "CATWRK": 71,
    "WATLEV": 187, "VALDCO": 174,
}

# ── OpenSeaMap seamark:type → S-57 object code ────────────────────────────────

SEAMARK_POINT_MAP = {
    "buoy_lateral":           "BOYLAT",
    "buoy_cardinal":          "BOYCAR",
    "buoy_isolated_danger":   "BOYISD",
    "buoy_safe_water":        "BOYSAW",
    "buoy_special_purpose":   "BOYSPP",
    "beacon_lateral":         "BCNLAT",
    "beacon_cardinal":        "BCNCAR",
    "beacon_isolated_danger": "BCNISD",
    "beacon_safe_water":      "BCNSAW",
    "beacon_special_purpose": "BCNSPP",
    "light":                  "LIGHTS",
    "light_vessel":           "LITVES",
    "light_float":            "LITFLT",
    "rock":                   "UWTROC",
    "wreck":                  "WRECKS",
    "obstruction":            "OBSTRN",
    "mooring":                "MORFAC",
    "harbour":                "HRBFAC",
    "marina":                 "SMCFAC",
    "sounding":               "SOUNDG",
    "pile":                   "PILPNT",
}

SEAMARK_AREA_MAP = {
    "anchorage":          "ACHARE",
    "fairway":            "FAIRWY",
    "separation_zone":    "TSEZNE",
    "restricted_area":    "RESARE",
    "marine_farm":        "MARFAR",
    "cable_area":         "CBLARE",
    "pipeline_area":      "PIPARE",
    "precautionary_area": "PRCARE",
    "dredged_area":       "DRGARE",
}

MAN_MADE_MAP = {
    "breakwater": "SLCONS",
    "jetty":      "SLCONS",
    "pier":       "SLCONS",
    "groyne":     "SLCONS",
}

# ── Attribute lookup tables ───────────────────────────────────────────────────

COLOUR_MAP = {
    "white":"1","black":"2","red":"3","green":"4","blue":"5",
    "yellow":"6","grey":"7","gray":"7","brown":"8","amber":"9",
    "violet":"10","orange":"11","magenta":"12","pink":"13",
}
LITCHR_MAP = {
    "F":"1","Fl":"2","LFl":"3","Q":"4","VQ":"5","UQ":"6",
    "Iso":"7","Oc":"8","IQ":"9","IVQ":"10","IUQ":"11","Mo":"12",
    "FFl":"13","Al":"16",
}
BOYSHP_MAP = {
    "conical":"1","can":"2","spherical":"3","pillar":"4",
    "spar":"5","barrel":"6","super-buoy":"7","ice-buoy":"8",
}
CATWRK_MAP = {
    "non-dangerous":"1","dangerous":"2","distributed_remains":"3",
    "mast_showing":"4","hull_showing":"5",
}
WATLEV_MAP = {
    "submerged":"3","always_dry":"4","awash":"5",
    "covers_and_uncovers":"6","intertidal":"7",
}

_COLOUR_KEYS = (
    "seamark:light:colour","seamark:buoy_lateral:colour",
    "seamark:buoy_cardinal:colour","seamark:buoy_safe_water:colour",
    "seamark:buoy_isolated_danger:colour","seamark:beacon_lateral:colour",
    "seamark:beacon_cardinal:colour","seamark:colour","colour",
)
_SHAPE_KEYS = (
    "seamark:buoy_lateral:shape","seamark:buoy_cardinal:shape",
    "seamark:buoy_safe_water:shape","seamark:buoy_isolated_danger:shape",
    "seamark:buoy:shape",
)

def _parse_colour(raw):
    parts = [p.strip().lower() for p in raw.replace(";",",").split(",")]
    codes = [COLOUR_MAP[p] for p in parts if p in COLOUR_MAP]
    return ",".join(codes) if codes else None

# ── OSM handler ───────────────────────────────────────────────────────────────

class NauticalHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.points = []   # (s57_code, lon, lat, tags_dict)
        self.lines  = []   # (s57_code, [(lon,lat),...], tags_dict)
        self.areas  = []   # (s57_code, [(lon,lat),...], tags_dict)

    def node(self, n):
        if not n.location.valid():
            return
        tags = dict(n.tags)
        st   = tags.get("seamark:type","")
        if st in SEAMARK_POINT_MAP:
            self.points.append((SEAMARK_POINT_MAP[st],n.location.lon,n.location.lat,tags))
        elif tags.get("harbour") or tags.get("leisure")=="marina":
            self.points.append(("HRBFAC",n.location.lon,n.location.lat,tags))

    def way(self, w):
        tags = dict(w.tags)
        try:
            coords = [(nd.lon,nd.lat) for nd in w.nodes if nd.location.valid()]
        except Exception:
            return
        if len(coords) < 2:
            return
        is_closed = len(coords)>=4 and coords[0]==coords[-1]
        nat = tags.get("natural","")
        dep = tags.get("depth","")
        st  = tags.get("seamark:type","")
        mm  = tags.get("man_made","")
        ww  = tags.get("waterway","")
        if nat=="coastline":
            self.lines.append(("COALNE",coords,tags))
        elif dep:
            self.lines.append(("DEPCNT",coords,tags))
        elif st in SEAMARK_AREA_MAP:
            if is_closed:
                self.areas.append((SEAMARK_AREA_MAP[st],coords,tags))
            else:
                self.lines.append((SEAMARK_AREA_MAP[st],coords,tags))
        elif st in SEAMARK_POINT_MAP:
            lon=sum(c[0] for c in coords)/len(coords)
            lat=sum(c[1] for c in coords)/len(coords)
            self.points.append((SEAMARK_POINT_MAP[st],lon,lat,tags))
        elif mm in MAN_MADE_MAP:
            code=MAN_MADE_MAP[mm]
            if is_closed: self.areas.append((code,coords,tags))
            else:         self.lines.append((code,coords,tags))
        elif ww in ("dock","basin") and is_closed:
            self.areas.append(("HRBARE",coords,tags))

# ── Attribute builder ─────────────────────────────────────────────────────────

def build_attf(tags, code):
    """Return list of (attl_code, value_str) pairs for ATTF field."""
    attrs = []
    name = tags.get("name","")
    if name:
        attrs.append((ATTL["OBJNAM"], name))
    for k in _COLOUR_KEYS:
        raw = tags.get(k,"")
        if raw:
            col = _parse_colour(raw)
            if col: attrs.append((ATTL["COLOUR"], col))
            break
    if code in ("LIGHTS","LITVES","LITFLT"):
        char = tags.get("seamark:light:character","")
        if char in LITCHR_MAP: attrs.append((ATTL["LITCHR"], LITCHR_MAP[char]))
        for a,k in (("SIGPER","seamark:light:period"),("HEIGHT","seamark:light:height"),("VALNMR","seamark:light:range")):
            raw=tags.get(k,"")
            if raw:
                try: attrs.append((ATTL[a],str(float(raw))))
                except ValueError: pass
    if code in ("BOYLAT","BOYCAR","BOYISD","BOYSAW","BOYSPP","BCNLAT","BCNCAR","BCNISD","BCNSAW","BCNSPP"):
        for k in _SHAPE_KEYS:
            sv=tags.get(k,"").lower()
            if sv in BOYSHP_MAP: attrs.append((ATTL["BOYSHP"],BOYSHP_MAP[sv])); break
    if code=="DEPCNT":
        raw=tags.get("depth","")
        if raw:
            try: attrs.append((ATTL["VALDCO"],str(float(raw))))
            except ValueError: pass
    if code=="WRECKS":
        cat=tags.get("seamark:wreck:category","").lower()
        if cat in CATWRK_MAP: attrs.append((ATTL["CATWRK"],CATWRK_MAP[cat]))
    if code=="UWTROC":
        wl=tags.get("seamark:rock:water_level","").lower()
        if wl in WATLEV_MAP: attrs.append((ATTL["WATLEV"],WATLEV_MAP[wl]))
    return attrs

# ── ISO 8211 / S-57 binary writer ────────────────────────────────────────────

# DDR verbatim from a GDAL reference file — defines all field schemas.
# Data record leader entry map: tag=4, len=5, pos=5.

_DDR_B64 = "MDIwNzYzTEUxIDA5MDAyNDUgISAzNDA0MDAwMDE1NTAwMDAwMDAxMDQyMDE1NURTSUQxNzYwMTk3RFNTSTE0NzAzNzNEU1BNMTUxMDUyMFZSSUQwNzgwNjcxVlJQQzA3NDA3NDlWUlBUMDg3MDgyM0FUVFYwNTgwOTEwU0dDQzA2MzA5NjhTRzJEMDUxMTAzMVNHM0QwNzcxMDgyRlJJRDEwNjExNTlGT0lEMDcwMTI2NUFUVEYwNTkxMzM1TkFURjA2ODEzOTRGRlBDMDkzMTQ2MkZGUFQwODYxNTU1RlNQQzA5MzE2NDFGU1BUMDk3MTczNB4wMDAwOyYgICAfMDAwMURTSUREU0lERFNTSTAwMDFEU1BNMDAwMVZSSURWUklEQVRUVlZSSURWUlBDVlJJRFZSUFRWUklEU0dDQ1ZSSURTRzJEVlJJRFNHM0QwMDAxRlJJREZSSURGT0lERlJJREFUVEZGUklETkFURkZSSURGRlBDRlJJREZGUFRGUklERlNQQ0ZSSURGU1BUHjA1MDA7JiAgIElTTyA4MjExIFJlY29yZCBJZGVudGlmaWVyHyhiMTIpHjE2MDA7JiAgIERhdGEgc2V0IGlkZW50aWZpY2F0aW9uIGZpZWxkH1JDTk0hUkNJRCFFWFBQIUlOVFUhRFNOTSFFRFROIVVQRE4hVUFEVCFJU0RUIVNURUQhUFJTUCFQU0ROIVBSRUQhUFJPRiFBR0VOIUNPTVQfKGIxMSxiMTQsYjExLGIxMSxBLEEsQSxBKDgpLEEoOCksUig0KSxiMTEsQSxBLGIxMSxiMTIsQSkeMTYwMDsmICAgRGF0YSBzZXQgc3RydWN0dXJlIGluZm9ybWF0aW9uIGZpZWxkH0RTVFIhQUFMTCFOQUxMIU5PTVIhTk9DUiFOT0dSIU5PTFIhTk9JTiFOT0NOIU5PRUQhTk9GQR8oYjExLGIxMSxiMTEsYjE0LGIxNCxiMTQsYjE0LGIxNCxiMTQsYjE0LGIxNCkeMTYwMDsmICAgRGF0YSBzZXQgcGFyYW1ldGVyIGZpZWxkH1JDTk0hUkNJRCFIREFUIVZEQVQhU0RBVCFDU0NMIURVTkkhSFVOSSFQVU5JIUNPVU4hQ09NRiFTT01GIUNPTVQfKGIxMSxiMTQsYjExLGIxMSxiMTEsYjE0LGIxMSxiMTEsYjExLGIxMSxiMTQsYjE0LEEpHjE2MDA7JiAgIFZlY3RvciByZWNvcmQgaWRlbnRpZmllciBmaWVsZB9SQ05NIVJDSUQhUlZFUiFSVUlOHyhiMTEsYjE0LGIxMixiMTEpHjE2MDA7JiAgIFZlY3RvciBSZWNvcmQgUG9pbnRlciBDb250cm9sIGZpZWxkH1ZQVUkhVlBJWCFOVlBUHyhiMTEsYjEyLGIxMikeMjYwMDsmICAgVmVjdG9yIHJlY29yZCBwb2ludGVyIGZpZWxkHypOQU1FIU9STlQhVVNBRyFUT1BJIU1BU0sfKEIoNDApLGIxMSxiMTEsYjExLGIxMSkeMjYwMDsmICAgVmVjdG9yIHJlY29yZCBhdHRyaWJ1dGUgZmllbGQfKkFUVEwhQVRWTB8oYjEyLEEpHjE2MDA7JiAgIENvb3JkaW5hdGUgQ29udHJvbCBGaWVsZB9DQ1VJIUNDSVghQ0NOQx8oYjExLGIxMixiMTIpHjI1MDA7JiAgIDItRCBjb29yZGluYXRlIGZpZWxkHypZQ09PIVhDT08fKGIyNCxiMjQpHjI1MDA7JiAgIDMtRCBjb29yZGluYXRlIChzb3VuZGluZyBhcnJheSkgZmllbGQfKllDT08hWENPTyFWRTNEHyhiMjQsYjI0LGIyNCkeMTYwMDsmICAgRmVhdHVyZSByZWNvcmQgaWRlbnRpZmllciBmaWVsZB9SQ05NIVJDSUQhUFJJTSFHUlVQIU9CSkwhUlZFUiFSVUlOHyhiMTEsYjE0LGIxMSxiMTEsYjEyLGIxMixiMTEpHjE2MDA7JiAgIEZlYXR1cmUgb2JqZWN0IGlkZW50aWZpZXIgZmllbGQfQUdFTiFGSUROIUZJRFMfKGIxMixiMTQsYjEyKR4yNjAwOyYgICBGZWF0dXJlIHJlY29yZCBhdHRyaWJ1dGUgZmllbGQfKkFUVEwhQVRWTB8oYjEyLEEpHjI2MDA7JiAgIEZlYXR1cmUgcmVjb3JkIG5hdGlvbmFsIGF0dHJpYnV0ZSBmaWVsZB8qQVRUTCFBVFZMHyhiMTIsQSkeMTYwMDsmICAgRmVhdHVyZSByZWNvcmQgdG8gZmVhdHVyZSBvYmplY3QgcG9pbnRlciBjb250cm9sIGZpZWxkH0ZGVUkhRkZJWCFORlBUHyhiMTEsYjEyLGIxMikeMjYwMDsmICAgRmVhdHVyZSByZWNvcmQgdG8gZmVhdHVyZSBvYmplY3QgcG9pbnRlciBmaWVsZB8qTE5BTSFSSU5EIUNPTVQfKEIoNjQpLGIxMSxBKR4xNjAwOyYgICBGZWF0dXJlIHJlY29yZCB0byBzcGF0aWFsIHJlY29yZCBwb2ludGVyIGNvbnRyb2wgZmllbGQfRlNVSSFGU0lYIU5TUFQfKGIxMSxiMTIsYjEyKR4yNjAwOyYgICBGZWF0dXJlIHJlY29yZCB0byBzcGF0aWFsIHJlY29yZCBwb2ludGVyIGZpZWxkHypOQU1FIU9STlQhVVNBRyFNQVNLHyhCKDQwKSxiMTEsYjExLGIxMSke"

_DDR = base64.b64decode(_DDR_B64)

def _u8(v):  return struct.pack("B",  v)
def _u16(v): return struct.pack("<H", v)
def _u32(v): return struct.pack("<I", v)
def _i32(v): return struct.pack("<i", v)

def _make_dr(fields):
    """
    Build a data record (ISO 8211 DR).
    fields: list of (tag, field_bytes) — field_bytes must NOT include 0x1E.
    Returns: complete record bytes including all terminators.
    """
    # Data records use entry map: tag=4, len=5, pos=5
    directory = b""
    field_area = b""
    pos = 0
    for tag, data in fields:
        fdata = data + FT           # append field terminator
        flen  = len(fdata)
        directory += tag.encode("ascii") + f"{flen:05d}".encode() + f"{pos:05d}".encode()
        field_area += fdata
        pos += flen
    directory += FT                 # directory field terminator
    ba = 24 + len(directory)
    rl = ba + len(field_area) + 1   # +1 for record terminator
    leader = (
        f"{rl:05d}".encode() +
        b" D     " +                # IL=' ' LI='D' CEI=' ' VN=' ' AI=' ' FCL='  '
        f"{ba:05d}".encode() +
        b"   5504"                  # ext_charset='   ' len=5 pos=5 rsv='0' tag=4
    )
    return leader + directory + field_area + RT


class S57Writer:
    """Writes an IHO S-57 ENC (.000) file using direct ISO 8211 binary encoding."""

    def __init__(self, filepath, dataset_name="AU_NAUTICAL", cscl=90000):
        self._fp = open(filepath, "wb")
        self._name  = dataset_name
        self._cscl  = cscl
        self._vrcid = 1   # VRID record counter
        self._frcid = 1   # FRID record counter
        self._fidn  = 1   # Feature Object ID counter
        self._fp.write(_DDR)
        self._write_dsid()
        self._write_dspm()

    # ── Header records ────────────────────────────────────────────────────────

    def _write_dsid(self):
        today = date.today().strftime("%Y%m%d").encode()
        f_0001 = _u16(RCNM_DS)                       # (b12) RCNM for DSID record
        f_dsid = (
            _u8(10)  + _u32(1)  +                    # RCNM=10, RCID=1
            _u8(1)   + _u8(4)   +                    # EXPP=1(new), INTU=4(harbour)
            self._name.encode("latin-1") + UT +      # DSNM
            b"1" + UT + b"0" + UT +                  # EDTN, UPDN
            today + today +                          # UADT(8), ISDT(8) — fixed length
            b"3.10" +                                # STED(4) — fixed length
            _u8(1)   +                               # PRSP=1(ENC)
            UT +                                     # PSDN empty
            b"INIT" + UT +                           # PRED
            _u8(1)   +                               # PROF=1(EN)
            _u16(0)                                  # AGEN=0 (no agency)
            # COMT empty → field terminator comes from _make_dr
        )
        f_dssi = (
            _u8(2) + _u8(0) + _u8(0) +              # DSTR=2, AALL=0, NALL=0
            _u32(0) * 8                              # NOMR..NOFA = 0
        )
        self._fp.write(_make_dr([("0001",f_0001),("DSID",f_dsid),("DSSI",f_dssi)]))

    def _write_dspm(self):
        f_0001 = _u16(20)                            # RCNM for DSPM
        f_dspm = (
            _u8(20) + _u32(1) +                      # RCNM=20, RCID=1
            _u8(2)  + _u8(12) + _u8(23) +           # HDAT=2(WGS84), VDAT=12, SDAT=23
            _u32(self._cscl) +                       # CSCL
            _u8(1) * 4 +                             # DUNI=1,HUNI=1,PUNI=1,COUN=1
            _u32(COMF) + _u32(SOMF)                  # COMF, SOMF
            # COMT empty
        )
        self._fp.write(_make_dr([("0001",f_0001),("DSPM",f_dspm)]))

    # ── Vector records ────────────────────────────────────────────────────────

    def _write_vrid_point(self, lon, lat):
        """Write a VRID (Isolated Node) + SG2D record. Returns RCID."""
        rcid = self._vrcid; self._vrcid += 1
        f_0001 = _u16(RCNM_VI)
        f_vrid = _u8(RCNM_VI) + _u32(rcid) + _u16(1) + _u8(1)   # RCNM,RCID,RVER=1,RUIN=1
        f_sg2d = _i32(round(lat*COMF)) + _i32(round(lon*COMF))   # YCOO, XCOO
        self._fp.write(_make_dr([("0001",f_0001),("VRID",f_vrid),("SG2D",f_sg2d)]))
        return rcid

    def _write_vc_node(self, lon, lat):
        """Write a Connected Node (VC, RCNM=120) record with SG2D. Returns RCID."""
        rcid = self._vrcid; self._vrcid += 1
        f_0001 = _u16(120)                              # RCNM_VC = 120
        f_vrid = _u8(120) + _u32(rcid) + _u16(1) + _u8(1)
        f_sg2d = _i32(round(lat * COMF)) + _i32(round(lon * COMF))
        self._fp.write(_make_dr([("0001", f_0001), ("VRID", f_vrid), ("SG2D", f_sg2d)]))
        return rcid

    def _write_vrid_line(self, coords):
        """
        Write chain-node line geometry:
          1. VC at start  (Connected Node)
          2. VC at end    (Connected Node)
          3. VE (Edge) with VRPC+VRPT linking to VC nodes
             + SG2D for any intermediate (non-endpoint) vertices
        Returns VE RCID.
        """
        vc_start = self._write_vc_node(coords[0][0],  coords[0][1])
        vc_end   = self._write_vc_node(coords[-1][0], coords[-1][1])

        ve_rcid = self._vrcid; self._vrcid += 1
        f_0001  = _u16(RCNM_VE)
        f_vrid  = _u8(RCNM_VE) + _u32(ve_rcid) + _u16(1) + _u8(1)

        # VRPC — control field: insert 2 node pointers starting at index 1
        f_vrpc = _u8(1) + _u16(1) + _u16(2)   # VPUI=1, VPIX=1, NVPT=2

        # VRPT — two VC node pointers (binary only, no UT separators)
        # Each entry: NAME(5) + ORNT(1) + USAG(1) + TOPI(1) + MASK(1) = 9 bytes
        name_s = bytes([120]) + _u32(vc_start)   # begin node
        name_e = bytes([120]) + _u32(vc_end)     # end node
        f_vrpt = (name_s + _u8(255) + _u8(255) + _u8(1) + _u8(255) +
                  name_e + _u8(255) + _u8(255) + _u8(2) + _u8(255))

        fields = [("0001", f_0001), ("VRID", f_vrid), ("VRPC", f_vrpc), ("VRPT", f_vrpt)]

        # SG2D — intermediate vertices only (all coords between first and last)
        intermediate = coords[1:-1]
        if intermediate:
            f_sg2d = b"".join(_i32(round(lat * COMF)) + _i32(round(lon * COMF))
                              for lon, lat in intermediate)
            fields.append(("SG2D", f_sg2d))

        self._fp.write(_make_dr(fields))
        return ve_rcid

    def _write_vrid_area(self, coords):
        """Write a VRID (Face) + SG2D record for an area ring. Returns RCID."""
        rcid = self._vrcid; self._vrcid += 1
        f_0001 = _u16(RCNM_VE)  # Chain-node: areas use closed Edge records
        f_vrid = _u8(RCNM_VE) + _u32(rcid) + _u16(1) + _u8(1)
        f_sg2d = b"".join(_i32(round(lat*COMF)) + _i32(round(lon*COMF)) for lon,lat in coords)
        self._fp.write(_make_dr([("0001",f_0001),("VRID",f_vrid),("SG2D",f_sg2d)]))
        return rcid

    # ── Feature records ───────────────────────────────────────────────────────

    def _write_frid(self, objl_code, prim, vrcid, rcnm_v, attf_pairs):
        """Write a FRID + FOID + ATTF + FSPT record."""
        rcid = self._frcid; self._frcid += 1
        fidn = self._fidn;  self._fidn  += 1
        objl = OBJL.get(objl_code, 0)

        f_0001 = _u16(RCNM_FE)
        f_frid = (
            _u8(RCNM_FE) + _u32(rcid) +
            _u8(prim) + _u8(1) +         # PRIM, GRUP=1
            _u16(objl) +                 # OBJL
            _u16(1) + _u8(1)             # RVER=1, RUIN=1
        )
        f_foid = _u16(0) + _u32(fidn) + _u16(1)   # AGEN=0, FIDN, FIDS=1

        # ATTF: repeating (b12 ATTL, A ATVL) — each pair ends with UT (0x1f),
        # the very last pair ends with FT (0x1e) which _make_dr appends.
        f_attf = b""
        if attf_pairs:
            for i,(attl,atvl) in enumerate(attf_pairs):
                sep = UT if i < len(attf_pairs)-1 else b""
                f_attf += _u16(attl) + atvl.encode("latin-1") + sep
        else:
            f_attf = None   # omit ATTF if no attributes

        # FSPT: NAME(5 bytes = [RCNM(1)][RCID(4)LE]) + ORNT + USAG + MASK
        name = bytes([rcnm_v]) + _u32(vrcid)
        ornt = 1 if prim in (PRIM_LINE, PRIM_AREA) else 255
        usag = 1 if prim == PRIM_AREA else 255
        f_fspt = name + _u8(ornt) + _u8(usag) + _u8(255)  # MASK=255(null)

        fields = [("0001",f_0001),("FRID",f_frid),("FOID",f_foid)]
        if f_attf is not None:
            fields.append(("ATTF",f_attf))
        fields.append(("FSPT",f_fspt))
        self._fp.write(_make_dr(fields))

    # ── Public write methods ──────────────────────────────────────────────────

    def add_point(self, code, lon, lat, tags):
        vrcid = self._write_vrid_point(lon, lat)
        attf  = build_attf(tags, code)
        self._write_frid(code, PRIM_POINT, vrcid, RCNM_VI, attf)

    def add_line(self, code, coords, tags):
        if len(coords) < 2: return
        vrcid = self._write_vrid_line(coords)
        attf  = build_attf(tags, code)
        self._write_frid(code, PRIM_LINE, vrcid, RCNM_VE, attf)

    def add_area(self, code, coords, tags):
        if len(coords) < 4: return
        vrcid = self._write_vrid_area(coords)
        attf  = build_attf(tags, code)
        self._write_frid(code, PRIM_AREA, vrcid, RCNM_VE, attf)

    def close(self):
        self._fp.close()

# ── Top-level pipeline ────────────────────────────────────────────────────────

def write_enc(handler, output_path, dataset_name="AU_NAUTICAL"):
    w = S57Writer(output_path, dataset_name=dataset_name)
    for code,lon,lat,tags in handler.points:
        w.add_point(code,lon,lat,tags)
    for code,coords,tags in handler.lines:
        w.add_line(code,coords,tags)
    for code,coords,tags in handler.areas:
        w.add_area(code,coords,tags)
    w.close()
    total = len(handler.points)+len(handler.lines)+len(handler.areas)
    print(f"  Point features  : {len(handler.points):,}")
    print(f"  Line features   : {len(handler.lines):,}")
    print(f"  Area features   : {len(handler.areas):,}")
    print(f"  Total           : {total:,}")


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.osm.pbf> <output.000>")
        sys.exit(1)
    input_pbf, output_enc = sys.argv[1], sys.argv[2]
    if not os.path.exists(input_pbf):
        sys.exit(f"ERROR: Input file not found: {input_pbf}")
    print(f"Parsing: {input_pbf}")
    handler = NauticalHandler()
    handler.apply_file(input_pbf, locations=True)
    print(f"  Nodes : {len(handler.points):,}")
    print(f"  Ways  : {len(handler.lines)+len(handler.areas):,}  ({len(handler.lines):,} lines, {len(handler.areas):,} areas)")
    print(f"\nWriting S-57 ENC: {output_enc}")
    write_enc(handler, output_enc)
    size = os.path.getsize(output_enc)
    print(f"\nDone — {output_enc}  ({size:,} bytes)")

if __name__ == "__main__":
    main()
