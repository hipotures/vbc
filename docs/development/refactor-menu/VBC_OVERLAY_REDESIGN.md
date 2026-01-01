# VBC Overlay Modernization Proposal

## 🎯 Podsumowanie zmian

Proponowana modernizacja trzech ekranów overlay (CONFIG, LEGEND, MENU) wprowadza:

### Nowe nazewnictwo
| Stara nazwa | Nowa nazwa | Klawisz |
|-------------|------------|---------|
| CONFIG | **SETTINGS** | `C` |
| LEGEND | **REFERENCE** | `L` |
| MENU | **SHORTCUTS** | `M` |

### Kluczowe ulepszenia

1. **Struktura kartowa** - informacje pogrupowane w logiczne karty z ikonami
2. **Układ dwukolumnowy** - lepsze wykorzystanie przestrzeni
3. **Spójna kolorystyka** - motyw GitHub Dark z akcentami
4. **Hierarchia wizualna** - nagłówki, sekcje, wyróżnienia
5. **Interaktywne wskazówki** - footer z nawigacją między panelami

---

## 📐 Struktura paneli

### ⚙ SETTINGS (dawniej CONFIG)

```
╭───────────────────────── ⚙ SETTINGS ─────────────────────────╮
│  ╭──────────────────────────────────────────────────────╮    │
│  │ Video Batch Compression - NVENC AV1 (GPU)   ● Active │    │
│  │ Started 2025-12-31 19:57:56                          │    │
│  ╰──────────────────────────────────────────────────────╯    │
│                                                              │
│  ╭─ 🎬 ENCODING ─────────╮ ╭─ ⚡ PROCESSING ─────────╮       │
│  │  Encoder    NVENC...  │ │  Threads             1  │       │
│  │  Preset     p7...     │ │  Prefetch           1x  │       │
│  │  Quality    CQ44      │ │  Queue Sort       rand  │       │
│  │  ...                  │ │  ...                    │       │
│  ╰───────────────────────╯ ╰─────────────────────────╯       │
│                                                              │
│  ╭─ 📁 INPUT/OUTPUT ─────╮ ╭─ 🎯 QUALITY & FILTERS ──╮       │
│  │  ...                  │ │  ...                    │       │
│  ╰───────────────────────╯ ╰─────────────────────────╯       │
│                                                              │
│  ╭─ 📋 METADATA & DEBUG ─────────────────────────────────╮   │
│  │  Metadata    Deep   Analysis    True   Autorotate  1  │   │
│  │  ...                                                  │   │
│  ╰───────────────────────────────────────────────────────╯   │
│                                                              │
│         Press [Esc] close • [L] Reference • [M] Shortcuts    │
╰───────────────────────── [C] to toggle ──────────────────────╯
```

**Karty:**
- **ENCODING** - encoder, preset, quality, audio, cpu fallback
- **PROCESSING** - threads, prefetch, queue sort, cpu threads
- **INPUT/OUTPUT** - folders, extensions, output format, min size
- **QUALITY & FILTERS** - dynamic CQ, camera filter, skip AV1, rotation
- **METADATA & DEBUG** - exiftool, analysis, autorotate, debug flags

---

### 📖 REFERENCE (dawniej LEGEND)

```
╭──────────────────────── 📖 REFERENCE ────────────────────────╮
│  ╭─ ◆ STATUS CODES ──────────────────────────────────────╮   │
│  │  fail    Session errors       kept    Original kept   │   │
│  │  err     Historic errors      small   Below min-size  │   │
│  │  hw_cap  Out of NVENC         av1     Already AV1     │   │
│  │  skip    Already AV1/cam      cam     Camera filtered │   │
│  │  ─────────────────────────────────────────────────────│   │
│  │     ✓ Success   ✗ Error   ≡ Kept   ⚡ Interrupted     │   │
│  ╰───────────────────────────────────────────────────────╯   │
│                                                              │
│  ╭─ ◈ JOB INDICATORS ────╮ ╭─ ◈ GPU GRAPH [G] ───────────╮   │
│  │  ● ○ ◉ ◎  Normal      │ │  Cycle: temp→fan→pwr→gpu→mem│   │
│  │  ◐ ◓ ◑ ◒  Rotation    │ │  Scales: temp 35-70°C, ...  │   │
│  ╰───────────────────────╯ │  Symbols: ▁▂▃▄▅▆▇█ · missing│   │
│                            ╰─────────────────────────────╯   │
│                                                              │
│         Press [Esc] close • [C] Settings • [M] Shortcuts     │
╰───────────────────────── [L] to toggle ──────────────────────╯
```

**Sekcje:**
- **STATUS CODES** - wszystkie kody statusu z kolorami (fail, err, hw_cap, skip, kept, small, av1, cam)
- **RESULT SYMBOLS** - symbole wyniku (✓, ✗, ≡, ⚡)
- **JOB INDICATORS** - animowane spinnery (normalny vs rotation)
- **GPU GRAPH** - metryki, skale, symbole sparkline

---

### ⌨ SHORTCUTS (dawniej MENU)

```
╭──────────────────────── ⌨ SHORTCUTS ─────────────────────────╮
│  ╭─ ▸ NAVIGATION ────────╮ ╭─ ▸ PANELS ──────────────────╮   │
│  │  [M]      This menu   │ │  [C]    Configuration       │   │
│  │  [Esc]    Close       │ │  [L]    Legend & reference  │   │
│  │  [Ctrl+C] Exit        │ │  [G]    GPU graph metric    │   │
│  ╰───────────────────────╯ ╰─────────────────────────────╯   │
│                                                              │
│  ╭─ ▸ JOB CONTROL ───────────────────────────────────────╮   │
│  │  [S]    Shutdown toggle      [R]    Refresh queue     │   │
│  │  [< ,]  Decrease threads     [> .]  Increase threads  │   │
│  ╰───────────────────────────────────────────────────────╯   │
│                                                              │
│  ╭─────────────── QUICK REFERENCE ───────────────────────╮   │
│  │   [< >] Threads     [S] Shutdown     [R] Refresh      │   │
│  ╰───────────────────────────────────────────────────────╯   │
│                                                              │
│         Press [Esc] close • [C] Settings • [L] Reference     │
╰───────────────────────── [M] to toggle ──────────────────────╯
```

**Grupy:**
- **NAVIGATION** - M, Esc, Ctrl+C
- **PANELS** - C, L, G
- **JOB CONTROL** - S, R, <, >
- **QUICK REFERENCE** - kolorowe badge'y z najważniejszymi skrótami

---

## 🎨 Paleta kolorów

```python
COLORS = {
    'accent_green': '#3fb950',    # Sukces, aktywny status
    'accent_blue': '#58a6ff',     # Nagłówki sekcji
    'accent_orange': '#f0883e',   # Status codes, ostrzeżenia
    'accent_purple': '#a371f7',   # Spinnery, akcenty
    'accent_cyan': '#79c0ff',     # GPU, panele
    'error_red': '#f85149',       # Błędy
    'warning_yellow': '#d29922',  # Ostrzeżenia
    'muted': '#8b949e',           # Tekst drugorzędny
    'dim': '#6e7681',             # Tekst przygaszony
    'border': '#30363d',          # Ramki
}
```

---

## 🔧 Integracja

### Opcja 1: Podmiana metod w dashboard.py

```python
# W klasie Dashboard, zamień:

def _generate_config_overlay(self) -> Panel:
    from vbc.ui.modern_overlays import generate_settings_overlay
    with self.state._lock:
        lines = self.state.config_lines[:]
    return generate_settings_overlay(lines, self._spinner_frame)

def _generate_legend_overlay(self) -> Panel:
    from vbc.ui.modern_overlays import generate_reference_overlay
    return generate_reference_overlay(self._spinner_frame)

def _generate_menu_overlay(self) -> Panel:
    from vbc.ui.modern_overlays import generate_shortcuts_overlay
    return generate_shortcuts_overlay()
```

### Opcja 2: Skopiuj modern_overlays.py do vbc/ui/

```bash
cp modern_overlays.py vbc/ui/modern_overlays.py
```

### Zmiana szerokości overlay

W `create_display()`, zmień `overlay_width` z 80 na 85:

```python
if self.state.show_config:
    return _Overlay(layout, self._generate_config_overlay(), overlay_width=85)
elif self.state.show_legend:
    return _Overlay(layout, self._generate_legend_overlay(), overlay_width=85)
elif self.state.show_menu:
    return _Overlay(layout, self._generate_menu_overlay(), overlay_width=85)
```

---

## ✅ Pokrycie funkcjonalności

| Obecna funkcjonalność | Nowa lokalizacja |
|----------------------|------------------|
| Encoder info | SETTINGS → ENCODING |
| Thread/prefetch | SETTINGS → PROCESSING |
| Input folders | SETTINGS → INPUT/OUTPUT |
| Extensions | SETTINGS → INPUT/OUTPUT |
| Dynamic CQ | SETTINGS → QUALITY & FILTERS |
| Camera filter | SETTINGS → QUALITY & FILTERS |
| Metadata mode | SETTINGS → METADATA & DEBUG |
| Autorotate rules | SETTINGS → METADATA & DEBUG |
| Status codes (fail, err, etc.) | REFERENCE → STATUS CODES |
| Result symbols (✓, ✗, etc.) | REFERENCE → STATUS CODES |
| Spinner types | REFERENCE → JOB INDICATORS |
| GPU graph info | REFERENCE → GPU GRAPH |
| Navigation keys | SHORTCUTS → NAVIGATION |
| Panel toggle keys | SHORTCUTS → PANELS |
| Job control keys | SHORTCUTS → JOB CONTROL |

**100% obecnej funkcjonalności zachowane** ✓
