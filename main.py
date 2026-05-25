from __future__ import annotations
from collections import deque
from datetime import datetime
import psutil
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import (
    Footer,
    Header,
    Label,
    ProgressBar,
    Static,
    DataTable
)
from textual.timer import Timer
from rich.text import Text
from rich.console import RenderableType
from rich.style import Style
import platform
from gpu import get_gpus, gpu_available

HISTORY = 40
UPDATE_S = 1.0

COLOR_CPU = "bright_magenta"
COLOR_RAM = "bright_green"
COLOR_NET = "yellow"
COLOR_DISK = "bright_cyan"
COLOR_GPU = "bright_red"

# sparkline widget
class Sparkline(Static):
    # ascii sparkline
    BARS = "▁▂▃▄▅▆▇█"
    
    def __init__(self, color: str, history: int = HISTORY, **kwargs):
        super().__init__(**kwargs)
        self.color = color
        self._data: deque[float] = deque([0.0] * history, maxlen=history)
        
    def push(self, value: float) -> None:
        self._data.append(max(0.0, min(100.0, value)))
        self.refresh()
        
    def render(self) -> RenderableType:
        data = list(self._data)
        mx = max(max(data), 1.0)
        chars = [self.BARS[int(v / mx * (len(self.BARS) - 1))] for v in data]
        return Text("".join(chars), style=Style(color=self.color))
    
# single stat card
class StatCard(Widget):
    DEFAULT_CSS = """
        StatCard {
        height: auto;
        border: round $panel-lighten-2;
        padding: 0 1;
        margin: 0 1 1 0;
    }
    StatCard .card-header {
        height: 1;
    }
    StatCard .card-title {
        color: $text-muted;
    }
    StatCard .card-value {
        text-align: right;
        text-style: bold;
    }
    StatCard ProgressBar {
        height: 1;
        margin: 0;
        padding: 0;
    }
    StatCard Sparkline {
        height: 2;
        margin-top: 0;
    }
    """
    
    value: reactive[float] = reactive(0.0)
    
    def __init__(self, title: str, color: str, unit: str = "%", show_graph: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._title = title
        self._color = color
        self._unit = unit
        self._show_graph = show_graph
        self._label_text: str | None = None
        
    def compose(self) -> ComposeResult:
        with Horizontal(classes="card-header"):
            yield Label(f"[{self._color}]●[/] {self._title}", classes="card-title")
            yield Label("-", classes="card-value", id=f"{self.id}-val")
        
        yield ProgressBar(total=100, show_eta=False, show_percentage=False, id=f"{self.id}-bar")
        if self._show_graph:
            yield Sparkline(self._color, id=f"{self.id}-spark")
            
    def update_value(self, value: float, max_val: float = 100.0, label_text: str | None = None) -> None:
        self._label_text = label_text
        pct = min(value / max_val * 100.0, 100.0) if max_val else 0.0
        
        val_lbl = self.query_one(f"#{self.id}-val", Label)
        display = label_text if label_text else f"{value:.1f}{self._unit}"
        val_lbl.update(f"[{self._color}]{display}[/]")
        
        bar = self.query_one(f"#{self.id}-bar", ProgressBar)
        bar.progress = pct
        
        if self._show_graph:
            spark = self.query_one(f"#{self.id}-spark", Sparkline)
            spark.push(pct)
            
# disk row
class DiskRow(Widget):
    DEFAULT_CSS = """
        DiskRow {
        height: 3;
        layout: horizontal;
    }
    DiskRow .disk-label {
        width: 18;
        color: $text-muted;
    }
    DiskRow .disk-bar {
        width: 1fr;
    }
    DiskRow .disk-pct {
        width: 6;
        text-align: right;
        color: $text;
    }
    """
    
    def __init__(self, mountpoint: str, **kwargs):
        super().__init__(**kwargs)
        self.mountpoint = mountpoint
        mp = mountpoint if len(mountpoint) <= 16 else mountpoint[:14] + "…"
        self._mp_display = mp
        
    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Label(self._mp_display, classes="disk-label")
            yield ProgressBar(total=100, show_eta=False, show_percentage=False, classes="disk-bar", id=f"disk-bar-{self.id}")
            yield Label("", classes="disk-pct", id=f"disk-pct-{self.id}")
        
        yield Label("", classes="disk-speed", id=f"disk-speed-{self.id}")
    
    def update(self, used: int, total: int, read_mb: float = 0.0, write_mb: float = 0.0) -> None:
        pct = used / total * 100 if total else 0
        self.query_one(f"#disk-bar-{self.id}", ProgressBar).progress = pct
        self.query_one(f"#disk-pct-{self.id}", Label).update(
            f"[bright_cyan]{pct:.0f}%[/]"
        )
        self.query_one(f"#disk-speed-{self.id}", Label).update(
            f"[dim]▼ {read_mb:.1f} MB/s  ▲ {write_mb:.1f} MB/s[/]"
        )
        
# core row
class CoreRow(Widget):
    DEFAULT_CSS = """
        CoreRow {
        height: 1;
        layout: horizontal;
    }
    CoreRow .core-id {
        width: 4;
        color: $text-disabled;
    }
    CoreRow .core-bar {
        width: 1fr;
    }
    CoreRow .core-pct {
        width: 5;
        text-align: right;
        color: $text-muted;
    }
    """

    def __init__(self, idx: int, **kwargs):
        super().__init__(**kwargs)
        self._idx = idx
        
    def compose(self) -> ComposeResult:
        yield Label(f"C{self._idx}", classes="core-id")
        yield ProgressBar(total=100, show_eta=False, show_percentage=False, classes="core-bar", id=f"core-bar-{self._idx}")
        yield Label("0%", classes="core-pct", id=f"core-pct-{self._idx}")
        
    def update(self, pct: float) -> None:
        self.query_one(f"#core-bar-{self._idx}", ProgressBar).progress = pct
        self.query_one(f"#core-pct-{self._idx}", Label).update(f"{pct:.0f}%")
        
# main app
class SystemMonitor(App):
    CSS = """
        Screen {
        background: $background;
    }
 
    #top-grid {
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        height: auto;
        margin: 0 1;
    }
 
    #net-row {
        height: auto;
        margin: 0 1 1 1;
    }
 
    .section-card {
        border: round $panel-lighten-2;
        padding: 0 1 1 1;
        margin: 0 1 1 1;
        height: auto;
    }
 
    .section-title {
        color: $text-muted;
        margin-bottom: 1;
    }
 
    #net-detail {
        color: $text-muted;
        margin: 0 2 1 2;
    }
 
    #processes-table {
        margin: 0 1 1 1;
        height: 12;
    }
 
    DataTable {
        height: 10;
    }
 
    #cores-grid {
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        height: auto;
    }
 
    ProgressBar > .bar--bar {
        color: $accent;
    }
    """
    
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh")]
    
    def __init__(self):
        super().__init__()
        self._net_prev = psutil.net_io_counters()
        self._disk_rows: dict[str, DiskRow] = {}
        self._core_rows: dict[int, CoreRow] = {}
        self._timer: Timer | None = None
        self.disk_io_prev = psutil.disk_io_counters()
        
    # layout
    def compose(self) -> ComposeResult:
        uname = platform.uname()
        os_text = f"{uname.system} {uname.release} · {uname.node}"
        
        yield Header(show_clock=True)
        
        with ScrollableContainer():
            yield Label(f"[dim]{os_text}[/]", id="os-info")
            yield Label("", id="os-info-spacer")
            
            # cpu + ram
            with Horizontal(id="top-grid"):
                yield StatCard("CPU", COLOR_CPU, id="cpu-card")
                yield StatCard("Memory", COLOR_RAM, id="ram-card")
                
            # network
            with Container(id="net-row"):
                yield StatCard("Network", COLOR_NET, unit="KB/s", id="net-card")
                yield Label("↓ 0 KB/s   ↑ 0 KB/s", id="net-detail")
                
            # gpu (mostra se disponível)
            if gpu_available():
                with Container(id="net-row"):
                    yield StatCard("GPU", COLOR_GPU, id="gpu-card")
                    yield Label("", id="gpu-detail")
                    
            # disk
            with Container(classes="section-card", id="disk-section"):
                yield Label(f"[{COLOR_DISK}]● Disk[/]", classes="section-title")
                yield Container(id="disk-container")

            # top processes
            with Container(classes="section-card"):
                yield Label("[bright_white]● Top Processes[/]", classes="section-title")
                table = DataTable(id="proc-table", cursor_type="none")
                table.add_columns("Process", "PID", "CPU %", "RAM %", "RAM MB")
                yield table
                
            # cpu cores
            with Container(classes="section-card"):
                yield Label(f"[{COLOR_CPU}]● CPU Cores[/]", classes="section-title")
                with Container(id="cores-grid"):
                    for i in range(psutil.cpu_count(logical=True)):
                        row = CoreRow(i, id=f"core-row-{i}")
                        self._core_rows[i] = row
                        yield row
                        
        yield Footer()
        
    def on_mount(self) -> None:
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(percpu=True)
        self._timer = self.set_interval(UPDATE_S, self._tick)
        
    # update tick
    def _tick(self) -> None:
        self._update_cpu()
        self._update_ram()
        self._update_net()
        self._update_gpu()
        self._update_disk()
        self._update_processes()
        self._update_cores()
        
    def _update_cpu(self) -> None:
        cpu = psutil.cpu_percent(interval=None)
        self.query_one("#cpu-card", StatCard).update_value(cpu)
        
    def _update_ram(self) -> None:
        mem = psutil.virtual_memory()
        used = mem.used / 1e9
        total = mem.total / 1e9
        self.query_one("#ram-card", StatCard).update_value(
            mem.percent, label_text=f"{used:.1f}/{total:.1f} GB"
        )
        
    def _update_net(self) -> None:
        net = psutil.net_io_counters()
        dl = (net.bytes_recv - self._net_prev.bytes_recv) / 1024
        ul = (net.bytes_sent - self._net_prev.bytes_sent) / 1024
        self._net_prev = net
        total = dl + ul
        self.query_one("#net-card", StatCard).update_value(
            total, max_val=max(total, 1000), label_text=f"{total:.0f} KB/s"
        )
        self.query_one("#net-detail", Label).update(
            f"[dim]↓ {dl:.0f} KB/s   ↑ {ul:.0f} KB/s[/]"
        )
        
    def _update_gpu(self) -> None:
        try:
            gpus = get_gpus()
            if not gpus:
                return
            
            gpu = gpus[0]
            
            self.query_one("#gpu-card", StatCard).update_value(
                gpu.load_pct,
                label_text=f"{gpu.load_pct:.0f}%"
            )
            
            # monta linha de detalhe conforme o que está disponível
            parts = [f"[dim]{gpu.name}"]
            if gpu.mem_total_mb > 0:
                parts.append(f"VRAM {gpu.mem_used_mb:.0f}/{gpu.mem_total_mb:.0f} MB")
                
            if gpu.temp_c is not None:
                parts.append(f"Temp {gpu.temp_c:.0f}°C")
            
            vendor_tag = {"nvidia": "NVIDIA", "intel": "Intel"}.get(gpu.vendor, "")
            if vendor_tag:
                parts.append(vendor_tag)
                
            self.query_one("#gpu-detail", Label).update(" · ".join(parts) + "[/]")
        except Exception:
            pass
        
    def _update_disk(self) -> None:
        container = self.query_one("#disk-container")
        
        io = psutil.disk_io_counters()
        read_mb = (io.read_bytes - self.disk_io_prev.read_bytes) / 1e6
        write_mb = (io.write_bytes - self.disk_io_prev.write_bytes) / 1e6
        self._disk_io_prev = io
        
        for p in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(p.mountpoint)
            except (PermissionError, OSError):
                continue
            
            mp = p.mountpoint
            if mp not in self._disk_rows:
                safe_id = mp.replace('/', '-').replace('\\', '-').replace(':', '').strip('-')
                row = DiskRow(mp, id=f"disk-{safe_id}")
                container.mount(row)
                self._disk_rows[mp] = row
            
            self._disk_rows[mp].update(usage.used, usage.total)
            
    def _update_processes(self) -> None:
        procs = []
        cpu_count = psutil.cpu_count() or 1
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "memory_info"]):
            try:
                info = p.info
                if info["cpu_percent"] is None:
                    continue
                
                if info["name"] in ("System Idle Process", "Idle"):
                    continue
                
                # normaliza para 100%
                info["cpu_percent"] = info["cpu_percent"] / cpu_count
                procs.append(info)
                
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            
        top5 = sorted(procs, key=lambda x: (x["cpu_percent"] or 0), reverse=True)[:5]
        
        table = self.query_one("#proc-table", DataTable)
        table.clear()
        for p in top5:
            name = (p["name"] or "?")[:22]
            pid = str(p["pid"])
            cpu = f"{p['cpu_percent']:.1f}"
            ram_pct = f"{p['memory_percent']:.1f}"
            ram_mb = f"{p['memory_info'].rss / 1e6:.0f}" if p.get("memory_info") else "-"
            table.add_row(name, pid, cpu, ram_pct, ram_mb)
            
    def _update_cores(self) -> None:
        per_cpu = psutil.cpu_percent(percpu=True)
        for i, pct in enumerate(per_cpu):
            if i in self._core_rows:
                self._core_rows[i].update(pct)
                
    def action_refresh(self) -> None:
        self._tick()
        
if __name__ == "__main__":
    SystemMonitor().run()