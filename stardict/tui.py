import html
import os
import re
import select
import subprocess
import sys
import termios
import tty
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
import readchar

from .dictionary import DictionaryManager

console = Console()

LINE_KEYS = "1234567890abcdefghijklmnoprstuvwxyz"
LINES_PER_PAGE = len(LINE_KEYS)
HEADER_HELP = (
    "← → : Prev/Next Dict  |  ↑ ↓ : Prev/Next Section  |  [ ] : Prev/Next Page\n"
    "Space : Speak Next    |  1-0,a-z : Speak Line     |  ESC/Enter : Return\n"
    "Q : Quit"
)

# Tags whose content is numberable (definition text and examples)
_NUMBERABLE_TAGS = re.compile(r"<(dtrn|ex|blockquote)>(.*?)</\1>", re.DOTALL)
# Ordinal pattern that marks the start of a new section (e.g. "1) ", "2. ", "♦ 1) ")
_SECTION_ORDINAL = re.compile(r"^\s*(?:[♦►●•◆▶✓✗]\s*)?\d+[).]\s")
# Word-relationship label (<b>Syn:</b>, <b>Ant:</b>, etc.) followed by kref/co items
_SYNANT_RE = re.compile(
    r"<b>([A-Za-z][^<]{0,40}?:)</b>\s*((?:<(?:kref|co)>[^<]*</(?:kref|co)>\s*(?:,\s*)?)+)",
    re.DOTALL,
)
# Any remaining bold label ending with : (e.g. <b>Derived words:</b> with plain-text items)
_RELABEL_RE = re.compile(r"<b>([A-Za-z][^<]{0,40}?:)</b>")
# Inline tags converted to rich markup
_INLINE_TAG_MAP = {
    "k":    ("[bold underline]", "[/bold underline]"),
    "b":    ("[bold white]",      "[/bold white]"),
    "i":    ("[italic green]",    "[/italic green]"),
    "tr":   ("[italic cyan]/",   "/[/italic cyan]"),
    "kref": ("[cyan]",           "[/cyan]"),
    "abr":  ("[italic green]",   "[/italic green]"),
}
_PASSTHROUGH_TAGS = {"c", "co", "pos", "u", "s", "gr"}


def _preprocess_synant(content: str) -> str:
    """Colorize word-relationship labels and their items."""
    def repl_kref(m: re.Match) -> str:
        label = m.group(1)
        items = re.findall(r"<(?:kref|co)>([^<]*)</(?:kref|co)>", m.group(2))
        return f"\n[yellow]{label}[/yellow] [cyan]{', '.join(items)}[/cyan]\n"
    content = _SYNANT_RE.sub(repl_kref, content)
    # Make any remaining bold Label: (e.g. Derived words: with plain-text items) yellow
    content = _RELABEL_RE.sub(lambda m: f"[yellow]{m.group(1)}[/yellow]", content)
    return content


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _strip_rich(text: str) -> str:
    text = text.replace(r"\[", "[")                  # unescape \[ → [
    text = re.sub(r"\[/?[a-z][^\]]*\]", "", text)   # remove rich markup tags
    text = text.replace("[", r"\[")                  # re-escape remaining [ (grammar notes)
    return text


def _escape_nonrich_brackets(text: str) -> str:
    """Escape literal [ ] that are not valid rich markup tags."""
    # Temporarily replace known rich markup with placeholders
    RICH_RE = re.compile(r"\[/?[a-z][a-z0-9 _#]*\]")
    saved: list[str] = []

    def save(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"\x02{len(saved) - 1}\x03"

    text = RICH_RE.sub(save, text)
    text = text.replace("[", r"\[")
    for i, tag in enumerate(saved):
        text = text.replace(f"\x02{i}\x03", tag)
    return text


def _colorize_grammar_brackets(text: str) -> str:
    """Wrap escaped \\[...\\] grammar-code patterns in italic green (e.g. [V n], [Also V n P])."""
    return re.sub(r'\\\[([^\]]*)\]', r'[italic green]\[\1][/italic green]', text)


def _apply_inline_tags(text: str) -> str:
    """Convert inline XML tags to rich markup within a segment."""
    text = re.sub(r"<rref>[^<]*</rref>", "", text)
    text = re.sub(r'<c\s+[^>]*>(.*?)</c>', r"\1", text, flags=re.DOTALL)

    for _ in range(6):
        for tag, (o, c) in _INLINE_TAG_MAP.items():
            text = re.sub(
                rf"<{tag}>(.*?)</{tag}>",
                lambda m, o=o, c=c: f"{o}{m.group(1)}{c}",
                text,
                flags=re.DOTALL,
            )
        for tag in _PASSTHROUGH_TAGS:
            text = re.sub(rf"<{tag}>(.*?)</{tag}>", r"\1", text, flags=re.DOTALL)

    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text


def extract_headword(raw: str) -> str:
    m = re.match(r"^<k>([^<]*)</k>", raw)
    return html.unescape(m.group(1)) if m else ""


def render_definition_lines(raw: str) -> list[tuple[bool, str]]:
    """
    Return list of (is_numberable, rich_markup_text) per visual line.
    is_numberable=True only for <dtrn>, <ex>, <blockquote> content.
    Literal [ ] brackets are escaped to prevent rich markup conflicts.
    """
    text = raw
    text = re.sub(r"^<k>[^<]*</k>\s*", "", text, count=1)  # remove headword

    # Split into numbered / non-numbered segments based on block-level tags
    segments: list[tuple[bool, str, str | None]] = []
    last = 0
    for m in _NUMBERABLE_TAGS.finditer(text):
        if m.start() > last:
            segments.append((False, text[last:m.start()], None))
        segments.append((True, m.group(2), m.group(1)))
        last = m.end()
    if last < len(text):
        segments.append((False, text[last:], None))

    result: list[tuple[bool, str]] = []
    for is_num, content, tag in segments:
        content = _preprocess_synant(content)
        if tag == "ex":
            # Detect leading [grammar note] prefix at raw XML level
            m_note = re.match(r"^\[([^\]]+)\]\s*(.*)", content, re.DOTALL)
            if m_note:
                note = _strip_tags(m_note.group(1)).strip()
                sentence = _apply_inline_tags(m_note.group(2))
                sentence = _escape_nonrich_brackets(sentence)
                sentence = _colorize_grammar_brackets(sentence)
                rich = f"[italic green]\\[{note}][/italic green][steel_blue1] {sentence}[/steel_blue1]"
            else:
                rich = _apply_inline_tags(content)
                rich = _escape_nonrich_brackets(rich)
                rich = _colorize_grammar_brackets(rich)
                rich = f"[steel_blue1]{rich}[/steel_blue1]"
        elif tag == "dtrn":
            rich = _apply_inline_tags(content)
            rich = _escape_nonrich_brackets(rich)
            rich = _colorize_grammar_brackets(rich)
            rich = f"[color(248)]{rich}[/color(248)]"
        else:
            rich = _apply_inline_tags(content)
            rich = _escape_nonrich_brackets(rich)
            rich = _colorize_grammar_brackets(rich)

        for line in rich.split("\n"):
            if line.strip():
                result.append((is_num, line))

    return result


def group_into_sections(
    lines: list[tuple[bool, str]],
) -> tuple[list[tuple[bool, str]], list[list[tuple[bool, str]]]]:
    """
    Split rendered lines into (preamble_lines, [section_lines, ...]).
    Section boundaries are detected by ordinal markers at line start (e.g. "1) ", "2. ").
    Lines before the first ordinal are the preamble (pronunciation, etc.).
    If no ordinals found, returns ([], [lines]) — one section, no preamble.
    """
    section_starts = [
        i for i, (_, line) in enumerate(lines)
        if _SECTION_ORDINAL.match(_strip_rich(line))
    ]
    if not section_starts:
        return [], [lines]
    preamble = lines[: section_starts[0]]
    sections = []
    for j, start in enumerate(section_starts):
        end = section_starts[j + 1] if j + 1 < len(section_starts) else len(lines)
        sections.append(lines[start:end])
    return preamble, sections


def draw_header():
    console.print(
        Panel(
            f"[bold cyan]StarDict[/bold cyan]\n[white]Multi-dictionary lookup[/white]\n\n"
            f"[dim white]{HEADER_HELP}[/dim white]"
        )
    )


def clear():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _read_key_timeout(timeout: float = 0.1) -> str | None:
    """Read a key in raw mode with timeout. Returns None on timeout."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return None
        ch = os.read(fd, 1).decode("latin-1")
        if ch == "\x1b":
            r2, _, _ = select.select([sys.stdin], [], [], 0.05)
            if r2:
                rest = os.read(fd, 6).decode("latin-1")
                ch += rest
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _last_uniform_run(markup: str) -> str:
    """
    Split markup into style runs and return the last consecutive block
    that shares the same style as the final text run.
    e.g. "[italic]CONJ[/italic] You use [bold]because[/bold] when stating..."
         → " when stating..."  (last plain run)
    e.g. "[dim]He is called Mitch...[/dim]"
         → "He is called Mitch..."  (sole dim run = whole line)
    """
    TAG_RE = re.compile(r"(\[/?[a-z][a-z0-9 _#]*\])")
    tokens = TAG_RE.split(markup)

    runs: list[tuple[frozenset, str]] = []
    active: list[str] = []

    for token in tokens:
        open_m = re.fullmatch(r"\[([a-z][a-z0-9 _#]*)\]", token)
        close_m = re.fullmatch(r"\[/([a-z][a-z0-9 _#]*)\]", token)
        if open_m:
            active.append(open_m.group(1))
        elif close_m:
            tag = close_m.group(1)
            if tag in active:
                active.remove(tag)
        elif token:
            runs.append((frozenset(active), token))

    if not runs:
        return ""

    # Normalize: treat bold (incl. "bold white") as plain
    def _norm(style: frozenset) -> frozenset:
        return frozenset(s for s in style if not s.startswith("bold"))

    # Style of the last non-whitespace run (normalized)
    last_style = next(
        (_norm(style) for style, text in reversed(runs) if text.strip()),
        frozenset(),
    )

    # Collect trailing consecutive runs that share last_style (normalized)
    result: list[str] = []
    for style, text in reversed(runs):
        if _norm(style) == last_style:
            result.insert(0, text)
        elif text.strip():
            break  # different style with real content → stop

    return "".join(result)


def _clean_for_speech(markup: str) -> str:
    """Extract and clean text for TTS: last uniform-style run, stripped of grammar."""
    def _clean(text: str) -> str:
        text = text.replace(r"\[", "[")
        text = re.sub(r"^\s*[\(\[]?[0-9A-Za-z]{1,2}[\)\]\.]\s*", "", text)
        text = re.sub(r"\b[A-Z]{3,}(?:-[A-Z]{2,})*\b", "", text)
        text = re.sub(r"\[[^\]]{1,40}\]", "", text)
        text = re.sub(r"<[A-Z]\s*>", "", text)
        text = re.sub(r"[♦►●•◆▶✓✗\[\]]", "", text)
        return re.sub(r"\s+", " ", text).strip()

    text = _clean(_last_uniform_run(markup))
    if not text:
        # fallback: strip all rich markup and clean the full line
        text = _clean(_strip_rich(markup))
    return text


def say_line(text: str) -> subprocess.Popen | None:
    clean = _clean_for_speech(text)
    if clean:
        return subprocess.Popen(["say", clean])
    return None




def _print_line(markup: str) -> None:
    try:
        console.print(markup, highlight=False)
    except Exception:
        console.print(_strip_rich(markup), highlight=False)


def wait_for_navigation(results: list[tuple[str, str]]) -> None:
    current_dict = 0
    current_section = 0
    current_page = 0
    total_dicts = len(results)
    status_msg = ""
    say_proc: subprocess.Popen | None = None
    space_idx = 0

    def stop_say() -> None:
        nonlocal say_proc
        if say_proc:
            say_proc.terminate()
            say_proc = None

    while True:
        if say_proc is not None and say_proc.poll() is not None:
            say_proc = None
            status_msg = ""
        clear()
        draw_header()

        bookname, definition = results[current_dict]
        console.print(
            f"\n[bold cyan][{current_dict + 1}/{total_dicts}][/bold cyan]  [bold]{bookname}[/bold]"
        )
        console.rule(style="cyan")

        headword = extract_headword(definition)
        if headword:
            console.print(f"[bold white]{headword}[/bold white]")

        all_lines = render_definition_lines(definition)
        preamble_lines, section_groups = group_into_sections(all_lines)

        for _, line in preamble_lines:
            _print_line(f"   {line}")

        total_sections = max(1, len(section_groups))
        current_section = min(current_section, total_sections - 1)

        # Compute 3-section window (clamp at boundaries)
        win_start = current_section - 1
        win_start = max(0, min(win_start, total_sections - 3))
        win_indices = list(range(win_start, min(win_start + 3, total_sections)))

        numbered_lines: list[str] = []
        total_pages = 1

        for sec_idx in win_indices:
            is_current = (sec_idx == current_section)
            console.rule(style="cyan" if is_current else "dim")

            if is_current:
                section_lines = section_groups[sec_idx]
                total_pages = max(1, (len(section_lines) + LINES_PER_PAGE - 1) // LINES_PER_PAGE)
                current_page = min(current_page, total_pages - 1)
                start = current_page * LINES_PER_PAGE
                page_lines = section_lines[start : start + LINES_PER_PAGE]

                key_idx = 0
                for is_num, line in page_lines:
                    if is_num:
                        label = LINE_KEYS[key_idx] if key_idx < len(LINE_KEYS) else " "
                        key_idx += 1
                        numbered_lines.append(line)
                        _print_line(f"[dim]{label}[/dim]  {line}")
                    else:
                        _print_line(f"   {line}")
            else:
                for _, line in section_groups[sec_idx]:
                    _print_line(f"[dim]   {_strip_rich(line)}[/dim]")

        nav_parts = []
        if total_sections > 1:
            nav_parts.append(
                f"Section {current_section + 1}/{total_sections}  "
                f"({'↑ prev  ' if current_section > 0 else ''}"
                f"{'↓ next' if current_section < total_sections - 1 else ''})"
            )
        if total_pages > 1:
            nav_parts.append(
                f"Page {current_page + 1}/{total_pages}  "
                f"({'[ prev  ' if current_page > 0 else ''}"
                f"{'] next' if current_page < total_pages - 1 else ''})"
            )
        if nav_parts:
            console.print("\n[dim]" + "  |  ".join(nav_parts) + "[/dim]")

        if status_msg:
            console.print(f"\n[green]{status_msg}[/green]")
        console.print()

        status_msg = ""
        key = None
        while key is None:
            key = _read_key_timeout(0.1)
            if key is None and say_proc is not None and say_proc.poll() is not None:
                say_proc = None
                break

        if key is None:
            continue
        if key == readchar.key.RIGHT:
            stop_say(); space_idx = 0
            current_dict = min(current_dict + 1, total_dicts - 1)
            current_section = 0; current_page = 0
        elif key == readchar.key.LEFT:
            stop_say(); space_idx = 0
            current_dict = max(current_dict - 1, 0)
            current_section = 0; current_page = 0
        elif key == readchar.key.DOWN:
            stop_say(); space_idx = 0
            current_section = min(current_section + 1, total_sections - 1)
            current_page = 0
        elif key == readchar.key.UP:
            stop_say(); space_idx = 0
            current_section = max(current_section - 1, 0)
            current_page = 0
        elif key == "]":
            stop_say(); space_idx = 0
            current_page = min(current_page + 1, total_pages - 1)
        elif key == "[":
            stop_say(); space_idx = 0
            current_page = max(current_page - 1, 0)
        elif key == " ":
            stop_say()
            if space_idx < len(numbered_lines):
                say_proc = say_line(numbered_lines[space_idx])
                if say_proc:
                    status_msg = f"Speaking... ({space_idx + 1}/{len(numbered_lines)})"
                space_idx += 1
            else:
                space_idx = 0  # wrap around to beginning
        elif key in ("Q", "q", "\x04"):
            stop_say()
            sys.exit(0)
        elif key in (readchar.key.ENTER, "\r", "\n") or key.startswith("\x1b"):
            stop_say()
            break
        elif key in LINE_KEYS:
            stop_say(); space_idx = 0
            idx = LINE_KEYS.index(key)
            if idx < len(numbered_lines):
                say_proc = say_line(numbered_lines[idx])
                if say_proc:
                    status_msg = f"Speaking {key}..."


def main():
    base_dir = Path(__file__).parent.parent
    dict_dir = base_dir / "Dictionaries"

    manager = DictionaryManager(dict_dir)

    if manager.count == 0:
        console.print("[red]No dictionary files found.[/red]")
        console.print(f"[dim]Path: {dict_dir}[/dim]")
        sys.exit(1)

    while True:
        clear()
        draw_header()
        console.print(
            f"\n[dim]{manager.count} dictionar{'y' if manager.count == 1 else 'ies'} loaded.[/dim]\n"
        )

        try:
            word = input("Search: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Exiting.[/dim]")
            break

        if not word:
            continue

        results = manager.search_all(word)

        if not results:
            clear()
            draw_header()
            console.print(f"\n[yellow]No results found for '{word}'.[/yellow]\n")
            try:
                input("Press Enter to continue...")
            except (EOFError, KeyboardInterrupt):
                break
            continue

        wait_for_navigation(results)
