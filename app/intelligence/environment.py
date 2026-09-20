"""Plane? v3.4 deterministic atmosphere, contrail and celestial helpers."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from .trajectory import ProjectedPoint, bearing_deg

STANDARD_LEVELS_HPA = (1000, 925, 850, 700, 500, 400, 300, 250, 200, 150, 100)

@dataclass(slots=True)
class PressureLayer:
    pressure_hpa: float
    altitude_m: float | None = None
    temperature_c: float | None = None
    relative_humidity_pct: float | None = None
    wind_speed_kmh: float | None = None
    wind_direction_deg: float | None = None
    geopotential_height_m: float | None = None

@dataclass(slots=True)
class FlightLevelAtmosphere:
    pressure_hpa: float
    temperature_c: float | None
    relative_humidity_pct: float | None
    ice_relative_humidity_pct: float | None
    wind_speed_kmh: float | None
    wind_direction_deg: float | None
    confidence: str
    source_levels: tuple[float, float] | None = None

@dataclass(slots=True)
class ContrailEstimate:
    formation: str
    persistence: str
    confidence: str
    formation_score: int | None
    persistence_score: int | None
    reasons: list[str] = field(default_factory=list)

@dataclass(slots=True)
class AtmosphereScore:
    heat_haze: str
    heat_haze_score: int
    clarity_score: int
    clarity_label: str
    reasons: list[str] = field(default_factory=list)

@dataclass(slots=True)
class CrossingEstimate:
    candidate: bool
    min_separation_deg: float
    time_to_min_s: float | None
    safety_warning: str | None = None


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def altitude_to_pressure_hpa(altitude_m: float) -> float:
    h=max(-500.0,min(20000.0,float(altitude_m)))
    if h<=11000:return 1013.25*(1.0-2.25577e-5*h)**5.25588
    return 226.32*math.exp(-(h-11000.0)/6341.62)


def pressure_to_altitude_m(pressure_hpa: float) -> float:
    p=max(1.0,float(pressure_hpa))
    if p>=226.32:return (1.0-(p/1013.25)**(1.0/5.25588))/2.25577e-5
    return 11000.0-6341.62*math.log(p/226.32)


def _sat_water_hpa(t_c: float)->float:return 6.112*math.exp((17.62*t_c)/(243.12+t_c))
def _sat_ice_hpa(t_c: float)->float:return 6.112*math.exp((22.46*t_c)/(272.62+t_c))

def relative_humidity_over_ice(temp_c: float|None,rh_water_pct: float|None)->float|None:
    if temp_c is None or rh_water_pct is None:return None
    if temp_c>=0:return float(rh_water_pct)
    e=clamp(float(rh_water_pct),0.0,200.0)/100.0*_sat_water_hpa(temp_c)
    return 100.0*e/max(_sat_ice_hpa(temp_c),1e-6)


def _interp(a: float|None,b: float|None,f: float)->float|None:
    if a is None and b is None:return None
    if a is None:return b
    if b is None:return a
    return a+(b-a)*f


def interpolate_flight_level(layers: Iterable[PressureLayer],altitude_m: float)->FlightLevelAtmosphere:
    usable=list(layers)
    if not usable:return FlightLevelAtmosphere(altitude_to_pressure_hpa(altitude_m),None,None,None,None,None,"Low",None)
    for layer in usable:
        if layer.altitude_m is None:layer.altitude_m=layer.geopotential_height_m if layer.geopotential_height_m is not None else pressure_to_altitude_m(layer.pressure_hpa)
    usable.sort(key=lambda l:float(l.altitude_m or 0.0)); lower=usable[0]; upper=usable[-1]
    for a,b in zip(usable,usable[1:]):
        if float(a.altitude_m or 0.0)<=altitude_m<=float(b.altitude_m or 0.0):lower,upper=a,b;break
    z1,z2=float(lower.altitude_m or 0.0),float(upper.altitude_m or 0.0); f=0.0 if abs(z2-z1)<1 else clamp((altitude_m-z1)/(z2-z1),0.0,1.0)
    lp,up=math.log(max(lower.pressure_hpa,1.0)),math.log(max(upper.pressure_hpa,1.0)); pressure=math.exp(lp+(up-lp)*f)
    temp=_interp(lower.temperature_c,upper.temperature_c,f); rh=_interp(lower.relative_humidity_pct,upper.relative_humidity_pct,f); ws=_interp(lower.wind_speed_kmh,upper.wind_speed_kmh,f); wd=None
    if lower.wind_direction_deg is not None or upper.wind_direction_deg is not None:
        a=float(lower.wind_direction_deg if lower.wind_direction_deg is not None else upper.wind_direction_deg); b=float(upper.wind_direction_deg if upper.wind_direction_deg is not None else lower.wind_direction_deg); wd=(a+((b-a+180)%360-180)*f)%360
    rhi=relative_humidity_over_ice(temp,rh); fields=sum(x is not None for x in (temp,rh,ws)); conf="High" if fields>=3 and lower is not upper else "Medium" if fields>=2 else "Low"
    if not z1 <= altitude_m <= z2 or any(value is None for value in (lower.temperature_c, upper.temperature_c, lower.relative_humidity_pct, upper.relative_humidity_pct)):
        conf = "Low"
    return FlightLevelAtmosphere(pressure,temp,rh,rhi,ws,wd,conf,(lower.pressure_hpa,upper.pressure_hpa))


def estimate_contrail(atm: FlightLevelAtmosphere,aircraft_type: str="")->ContrailEstimate:
    """Conservative Schmidt-Appleman-inspired estimate plus ice-supersaturation persistence."""
    t,rhi,rh=atm.temperature_c,atm.ice_relative_humidity_pct,atm.relative_humidity_pct; reasons=[]
    if t is None or rh is None:return ContrailEstimate("Unknown","Unknown","Uncertain",None,None,["Flight-level temperature and humidity are required; atmospheric data unavailable"])
    cold=clamp((-35.0-t)/20.0,0.0,1.0); moisture=0.45 if rh is None else clamp((rh-20.0)/65.0,0.0,1.0); formation_score=int(round(100*(0.72*cold+0.28*moisture)))
    reasons.append(f"very cold flight-level air ({t:.0f}°C)" if t<=-50 else f"cold flight-level air ({t:.0f}°C)" if t<=-40 else f"marginal flight-level temperature ({t:.0f}°C)")
    if rhi is not None:
        if rhi>=100:reasons.append(f"ice-relative humidity near/above saturation (~{rhi:.0f}%)")
        elif rhi<75:reasons.append(f"flight-level air appears dry with respect to ice (~{rhi:.0f}%)")
    formation="Very Likely" if formation_score>=82 else "Likely" if formation_score>=66 else "Possible" if formation_score>=42 else "Unlikely" if formation_score>=22 else "Very Unlikely"
    if rhi is None:
        persistence_score=35 if t<=-40 else 20; persistence="Moderate persistence" if persistence_score>=35 else "Short-lived"; conf="Low"; reasons.append("upper-air ice humidity unavailable; persistence confidence reduced")
    else:
        persistence_score=int(round(clamp((rhi-70.0)/45.0,0.0,1.0)*100)); persistence="Persistent/spreading likely" if persistence_score>=85 else "Persistent" if persistence_score>=68 else "Moderate persistence" if persistence_score>=45 else "Short-lived" if persistence_score>=20 else "Immediate dissipation likely"; conf="High" if atm.confidence=="High" else "Medium" if atm.confidence=="Medium" else "Low"
    return ContrailEstimate(formation,persistence,conf,formation_score,persistence_score,reasons)


def estimate_atmosphere(*,temperature_c: float|None,surface_temperature_c: float|None,humidity_pct: float|None,visibility_m: float|None,wind_kmh: float|None,solar_wm2: float|None,sun_elevation_deg: float|None,distance_km: float,pm25: float|None=None,aerosol_optical_depth: float|None=None,precipitation_mm: float|None=None)->AtmosphereScore:
    haze=5.0; reasons=[]
    if temperature_c is not None:haze+=18*clamp((temperature_c-18)/18,0,1)
    if surface_temperature_c is not None and temperature_c is not None:
        delta=surface_temperature_c-temperature_c; haze+=30*clamp(delta/12,0,1)
        if delta>=4:reasons.append("heated surface is substantially warmer than the air")
    if solar_wm2 is not None:haze+=22*clamp(solar_wm2/900,0,1)
    elif sun_elevation_deg is not None and sun_elevation_deg>25:haze+=12
    if wind_kmh is not None:haze+=12*(1-clamp(wind_kmh/25,0,1))
    haze+=12*clamp((distance_km-3)/20,0,1)
    if humidity_pct is not None and humidity_pct>80:haze+=5
    haze_i=int(round(clamp(haze,0,100))); heat="Severe" if haze_i>=78 else "High" if haze_i>=62 else "Moderate" if haze_i>=42 else "Low" if haze_i>=22 else "Very Low"
    clarity=100.0
    if visibility_m is not None:clarity-=42*(1-clamp(visibility_m/30000,0,1))
    else:clarity-=12
    if humidity_pct is not None:clarity-=15*clamp((humidity_pct-55)/40,0,1)
    if pm25 is not None:clarity-=18*clamp(pm25/55,0,1)
    if aerosol_optical_depth is not None:clarity-=15*clamp(aerosol_optical_depth/0.8,0,1)
    if precipitation_mm is not None and precipitation_mm>0:clarity-=18*clamp(precipitation_mm/4,0,1)
    if wind_kmh is not None and wind_kmh>8:clarity+=4*clamp((wind_kmh-8)/20,0,1)
    clarity_i=int(round(clamp(clarity,0,100))); label="Excellent" if clarity_i>=85 else "Good" if clarity_i>=70 else "Fair" if clarity_i>=52 else "Poor" if clarity_i>=32 else "Very Poor"
    if all(value is None for value in (temperature_c, humidity_pct, visibility_m, wind_kmh, pm25, aerosol_optical_depth, precipitation_mm)):
        heat, label = "Unknown", "Unknown"
        reasons.append("Weather and air-quality data unavailable; conditions cannot be assessed.")
    return AtmosphereScore(heat,haze_i,clarity_i,label,reasons)


def angular_separation_deg(az1: float,el1: float,az2: float,el2: float)->float:
    a1,a2,e1,e2=map(math.radians,(az1,az2,el1,el2)); c=math.sin(e1)*math.sin(e2)+math.cos(e1)*math.cos(e2)*math.cos(a1-a2); return math.degrees(math.acos(clamp(c,-1,1)))

def aircraft_az_el(observer_lat: float,observer_lon: float,point: ProjectedPoint,observer_alt_m: float=0.0)->tuple[float,float]:
    az=bearing_deg(observer_lat,observer_lon,point.latitude,point.longitude); elev=math.degrees(math.atan2(max(-1000.0,(point.altitude_m or 0.0)-observer_alt_m)/1000.0,max(point.horizontal_km,1e-4))); return az,elev

def detect_crossing(path: Iterable[ProjectedPoint],observer_lat: float,observer_lon: float,celestial_az_deg: float,celestial_el_deg: float,*,threshold_deg: float,solar: bool=False,observer_alt_m: float=0.0)->CrossingEstimate:
    best=999.0; best_t=None
    for p in path:
        az,el=aircraft_az_el(observer_lat,observer_lon,p,observer_alt_m); sep=angular_separation_deg(az,el,celestial_az_deg,celestial_el_deg)
        if sep<best:best,best_t=sep,p.seconds
    warning=None
    if solar and best<=threshold_deg:warning="Solar crossing safety: never look at or aim magnifying optics/camera optical viewfinders at the Sun without a proper certified front-mounted solar filter and safe solar-observation technique."
    return CrossingEstimate(best<=threshold_deg,round(best,2),best_t,warning)
