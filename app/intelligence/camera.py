"""Plane? v3.4 deterministic camera/framing engine."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .trajectory import ProjectedPoint, TrajectoryPrediction, bearing_deg

AIRCRAFT_DIMENSIONS: dict[str, tuple[float, float, float]] = {
    "A388": (79.75, 72.72, 24.1), "A359": (64.75, 66.80, 17.1), "A35K": (64.75, 73.79, 17.1),
    "A333": (60.30, 63.69, 16.8), "A332": (60.30, 58.82, 17.4), "A21N": (35.80, 44.51, 11.8),
    "A321": (35.80, 44.51, 11.8), "A20N": (35.80, 37.57, 11.8), "A320": (35.80, 37.57, 11.8),
    "A319": (35.80, 33.84, 11.8), "B77W": (64.80, 73.86, 18.5), "B772": (60.90, 63.73, 18.5),
    "B789": (60.12, 62.81, 17.0), "B788": (60.12, 56.72, 17.0), "B78X": (60.12, 68.28, 17.0),
    "B748": (68.40, 76.25, 19.4), "B744": (64.44, 70.67, 19.4), "B38M": (35.92, 39.52, 12.3),
    "B39M": (35.92, 42.16, 12.3), "B738": (35.79, 39.47, 12.5), "B737": (35.79, 33.63, 12.5),
    "E190": (28.72, 36.24, 10.6), "E195": (28.72, 38.65, 10.6), "BCS3": (35.10, 38.70, 11.5),
    "C17": (51.75, 53.04, 16.8), "C5M": (67.89, 75.31, 19.8), "F16": (9.96, 15.06, 4.9),
}

@dataclass(slots=True)
class CameraGeometry:
    sensor_width_mm: float
    sensor_height_mm: float
    megapixels: float | None
    crop_factor: float

@dataclass(slots=True)
class FrameEstimate:
    focal_mm: float
    frame_width_pct: float
    frame_height_pct: float | None
    clipping_risk: str

@dataclass(slots=True)
class CameraRecommendation:
    mode: str
    shutter_speed: str
    shutter_floor: str
    aperture: str
    iso: str
    exposure_compensation: str
    focal_length: str
    focal_range_mm: tuple[int, int]
    frame_fill_pct: float | None
    clipping_risk: str
    autofocus: str
    burst: str
    stabilization: str
    angular_speed_deg_s: float | None
    best_window_start_s: float | None
    best_window_end_s: float | None
    dynamic_focal: list[tuple[int, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def geometry_from_profile(camera: Any) -> CameraGeometry:
    sw, sh = getattr(camera, "sensor_width_mm", None), getattr(camera, "sensor_height_mm", None)
    mp, crop = getattr(camera, "sensor_megapixels", None), getattr(camera, "crop_factor", None)
    if sw and sh:
        return CameraGeometry(float(sw), float(sh), float(mp) if mp else None, float(crop or (36.0 / float(sw))))
    fmt = str(getattr(camera, "sensor_format", "") or "").lower()
    model = (str(getattr(camera, "model", "") or "") + " " + str(getattr(camera, "raw_input", "") or "")).lower()
    if "r7" in model: return CameraGeometry(22.3, 14.9, float(mp or 32.5), 1.6)
    if "micro" in fmt or "mft" in fmt or "four thirds" in fmt: return CameraGeometry(17.3, 13.0, float(mp) if mp else None, 2.0)
    if "aps-c" in fmt or "apsc" in fmt:
        return CameraGeometry(22.3,14.9,float(mp) if mp else None,1.6) if "canon" in model else CameraGeometry(23.5,15.6,float(mp) if mp else None,1.5)
    if "1 inch" in fmt or "1-inch" in fmt: return CameraGeometry(13.2,8.8,float(mp) if mp else None,2.73)
    return CameraGeometry(36.0,24.0,float(mp) if mp else None,1.0)


def aircraft_dimensions(aircraft_type: str) -> tuple[float, float, float] | None:
    code=(aircraft_type or "").upper().strip()
    if not code: return None
    if code in AIRCRAFT_DIMENSIONS: return AIRCRAFT_DIMENSIONS[code]
    for key,dims in AIRCRAFT_DIMENSIONS.items():
        if code.startswith(key[:3]): return dims
    return None


def frame_occupancy(sensor_width_mm: float, sensor_height_mm: float, focal_mm: float, distance_km: float, wingspan_m: float, length_m: float | None = None) -> FrameEstimate:
    if min(sensor_width_mm,sensor_height_mm,focal_mm,distance_km,wingspan_m)<=0: raise ValueError("positive geometry values required")
    distance_m=distance_km*1000.0
    width_pct=100.0*(focal_mm*wingspan_m/distance_m)/sensor_width_mm
    height_pct=100.0*(focal_mm*length_m/distance_m)/sensor_height_mm if length_m else None
    extent = max(width_pct, height_pct or 0.0)
    risk="High" if extent>=94 else "Moderate" if extent>=84 else "Low"
    return FrameEstimate(focal_mm,max(0.0,width_pct),max(0.0,height_pct) if height_pct is not None else None,risk)


def focal_for_fill(sensor_width_mm: float, distance_km: float, wingspan_m: float, target_fill: float = 0.70) -> float:
    return target_fill*sensor_width_mm*distance_km*1000.0/wingspan_m


def angular_speed_deg_s(observer_lat: float, observer_lon: float, point: ProjectedPoint, next_point: ProjectedPoint | None) -> float | None:
    if next_point is None or next_point.seconds<=point.seconds:return None
    az1=bearing_deg(observer_lat,observer_lon,point.latitude,point.longitude); az2=bearing_deg(observer_lat,observer_lon,next_point.latitude,next_point.longitude)
    el1=math.degrees(math.atan2((point.altitude_m or 0.0)/1000.0,max(point.horizontal_km,1e-4))); el2=math.degrees(math.atan2((next_point.altitude_m or 0.0)/1000.0,max(next_point.horizontal_km,1e-4)))
    daz=(az2-az1+180.0)%360.0-180.0; dt=next_point.seconds-point.seconds
    # Great-circle separation of the two sight lines. Azimuth alone becomes
    # singular overhead and must be weighted by elevation.
    a, b, delta = map(math.radians, (el1, el2, daz))
    chord = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(delta/2)**2
    return math.degrees(2 * math.asin(math.sqrt(min(1.0, max(0.0, chord))))) / dt


def _standard_shutter(denom: float) -> int:
    choices=[160,200,250,320,400,500,640,800,1000,1250,1600,2000,2500,3200,4000,5000,6400,8000]
    return next((v for v in choices if v>=denom),choices[-1])


def _shutter_for_motion(angular_deg_s: float | None,focal_mm: float,geom: CameraGeometry,mode: str)->tuple[int,int]:
    if mode=="Prop blur":return 250,320
    if mode=="Night aircraft":return 800,1250
    if mode in {"Silhouette","Contrail shot"}:return 1000,1600
    if angular_deg_s is None:floor=1000
    else:
        # Aircraft photography normally includes active panning.  A 2-pixel
        # no-pan blur target was far too aggressive and routinely produced
        # pointless 1/3200-1/4000 suggestions.  A 5-pixel panning allowance is
        # much closer to practical handheld aviation shooting.
        h_pixels=math.sqrt((geom.megapixels or 24.0)*1_000_000*3/2); hfov=math.degrees(2*math.atan(geom.sensor_width_mm/(2*focal_mm))); deg_per_px=max(hfov/h_pixels,1e-6)
        floor=int(max(640,min(2500,_standard_shutter(angular_deg_s/(5.0*deg_per_px)))))
    return floor,_standard_shutter(floor*(1.25 if mode=="Maximum detail" else 1.0))


def _practical_standard_shutter(floor:int,preferred:int,closest:ProjectedPoint|None,mode:str)->tuple[int,int]:
    """Keep Standard aviation speeds practical for panned aircraft shots."""
    if mode!="Standard aviation": return floor,preferred
    altitude_m=abs(float(closest.altitude_m)) if closest and closest.altitude_m is not None else None
    if altitude_m is not None and altitude_m<=3000:
        # Low aircraft are large in frame; smooth panning at 1/1000 is a much
        # better default than forcing ISO up with 1/3200-1/4000.
        return min(max(floor,800),1000),1000
    if altitude_m is not None and altitude_m<=6000:
        return min(max(floor,800),1250),min(max(preferred,1000),1600)
    return min(max(floor,800),1600),min(max(preferred,1000),2000)


def _window_score(fill: float | None,point: ProjectedPoint,angular: float | None,light_quality: float,haze_penalty: float)->float:
    distance_score=1.0/(1.0+point.slant_km/8.0); fill_score=0.5 if fill is None else max(0.0,1.0-abs(fill-70.0)/55.0); tracking=1.0 if angular is None else max(0.25,1.0-angular/8.0)
    return 0.38*distance_score+0.30*fill_score+0.18*light_quality+0.14*tracking-haze_penalty


def recommend_camera(*,camera: Any,lens: Any|None,aircraft_type: str,prediction: TrajectoryPrediction,observer_lat: float,observer_lon: float,mode: str="Standard aviation",max_auto_iso: int=1600,light_relationship: str="unknown",sun_elevation_deg: float|None=None,heat_haze_level: str="Low")->CameraRecommendation:
    geom=geometry_from_profile(camera); dims=aircraft_dimensions(aircraft_type); wingspan=dims[0] if dims else None; length=dims[1] if dims else None
    lens_min=int(round(float(getattr(lens,"min_focal_mm",None) or 100))) if lens else 100; lens_max=int(round(float(getattr(lens,"max_focal_mm",None) or 600))) if lens else 600; target=0.62 if mode=="Contrail shot" else 0.70
    closest=min(prediction.path,key=lambda p:p.horizontal_km) if prediction.path else None; fill=None; clipping="Unknown"; reco_focal=min(lens_max,max(lens_min,int((lens_min+lens_max)/2)))
    if closest and wingspan:
        raw=focal_for_fill(geom.sensor_width_mm,closest.slant_km,wingspan,target); reco_focal=int(round(max(lens_min,min(lens_max,raw))/10)*10)
        est=frame_occupancy(geom.sensor_width_mm,geom.sensor_height_mm,reco_focal,closest.slant_km,wingspan,length); fill,clipping=est.frame_width_pct,est.clipping_risk
    cpa_idx=prediction.path.index(closest) if closest and prediction.path else 0; next_p=prediction.path[cpa_idx+1] if prediction.path and cpa_idx+1<len(prediction.path) else None
    angular=angular_speed_deg_s(observer_lat,observer_lon,closest,next_p) if closest else None; floor,preferred=_shutter_for_motion(angular,max(reco_focal,lens_min),geom,mode); floor,preferred=_practical_standard_shutter(floor,preferred,closest,mode)
    low_light=sun_elevation_deg is not None and sun_elevation_deg<8; aperture="f/6.3" if low_light else "f/8" if mode in {"Maximum detail","Contrail shot"} else "f/7.1"
    wide = getattr(lens, "max_aperture_wide", None) if lens else None
    tele = getattr(lens, "max_aperture_tele", None) if lens else None
    # Only endpoint specifications are known; no fabricated aperture curve.
    available = wide if reco_focal <= lens_min else (tele or wide)
    if available:
        aperture = f"f/{max(float(aperture[2:]), float(available)):g}"
    if mode=="Night aircraft":aperture=f"f/{float(available):g}" if available else "widest practical aperture"
    iso=f"Auto ISO ≤{max_auto_iso}"; ev="+0.7 EV" if "back" in light_relationship.lower() else "+0.3 EV" if low_light else "0 EV"
    if mode=="Silhouette":ev="-0.7 EV"
    haze_penalty={"Very Low":0.0,"Low":0.02,"Moderate":0.07,"High":0.14,"Severe":0.22}.get(heat_haze_level,0.05); light_quality=0.45 if "back" in light_relationship.lower() else 0.95 if "side" in light_relationship.lower() else 0.75
    scores=[]; dynamic=[]
    for i,p in enumerate(prediction.path):
        if p.seconds>(prediction.time_to_cpa_s or 900)+45:break
        f=occ=None
        if wingspan:
            f=max(lens_min,min(lens_max,focal_for_fill(geom.sensor_width_mm,p.slant_km,wingspan,target))); occ=frame_occupancy(geom.sensor_width_mm,geom.sensor_height_mm,f,p.slant_km,wingspan).frame_width_pct
            if int(p.seconds)%30<=2:dynamic.append((int(p.seconds),int(round(f/10)*10)))
        ang=angular_speed_deg_s(observer_lat,observer_lon,p,prediction.path[i+1] if i+1<len(prediction.path) else None); scores.append((_window_score(occ,p,ang,light_quality,haze_penalty),p,f,occ))
    start_s=end_s=None
    if scores:
        best=max(v[0] for v in scores); usable=[r for r in scores if r[0]>=best-0.07 and r[1].seconds<=(prediction.time_to_cpa_s or 900)+10]
        if usable:
            center=max(usable,key=lambda r:r[0])[1].seconds; around=[r for r in usable if abs(r[1].seconds-center)<=24]; start_s=min(r[1].seconds for r in around); end_s=max(r[1].seconds for r in around)
    notes=["Shutter guidance assumes smooth panning; increase speed if tracking is difficult."]
    if wingspan is None:notes.append("Exact aircraft dimensions unavailable; framing estimate is conservative.")
    if mode=="Standard aviation" and closest and closest.altitude_m is not None and abs(float(closest.altitude_m))<=3000:notes.append("Low-altitude default favors ~1/1000 with smooth panning instead of unnecessarily high shutter speeds.")
    if heat_haze_level in {"High","Severe"}:notes.append("Atmospheric shimmer may erase detail before the lens reaches its maximum focal length.")
    return CameraRecommendation(mode,f"1/{preferred}",f"1/{floor}",aperture,iso,ev,f"~{reco_focal} mm",(max(lens_min,reco_focal-70),min(lens_max,reco_focal+70)),round(fill,1) if fill is not None else None,clipping,"Continuous/Servo AF + aircraft/subject tracking","High-speed continuous burst","Lens/IBIS on for viewfinder stability; do not rely on it to freeze subject motion",round(angular,2) if angular is not None else None,start_s,end_s,dynamic[:8],notes)
