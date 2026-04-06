# VoxDict

A keyboard-driven CLI dictionary for macOS that searches multiple StarDict dictionaries simultaneously and reads definitions aloud.

---

## Features

- Search multiple StarDict dictionaries at once
- Navigate by meaning section (↑/↓)
- Switch between dictionaries (←/→)
- Speak any line by its number key (macOS `say`)
- Sequential line-by-line speech playback (Space)

---

## Requirements

- macOS (TTS uses the built-in `say` command)
- Python 3.10+

---

## Installation

```bash
pip install -e .
```

Or install dependencies only:

```bash
pip install -r requirements.txt
```

---

## Usage

```bash
voxdict
```

Or:

```bash
python -m voxdict
```

---

## Dictionary Setup

Place each dictionary in its own subfolder under `Dictionaries/`.

```
VoxDict/
└── Dictionaries/
    └── DictionaryName/
        ├── DictionaryName.ifo
        ├── DictionaryName.idx
        └── DictionaryName.dict
```

A dictionary is skipped if any of the three required files (`.ifo`, `.idx`, `.dict`) is missing.

### StarDict File Format

| Extension | Contents |
|---|---|
| `.ifo` | Metadata: bookname, wordcount, sametypesequence, etc. |
| `.idx` | Binary index: `word\0 + 4B BE offset + 4B BE size`, repeated |
| `.dict` | Definition data (UTF-8 XDXF/HTML text) |
| `.idx.oftx` | Offset cache (optional) |
| `.cdi` | Program extension metadata (optional) |

---

## Key Bindings

| Key | Action |
|---|---|
| `←` / `→` | Previous / next dictionary |
| `↑` / `↓` | Previous / next meaning section |
| `[` / `]` | Previous / next page within a section (when > 35 lines) |
| `1`–`9`, `0`, `a`–`z` | Speak the numbered line |
| `Space` | Speak next line sequentially |
| `ESC` / `Enter` | Return to search |
| `Q` | Quit (from results screen) |
| `Ctrl+D` | Quit (from search prompt) |

---

## Dependencies

| Package | Purpose |
|---|---|
| [rich](https://github.com/Textualize/rich) | Terminal color rendering |
| [readchar](https://github.com/magmax/python-readchar) | Raw key input |
