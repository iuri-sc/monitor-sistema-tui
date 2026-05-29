
# abstração de gpu para nvidia e intel

from __future__ import annotations
from dataclasses import dataclass, field
import platform
import subprocess

@dataclass
class GpuInfo:
    name: str = "Unknown GPU"
    load_pct: float = 0.0
    mem_used_mb: float = 0.0
    mem_total_mb: float = 0.0
    temp_c: float | None = None
    vendor: str = "Unknown" # nvidia | intel | unknown
    
    @property
    def mem_pct(self) -> float:
        if self.mem_total_mb <= 0:
            return 0.0
        
        return self.mem_used_mb / self.mem_total_mb * 100.0
    
# detectores
def _try_nvidia() -> list[GpuInfo] | None:
    # tenta nvidia via gputil
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        if count == 0:
            return None
        
        result = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
                
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            try:
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = None
                
            result.append(GpuInfo(
                name=name,
                load_pct=float(util.gpu),
                mem_used_mb=mem.used / 1024 / 1024,
                mem_total_mb=mem.total / 1024 / 1024,
                temp_c=float(temp) if temp is not None else None,
                vendor="nvidia"
            ))
        
        return result or None
    except Exception:
        return None
    
def _try_intel_windows() -> list[GpuInfo] | None:
    # tenta gpu intel no windows via wmi + pdh, retorna uso estimado via 'Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine'
    if platform.system() != "Windows":
        return None
    
    try:
        import wmi
        w = wmi.WMI(namespace="root\\cimv2")
        
        # nome da gpu intel via Win32_VideoController
        controllers = w.Win32_VideoController()
        intel_gpus = [c for c in controllers if "Intel" in (c.Name or "")]
        if not intel_gpus:
            return None
        
        # uso via pdh - namespace separado
        try:
            w2 = wmi.WMI(namespace="root\\cimv2")
            perf = w2.query(
                "SELECT * FROM Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine"
            )
            # soma os engines de cada gpu intel
            total_load = 0.0
            count = 0
            for p in perf:
                if hasattr(p, "UtilizationPercentage"):
                    total_load += float(p.UtilizationPercentage or 0)
                    count += 1
                    
            load = (total_load / count) if count else 0.0
        except Exception:
            load = 0.0
            
        # vram via Win32_VideoController (DedicatedVideoMemory em bytes)
        result = []
        for c in intel_gpus:
            vram_total = int(c.AdapterRAM or 0) / 1024 / 1024 # bytes -> MB
            result.append(GpuInfo(
                name= c.Name or "Intel GPU",
                load_pct= load,
                mem_total_mb= 0.0, # wmi não espõe VRAM em tempo real
                mem_used_mb= vram_total,
                temp_c= None,
                vendor= "intel"
            ))
        return result or None
    except Exception:
        return None
    
def _try_intel_linux() -> list[GpuInfo] | None:
    # tenta gpu intel no linux via /sys/class/drm e intel_gpu_top
    if platform.system() != "Linux":
        return None
    
    try:
        import json, shutil
        if not shutil.which("intel_gpu_top"):
            # sem intel_gpu_top retorna só o nome sem métricas
            import pathlib
            drm = pathlib.Path("/sys/class/drm")
            cards = list(drm.glob("card*/device/vendor"))
            for c in cards:
                try:
                    vendor_id = c.read_text().strip()
                    if vendor_id == "0x8086": # intel vendor id
                        name_path = c.parent / "product"
                        name = name_path.read_text().strip() if name_path.exists() else "Intel GPU"
                        return [GpuInfo(name=name, vendor="Intel")]
                    
                except Exception:
                    pass
                
            return None
        
        # intel_gpu_top -J -s 1 - roda por 1 amostra e retorna json
        out = subprocess.check_output(
            ["intel_gpu_top", "-J", "-s", "200"],
            timeout=1, stderr=subprocess.DEVNULL
        ).decode()
        # a saída pode conter múltiplos objetos json; pega o último válido
        data = None
        for line in out.strip().splitlines():
            try:
                data = json.loads(line)
            except Exception:
                pass
            
        if not data:
            return None
        
        engines = data.get("engines", {})
        # soma o busy% de todas as engines
        total, n = 0.0, 0
        for eng in engines.values():
            if isinstance(eng, dict) and "busy" in eng:
                total += float(eng["busy"])
                n += 1
                
        load = total / n if n else 0.0
        
        freq = data.get("frequency", {})
        return [GpuInfo(
            name= "Intel GPU",
            load_pct= load,
            vendor= "intel"
        )]
    except Exception:
        return None
    
# api pública
_cached_vendor: str | None = None # nvidia | intel | none

def get_gpus() -> list[GpuInfo]:
    # retorna lista de gpus detectadas, tenta nvidia primeiro, depois intel, retorna lista vazia se nenhuma for suportada
    global _cached_vendor
    
    # se já sabe o vendor, vai direto
    if _cached_vendor == "nvidia":
        return _try_nvidia() or []
    if _cached_vendor == "intel":
        return (_try_intel_windows() or _try_intel_linux() or [])
    if _cached_vendor == "none":
        return []
    
    # primeira chamada - detecta
    gpus = _try_nvidia()
    if gpus:
        _cached_vendor = "nvidia"
        return gpus
    
    gpus = _try_intel_windows() or _try_intel_linux()
    if gpus:
        _cached_vendor = "intel"
        return gpus
    
    _cached_vendor = "none"
    return []

def gpu_available() -> bool:
    # retorna true se pelo menos uma gpu for detectada
    return len(get_gpus()) > 0
    
    