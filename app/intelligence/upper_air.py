"""Cached Open-Meteo pressure-level profile retrieval for v3.4."""
from __future__ import annotations
import asyncio, logging, time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import httpx
from app.config import settings
from .environment import PressureLayer, STANDARD_LEVELS_HPA, pressure_to_altitude_m
logger=logging.getLogger(__name__)
@dataclass
class _Entry:
    expires_at: float
    layers: list[PressureLayer]
    error: str|None=None
_CACHE: dict[tuple[float,float],_Entry]={}; _LOCK=asyncio.Lock(); _INFLIGHT: dict[tuple[float,float],asyncio.Task]={}
def _num(v:Any)->float|None:
    try:return float(v) if v is not None else None
    except (TypeError,ValueError):return None
def _nearest_time_index(times:list[str])->int:
    if not times:return 0
    now=datetime.now(timezone.utc); best=(10**30,0)
    for i,s in enumerate(times):
        try:
            d=datetime.fromisoformat(str(s).replace('Z','+00:00')); d=d if d.tzinfo else d.replace(tzinfo=timezone.utc); diff=abs((d-now).total_seconds())
            if diff<best[0]:best=(diff,i)
        except Exception:pass
    return best[1]
def _at(hourly:dict[str,Any],key:str,idx:int)->float|None:
    values=hourly.get(key) or []; return _num(values[idx]) if idx<len(values) else None
async def _fetch(latitude:float,longitude:float)->_Entry:
    variables=[]
    for p in STANDARD_LEVELS_HPA: variables += [f'temperature_{p}hPa',f'relative_humidity_{p}hPa',f'wind_speed_{p}hPa',f'wind_direction_{p}hPa',f'geopotential_height_{p}hPa']
    params={'latitude':latitude,'longitude':longitude,'timezone':'UTC','forecast_hours':6,'past_hours':1,'hourly':','.join(variables)}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(min(12.0,float(settings.photography_http_timeout_seconds)))) as client: resp=await client.get(settings.open_meteo_forecast_url,params=params)
        if resp.status_code!=200:return _Entry(time.monotonic()+60,[],f'Open-Meteo upper-air HTTP {resp.status_code}')
        hourly=(resp.json().get('hourly') or {}); idx=_nearest_time_index(hourly.get('time') or []); layers=[]
        for p in STANDARD_LEVELS_HPA:
            gh=_at(hourly,f'geopotential_height_{p}hPa',idx)
            layers.append(PressureLayer(float(p),gh if gh is not None else pressure_to_altitude_m(p),_at(hourly,f'temperature_{p}hPa',idx),_at(hourly,f'relative_humidity_{p}hPa',idx),_at(hourly,f'wind_speed_{p}hPa',idx),_at(hourly,f'wind_direction_{p}hPa',idx),gh))
        good=sum(1 for x in layers if x.temperature_c is not None or x.relative_humidity_pct is not None)
        return _Entry(time.monotonic()+(1800 if good else 120),layers,None if good else 'Upper-air model returned no usable pressure-level values')
    except Exception as exc:
        logger.warning('upper_air_fetch_failed error=%s',type(exc).__name__); return _Entry(time.monotonic()+60,[],f'upper-air unavailable: {type(exc).__name__}')
async def get_upper_air_profile(latitude:float,longitude:float,altitude_m:float|None=None)->tuple[list[PressureLayer],str|None]:
    # Profile is a vertical column; nearby aircraft share the same spatial cache regardless of flight level.
    key=(round(latitude*4)/4,round(longitude*4)/4); now=time.monotonic()
    async with _LOCK:
        cached=_CACHE.get(key)
        if cached and cached.expires_at>now:return list(cached.layers),cached.error
        task=_INFLIGHT.get(key)
        if task is None:
            if len(_INFLIGHT) >= 8:
                return [], "Upper-air refresh busy"
            task=asyncio.create_task(_fetch(latitude,longitude)); _INFLIGHT[key]=task
            def completed(done):
                _INFLIGHT.pop(key, None)
                if not done.cancelled() and done.exception() is None:
                    _CACHE[key] = done.result()
                    while len(_CACHE) > 256:
                        _CACHE.pop(next(iter(_CACHE)))
            task.add_done_callback(completed)
    # A timed-out caller must not cancel the shared refresh for other users.
    entry=await asyncio.shield(task)
    async with _LOCK:
        _CACHE[key]=entry
        if _INFLIGHT.get(key) is task:
            _INFLIGHT.pop(key,None)
        while len(_CACHE) > 256:
            _CACHE.pop(next(iter(_CACHE)))
    return list(entry.layers),entry.error
