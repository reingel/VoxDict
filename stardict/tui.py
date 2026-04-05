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
    "1-9,0,a-z : Say Line  |  ESC/Enter : Return  |  Q : Quit"
)


def _strip_tags(text: str) -> str:
    """Strip all XML/HTML tags from text."""
    return re.sub(r"<[^>]+>", "", text)


def _strip_rich(text: str) -> str:
    """Strip rich markup brackets for plain text."""
    return re.sub(r"\[/?[^\]]*\]", "", text)


def extract_headword(raw: str) -> str:
    """Extract the headword from the leading <k>...</k> tag."""
    m = re.match(r"^<k>([^<]*)</k>", raw)
    return html.unescape(m.group(1)) if m else ""


def render_definition_markup(raw: str) -> str:
    """Convert XDXF/HTML-tagged definition to a rich markup string."""
    text = raw

    # Remove outer <k>word</k> at start (headword shown separately)
    text = re.sub(r"^<k>[^<]*</k>\s*", "", text, count=1)

    # Audio reference tags — remove entirely
    text = re.sub(r"<rref>[^<]*</rref>", "", text)

    # <c c="color">content</c> — strip color attribute, keep content
    text = re.sub(r'<c\s+[^>]*>(.*?)</c>', r"\1", text, flags=re.DOTALL)

    TAG_MAP = {
        "k": ("[bold underline]", "[/bold underline]"),
        "b": ("[bold]", "[/bold]"),
        "i": ("[italic]", "[/italic]"),
        "tr": ("[italic cyan]/", "/[/italic cyan]"),
        "ex": ("[dim]", "[/dim]"),
        "kref": ("[cyan]", "[/cyan]"),
        "abr": ("[dim]", "[/dim]"),
    }
    PASSTHROUGH = {"dtrn", "c", "co", "pos", "u", "s", "gr"}

    for _ in range(6):
        for tag, (open_m, close_m) in TAG_MAP.items():
            text = re.sub(
                rf"<{tag}>(.*?)</{tag}>",
                lambda m, o=open_m, c=close_m: f"{o}{m.group(1)}{c}",
                text,
                flags=re.DOTALL,
            )
        for tag in PASSTHROUGH:
            text = re.sub(rf"<{tag}>(.*?)</{tag}>", r"\1", text, flags=re.DOTALL)

    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_definition(raw: str) -> Text:
    markup = render_definition_markup(raw)
    try:
        return Text.from_markup(markup)
    except Exception:
        return Text(_strip_tags(raw))


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


def say_line(text: str) -> subprocess.Popen | None:
    """Speak a line using macOS say command (non-blocking). Returns the process."""
    clean = _strip_rich(text).strip()
    if clean:
        return subprocess.Popen(["say", clean])
    return None


def wait_for_navigation(results: list[tuple[str, str]]) -> None:
    """Show results one dictionary at a time with line numbers and pagination."""
    current_dict = 0
    current_page = 0
    total_dicts = len(results)
    status_msg = ""
    say_proc: subprocess.Popen | None = None

    while True:
        if say_proc is not None and say_proc.poll() is not None:
            status_msg = ""
            say_proc = None
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

        markup = render_definition_markup(definition)
        all_lines = markup.split("\n")
        total_pages = max(1, (len(all_lines) + LINES_PER_PAGE - 1) // LINES_PER_PAGE)
        current_page = min(current_page, total_pages - 1)

        start = current_page * LINES_PER_PAGE
        page_lines = all_lines[start : start + LINES_PER_PAGE]

        for i, line in enumerate(page_lines):
            label = LINE_KEYS[i]
            try:
                console.print(f"[dim]{label}[/dim]  {line}")
            except Exception:
                console.print(f"[dim]{label}[/dim]  {_strip_rich(line)}")

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
                break  # redraw to remove "Speaking..."

        if key is None:
            continue
        if key == readchar.key.RIGHT:
            current_dict = min(current_dict + 1, total_dicts - 1)
            current_page = 0
        elif key == readchar.key.LEFT:
            current_dict = max(current_dict - 1, 0)
            current_page = 0
        elif key == readchar.key.DOWN:
            current_page = min(current_page + 1, total_pages - 1)
        elif key == readchar.key.UP:
            current_page = max(current_page - 1, 0)
        elif key in ("Q", "q", "\x04"):  # Q/q or Ctrl+D → quit
            sys.exit(0)
        elif key in (readchar.key.ENTER, "\r", "\n") or key.startswith("\x1b"):
            break
        elif key in LINE_KEYS:
            idx = LINE_KEYS.index(key)
            if idx < len(page_lines):
                say_proc = say_line(page_lines[idx])
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
