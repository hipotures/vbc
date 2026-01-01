# VBC Tabbed Overlay - Przewodnik implementacji

## 📋 Spis treści

1. [Przegląd zmian](#przegląd-zmian)
2. [Zmiany w UIState](#zmiany-w-uistate)
3. [Nowe eventy](#nowe-eventy)
4. [Zmiany w KeyboardListener](#zmiany-w-keyboardlistener)
5. [Zmiany w UIManager](#zmiany-w-uimanager)
6. [Zmiany w Dashboard](#zmiany-w-dashboard)
7. [Struktura wizualna](#struktura-wizualna)
8. [Logika przełączania](#logika-przełączania)
9. [Migracja](#migracja)
10. [Testowanie](#testowanie)

---

## Przegląd zmian

### Obecny stan
- 3 niezależne flagi: `show_config`, `show_legend`, `show_menu`
- 3 osobne metody generujące overlay: `_generate_config_overlay()`, `_generate_legend_overlay()`, `_generate_menu_overlay()`
- Klawisze C/L/M toggle'ują odpowiednie flagi
- Każdy overlay renderowany osobno

### Docelowy stan
- 1 flaga `show_overlay` + 1 stan `active_tab`
- 1 metoda `_generate_tabbed_overlay()` z wewnętrznym routingiem
- Klawisze C/L/M otwierają overlay i przeskakują do taba
- Klawisz Tab cyklicznie przełącza między tabami
- Jeden spójny overlay z nagłówkiem tabów

### Pliki do modyfikacji

| Plik | Zakres zmian |
|------|--------------|
| `vbc/ui/state.py` | Nowe pola stanu |
| `vbc/domain/events.py` | Nowe eventy |
| `vbc/ui/keyboard.py` | Obsługa Tab, zmiana logiki C/L/M |
| `vbc/ui/manager.py` | Nowe handlery eventów |
| `vbc/ui/dashboard.py` | Nowa metoda overlay, rendering tabów |
| `vbc/ui/modern_overlays.py` | Nowy plik z zawartością tabów |

---

## Zmiany w UIState

### Plik: `vbc/ui/state.py`

### Usunąć
```python
show_config: bool = False
show_legend: bool = False
show_menu: bool = False
```

### Dodać
```python
# Overlay state
show_overlay: bool = False
active_tab: str = "settings"  # "settings" | "reference" | "shortcuts"

# Lista dostępnych tabów (dla cyklicznego przełączania)
OVERLAY_TABS: ClassVar[list[str]] = ["settings", "reference", "shortcuts"]
```

### Nowe metody w UIState
```python
def open_overlay(self, tab: str = None) -> None:
    """Otwiera overlay, opcjonalnie na konkretnym tabie."""
    with self._lock:
        self.show_overlay = True
        if tab and tab in self.OVERLAY_TABS:
            self.active_tab = tab

def close_overlay(self) -> None:
    """Zamyka overlay."""
    with self._lock:
        self.show_overlay = False

def toggle_overlay(self, tab: str = None) -> None:
    """Toggle overlay. Jeśli otwarty na innym tabie, przełącza tab."""
    with self._lock:
        if not self.show_overlay:
            self.show_overlay = True
            if tab:
                self.active_tab = tab
        elif tab and self.active_tab != tab:
            # Overlay otwarty, ale inny tab - przełącz tab
            self.active_tab = tab
        else:
            # Overlay otwarty na tym samym tabie - zamknij
            self.show_overlay = False

def cycle_tab(self, direction: int = 1) -> None:
    """Cyklicznie przełącza tab. direction: 1=następny, -1=poprzedni."""
    with self._lock:
        if not self.show_overlay:
            self.show_overlay = True
            return
        
        current_idx = self.OVERLAY_TABS.index(self.active_tab)
        next_idx = (current_idx + direction) % len(self.OVERLAY_TABS)
        self.active_tab = self.OVERLAY_TABS[next_idx]
```

---

## Nowe eventy

### Plik: `vbc/domain/events.py`

### Usunąć (lub oznaczyć jako deprecated)
```python
class ToggleConfig:
    """Toggle config overlay."""
    pass

class ToggleLegend:
    """Toggle legend overlay."""
    pass

class ToggleMenu:
    """Toggle menu overlay."""
    pass
```

### Dodać
```python
@dataclass
class ToggleOverlayTab:
    """Toggle overlay z opcjonalnym przejściem do konkretnego taba."""
    tab: str | None = None  # "settings" | "reference" | "shortcuts" | None

@dataclass
class CycleOverlayTab:
    """Cyklicznie przełącz tab w overlay."""
    direction: int = 1  # 1=następny, -1=poprzedni

class CloseOverlay:
    """Zamknij overlay."""
    pass
```

---

## Zmiany w KeyboardListener

### Plik: `vbc/ui/keyboard.py`

### Zmienić obsługę klawiszy

**Obecna logika:**
```python
elif key.lower() == 'c':
    self.bus.publish(ToggleConfig())
elif key.lower() == 'l':
    self.bus.publish(ToggleLegend())
elif key.lower() == 'm':
    self.bus.publish(ToggleMenu())
elif key == '\x1b':  # Escape
    self.bus.publish(HideOverlays())
```

**Nowa logika:**
```python
elif key.lower() == 'c':
    self.bus.publish(ToggleOverlayTab(tab="settings"))
elif key.lower() == 'l':
    self.bus.publish(ToggleOverlayTab(tab="reference"))
elif key.lower() == 'm':
    self.bus.publish(ToggleOverlayTab(tab="shortcuts"))
elif key == '\t':  # Tab
    self.bus.publish(CycleOverlayTab(direction=1))
elif key == '\x1b[Z':  # Shift+Tab (opcjonalnie)
    self.bus.publish(CycleOverlayTab(direction=-1))
elif key == '\x1b':  # Escape
    self.bus.publish(CloseOverlay())
```

### Uwagi dotyczące detekcji Tab
- `'\t'` - standardowy Tab (ASCII 9)
- `'\x1b[Z'` - Shift+Tab (escape sequence) - opcjonalne
- Należy przetestować w kontekście terminala (niektóre terminale mogą inaczej raportować Tab)

---

## Zmiany w UIManager

### Plik: `vbc/ui/manager.py`

### Usunąć subskrypcje
```python
self.bus.subscribe(ToggleConfig, self._on_toggle_config)
self.bus.subscribe(ToggleLegend, self._on_toggle_legend)
self.bus.subscribe(ToggleMenu, self._on_toggle_menu)
```

### Usunąć handlery
```python
def _on_toggle_config(self, event):
    self.state.show_config = not self.state.show_config
    self.state.show_legend = False
    self.state.show_menu = False

def _on_toggle_legend(self, event):
    self.state.show_legend = not self.state.show_legend
    self.state.show_config = False
    self.state.show_menu = False

def _on_toggle_menu(self, event):
    self.state.show_menu = not self.state.show_menu
    self.state.show_config = False
    self.state.show_legend = False
```

### Dodać subskrypcje
```python
self.bus.subscribe(ToggleOverlayTab, self._on_toggle_overlay_tab)
self.bus.subscribe(CycleOverlayTab, self._on_cycle_overlay_tab)
self.bus.subscribe(CloseOverlay, self._on_close_overlay)
```

### Dodać handlery
```python
def _on_toggle_overlay_tab(self, event: ToggleOverlayTab):
    """Obsługa toggle overlay z konkretnym tabem."""
    self.state.toggle_overlay(event.tab)

def _on_cycle_overlay_tab(self, event: CycleOverlayTab):
    """Obsługa cyklicznego przełączania tabów."""
    self.state.cycle_tab(event.direction)

def _on_close_overlay(self, event: CloseOverlay):
    """Obsługa zamknięcia overlay."""
    self.state.close_overlay()
```

---

## Zmiany w Dashboard

### Plik: `vbc/ui/dashboard.py`

### Usunąć metody
```python
def _generate_config_overlay(self) -> Panel:
    ...

def _generate_legend_overlay(self) -> Panel:
    ...

def _generate_menu_overlay(self) -> Panel:
    ...
```

### Dodać import
```python
from vbc.ui.modern_overlays import (
    render_settings_content,
    render_reference_content,
    render_shortcuts_content,
)
```

### Dodać nową metodę
```python
def _generate_tabbed_overlay(self) -> Panel:
    """Generuje unified overlay z tabami."""
    
    with self.state._lock:
        active_tab = self.state.active_tab
        config_lines = self.state.config_lines[:]
    
    # === NAGŁÓWEK Z TABAMI ===
    tabs_table = Table(show_header=False, box=None, expand=True, padding=0)
    tabs_table.add_column(ratio=1)
    tabs_table.add_column(ratio=1)
    tabs_table.add_column(ratio=1)
    
    def tab_style(tab_id: str) -> tuple[str, str]:
        """Zwraca (text_style, border_style) dla taba."""
        if tab_id == active_tab:
            return ("bold white", "green")
        return ("dim", "dim")
    
    settings_style, settings_border = tab_style("settings")
    reference_style, reference_border = tab_style("reference")
    shortcuts_style, shortcuts_border = tab_style("shortcuts")
    
    tabs_table.add_row(
        Panel(
            f"[{settings_style}]⚙ Settings[/] [{settings_style}][C][/]",
            border_style=settings_border,
            box=ROUNDED if active_tab == "settings" else SIMPLE,
            padding=(0, 1),
        ),
        Panel(
            f"[{reference_style}]📖 Reference[/] [{reference_style}][L][/]",
            border_style=reference_border,
            box=ROUNDED if active_tab == "reference" else SIMPLE,
            padding=(0, 1),
        ),
        Panel(
            f"[{shortcuts_style}]⌨ Shortcuts[/] [{shortcuts_style}][M][/]",
            border_style=shortcuts_border,
            box=ROUNDED if active_tab == "shortcuts" else SIMPLE,
            padding=(0, 1),
        ),
    )
    
    # === ZAWARTOŚĆ AKTYWNEGO TABA ===
    if active_tab == "settings":
        content = render_settings_content(config_lines, self._spinner_frame)
    elif active_tab == "reference":
        content = render_reference_content(self._spinner_frame)
    else:  # shortcuts
        content = render_shortcuts_content()
    
    # === FOOTER ===
    footer = Text.from_markup(
        "[dim]Press [white on #30363d] Tab [/] next • "
        "[white on #30363d] Esc [/] close[/]",
        justify="center"
    )
    
    # === SKŁADANIE ===
    full_content = Group(
        tabs_table,
        Rule(style="#30363d"),
        "",
        content,
        "",
        Rule(style="#30363d"),
        footer,
    )
    
    return Panel(
        full_content,
        border_style="cyan",
        box=ROUNDED,
        padding=(1, 2),
    )
```

### Zmienić w metodzie `create_display()`

**Obecna logika:**
```python
# Overlays
if self.state.show_config:
    return _Overlay(layout, self._generate_config_overlay(), overlay_width=80)
elif self.state.show_legend:
    return _Overlay(layout, self._generate_legend_overlay(), overlay_width=80)
elif self.state.show_menu:
    return _Overlay(layout, self._generate_menu_overlay(), overlay_width=80)
```

**Nowa logika:**
```python
# Overlay
if self.state.show_overlay:
    return _Overlay(layout, self._generate_tabbed_overlay(), overlay_width=88)
```

---

## Struktura wizualna

### Wygląd nagłówka tabów

```
╭──────────────────────────────────────────────────────────────────────────────╮
│  ╭─────────────────╮  ┌─────────────────┐  ┌─────────────────┐               │
│  │ ⚙ Settings [C]  │  │ 📖 Reference [L]│  │ ⌨ Shortcuts [M] │               │
│  ╰─────────────────╯  └─────────────────┘  └─────────────────┘               │
│  ──────────────────────────────────────────────────────────────────────────  │
│                                                                              │
│                         [ZAWARTOŚĆ AKTYWNEGO TABA]                           │
│                                                                              │
│  ──────────────────────────────────────────────────────────────────────────  │
│                      Press [Tab] next • [Esc] close                          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### Stany tabów

| Stan | Ramka | Tekst | Tło |
|------|-------|-------|-----|
| Aktywny | `ROUNDED` + green | bold white | — |
| Nieaktywny | `SIMPLE` + dim | dim | — |

### Wymiary

| Element | Wartość |
|---------|---------|
| Szerokość overlay | 88 znaków |
| Wysokość nagłówka tabów | 3 linie |
| Wysokość footer | 2 linie |
| Margines wewnętrzny | 1 linia góra/dół, 2 znaki lewo/prawo |

---

## Logika przełączania

### Tabela zachowań

| Akcja | Overlay zamknięty | Overlay otwarty (ten sam tab) | Overlay otwarty (inny tab) |
|-------|-------------------|-------------------------------|---------------------------|
| `C` | Otwórz → Settings | Zamknij | Przełącz → Settings |
| `L` | Otwórz → Reference | Zamknij | Przełącz → Reference |
| `M` | Otwórz → Shortcuts | Zamknij | Przełącz → Shortcuts |
| `Tab` | Otwórz → Settings | Następny tab | Następny tab |
| `Shift+Tab` | Otwórz → Shortcuts | Poprzedni tab | Poprzedni tab |
| `Esc` | Nic | Zamknij | Zamknij |

### Cykl tabów

```
Settings → Reference → Shortcuts → Settings → ...
    ↑___________________________________________|  (Tab)

Settings ← Reference ← Shortcuts ← Settings ← ...
    |___________________________________________↑  (Shift+Tab)
```

### Diagram stanu

```
                    ┌─────────────────┐
                    │  OVERLAY CLOSED │
                    └────────┬────────┘
                             │
            ┌────────────────┼────────────────┐
            │ C              │ Tab            │ L/M
            ▼                ▼                ▼
    ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
    │   SETTINGS    │ │   SETTINGS    │ │ REFERENCE/    │
    │    ACTIVE     │ │    ACTIVE     │ │  SHORTCUTS    │
    └───────┬───────┘ └───────────────┘ └───────┬───────┘
            │                                    │
            │ Tab                                │ Tab
            ▼                                    ▼
    ┌───────────────┐                   ┌───────────────┐
    │   REFERENCE   │ ◄──── Tab ─────── │   SHORTCUTS   │
    │    ACTIVE     │                   │    ACTIVE     │
    └───────────────┘ ─────  Tab ─────► └───────────────┘
            │                                    │
            │ Esc                                │ Esc
            ▼                                    ▼
                    ┌─────────────────┐
                    │  OVERLAY CLOSED │
                    └─────────────────┘
```

---

## Migracja

### Krok 1: Backup
```bash
cp vbc/ui/state.py vbc/ui/state.py.bak
cp vbc/ui/keyboard.py vbc/ui/keyboard.py.bak
cp vbc/ui/manager.py vbc/ui/manager.py.bak
cp vbc/ui/dashboard.py vbc/ui/dashboard.py.bak
cp vbc/domain/events.py vbc/domain/events.py.bak
```

### Krok 2: Dodaj nowy plik
```bash
# Skopiuj modern_overlays.py do vbc/ui/
cp modern_overlays.py vbc/ui/modern_overlays.py
```

### Krok 3: Modyfikuj pliki w kolejności
1. `vbc/domain/events.py` - dodaj nowe eventy
2. `vbc/ui/state.py` - zmień stan i dodaj metody
3. `vbc/ui/keyboard.py` - zmień obsługę klawiszy
4. `vbc/ui/manager.py` - zmień handlery
5. `vbc/ui/dashboard.py` - dodaj nową metodę overlay
6. `vbc/ui/modern_overlays.py` - eksportuj funkcje `render_*_content()`

### Krok 4: Aktualizacja modern_overlays.py

Zmień funkcje eksportujące z `generate_*_overlay()` na `render_*_content()`:

```python
def render_settings_content(config_lines: List[str], spinner_frame: int = 0) -> RenderableType:
    """Zwraca zawartość taba Settings (bez zewnętrznego Panelu)."""
    return SettingsOverlay(config_lines, spinner_frame).render_content()

def render_reference_content(spinner_frame: int = 0) -> RenderableType:
    """Zwraca zawartość taba Reference (bez zewnętrznego Panelu)."""
    return ReferenceOverlay(spinner_frame).render_content()

def render_shortcuts_content() -> RenderableType:
    """Zwraca zawartość taba Shortcuts (bez zewnętrznego Panelu)."""
    return ShortcutsOverlay().render_content()
```

Oraz dodaj metodę `render_content()` w każdej klasie, która zwraca `Group` bez zewnętrznego `Panel`.

### Krok 5: Testy
```bash
uv run pytest tests/unit/test_dashboard.py -v
uv run pytest tests/unit/test_keyboard.py -v
```

### Krok 6: Test manualny
```bash
uv run vbc demo --demo
# Naciśnij C, L, M, Tab, Shift+Tab, Esc
```

---

## Testowanie

### Unit testy do dodania

#### test_state.py
```python
def test_overlay_toggle_same_tab():
    """Toggle na tym samym tabie zamyka overlay."""
    state = UIState()
    state.toggle_overlay("settings")
    assert state.show_overlay == True
    assert state.active_tab == "settings"
    
    state.toggle_overlay("settings")
    assert state.show_overlay == False

def test_overlay_toggle_different_tab():
    """Toggle na innym tabie przełącza tab."""
    state = UIState()
    state.toggle_overlay("settings")
    state.toggle_overlay("reference")
    
    assert state.show_overlay == True
    assert state.active_tab == "reference"

def test_cycle_tab():
    """Tab cyklicznie przełącza taby."""
    state = UIState()
    state.open_overlay("settings")
    
    state.cycle_tab(1)
    assert state.active_tab == "reference"
    
    state.cycle_tab(1)
    assert state.active_tab == "shortcuts"
    
    state.cycle_tab(1)
    assert state.active_tab == "settings"

def test_cycle_tab_reverse():
    """Shift+Tab przełącza w odwrotną stronę."""
    state = UIState()
    state.open_overlay("settings")
    
    state.cycle_tab(-1)
    assert state.active_tab == "shortcuts"
```

#### test_dashboard.py
```python
def test_tabbed_overlay_renders():
    """Tabbed overlay renderuje się poprawnie."""
    state = UIState()
    state.show_overlay = True
    state.active_tab = "settings"
    state.config_lines = ["Test config"]
    
    dashboard = Dashboard(state)
    overlay = dashboard._generate_tabbed_overlay()
    
    assert isinstance(overlay, Panel)

def test_tabbed_overlay_shows_correct_content():
    """Aktywny tab pokazuje właściwą zawartość."""
    state = UIState()
    state.show_overlay = True
    
    dashboard = Dashboard(state)
    
    for tab in ["settings", "reference", "shortcuts"]:
        state.active_tab = tab
        display = dashboard.create_display()
        assert isinstance(display, _Overlay)
```

### Manualne scenariusze testowe

| # | Scenariusz | Oczekiwany wynik |
|---|------------|------------------|
| 1 | Uruchom VBC, naciśnij `C` | Otwarty overlay na tabie Settings |
| 2 | W overlay, naciśnij `Tab` | Przełączenie na Reference |
| 3 | Naciśnij `Tab` | Przełączenie na Shortcuts |
| 4 | Naciśnij `Tab` | Powrót do Settings |
| 5 | Naciśnij `L` | Przełączenie na Reference |
| 6 | Naciśnij `L` | Zamknięcie overlay |
| 7 | Naciśnij `M` | Otwarty overlay na Shortcuts |
| 8 | Naciśnij `Esc` | Zamknięcie overlay |
| 9 | Naciśnij `Tab` (overlay zamknięty) | Otwarty overlay na Settings |

---

## Podsumowanie zmian

| Komponent | Linie do dodania | Linie do usunięcia | Nowe pliki |
|-----------|------------------|-------------------|------------|
| `state.py` | ~35 | ~3 | — |
| `events.py` | ~15 | ~12 | — |
| `keyboard.py` | ~10 | ~8 | — |
| `manager.py` | ~20 | ~25 | — |
| `dashboard.py` | ~60 | ~80 | — |
| `modern_overlays.py` | — | — | 1 (nowy) |

**Szacowany czas implementacji:** 2-3 godziny

**Ryzyko:** Niskie - zmiany są izolowane w warstwie UI, nie wpływają na pipeline ani logikę biznesową.
