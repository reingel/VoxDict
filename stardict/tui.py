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
    "← → : Prev/Next Dict  |  ↑ ↓ : Prev/Next Page  |  "
    "Space : Say Next  |  1-9,0,a-z : Say Line  |  ESC/Enter : Return  |  Q : Quit"
)

# Tags whose content is numberable (definition text and examples)
_NUMBERABLE_TAGS = re.compile(r"<(dtrn|ex|blockquote)>(.*?)</\1>", re.DOTALL)
# Inline tags converted to rich markup
_INLINE_TAG_MAP = {
    "k":    ("[bold underline]", "[/bold underline]"),
    "b":    ("[bold]",           "[/bold]"),
    "i":    ("[italic]",         "[/italic]"),
    "tr":   ("[italic cyan]/",   "/[/italic cyan]"),
    "kref": ("[cyan]",           "[/cyan]"),
    "abr":  ("[dim]",            "[/dim]"),
}
_PASSTHROUGH_TAGS = {"c", "co", "pos", "u", "s", "gr"}


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _strip_rich(text: str) -> str:
    return re.sub(r"\[/?[^\]]*\]", "", text)


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
        rich = _apply_inline_tags(content)
        rich = _escape_nonrich_brackets(rich)

        # Apply block-level dim for <ex>
        if tag == "ex":
            lines_in = rich.split("\n")
            rich = "\n".join(f"[dim]{l}[/dim]" if l.strip() else l for l in lines_in)

        for line in rich.split("\n"):
            if line.strip():
                result.append((is_num, line))

    return result


def draw_header():
    console.print(
        Panel(
            f"[bold white]StarDict[/bold white]\n[white]Multi-dictionary lookup[/white]\n\n"
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

    # Normalize: treat bold-only as plain (headword emphasis = same group as plain)
    def _norm(style: frozenset) -> frozenset:
        return style - {"bold"}

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
    text = _last_uniform_run(markup)

    # Unescape \[ \] back to literal brackets
    text = text.replace(r"\[", "[")

    # Remove leading ordinals: "1) ", "2. ", "A. ", "(i) ", etc.
    text = re.sub(r"^\s*[\(\[]?[0-9A-Za-z]{1,2}[\)\]\.]\s*", "", text)

    # Remove grammar labels in ALL-CAPS (3+ chars, may be hyphenated): VERB, CONJ-SUBORD
    text = re.sub(r"\b[A-Z]{3,}(?:-[A-Z]{2,})*\b", "", text)

    # Remove bracketed grammar/style notes (≤40 chars): [V n], [INFORMAL], [also V n]
    text = re.sub(r"\[[^\]]{1,40}\]", "", text)

    # Remove angle-bracket markers like <E >, <Ex>
    text = re.sub(r"<[A-Z]\s*>", "", text)

    # Remove bullet/marker characters
    text = re.sub(r"[♦►●•◆▶✓✗]", "", text)

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def say_line(text: str) -> subprocess.Popen | None:
    clean = _clean_for_speech(text)
    if clean:
        return subprocess.Popen(["say", clean])
    return None




def _print_line(markup: str) -> None:
    try:
        console.print(markup)
    except Exception:
        console.print(_strip_rich(markup))


def wait_for_navigation(results: list[tuple[str, str]]) -> None:
    current_dict = 0
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
        total_pages = max(1, (len(all_lines) + LINES_PER_PAGE - 1) // LINES_PER_PAGE)
        current_page = min(current_page, total_pages - 1)

        start = current_page * LINES_PER_PAGE
        page_lines = all_lines[start : start + LINES_PER_PAGE]

        numbered_lines: list[str] = []
        key_idx = 0
        for is_num, line in page_lines:
            if is_num:
                label = LINE_KEYS[key_idx] if key_idx < len(LINE_KEYS) else " "
                key_idx += 1
                numbered_lines.append(line)
                _print_line(f"[dim]{label}[/dim]  {line}")
            else:
                _print_line(f"   {line}")

        if total_pages > 1:
            console.print(
                f"\n[dim]Page {current_page + 1}/{total_pages}  "
                f"({'↑ prev  ' if current_page > 0 else ''}"
                f"{'↓ next' if current_page < total_pages - 1 else ''})[/dim]"
            )

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
            current_page = 0
        elif key == readchar.key.LEFT:
            stop_say(); space_idx = 0
            current_dict = max(current_dict - 1, 0)
            current_page = 0
        elif key == readchar.key.DOWN:
            stop_say(); space_idx = 0
            current_page = min(current_page + 1, total_pages - 1)
        elif key == readchar.key.UP:
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
                    status_msg = "Speaking..."


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
