import html
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
import readchar

from .dictionary import DictionaryManager

console = Console()

HEADER_HELP = "← : Prev Dict | → : Next Dict | ESC/Enter: Return | Ctrl + D : Quit"


def _strip_tags(text: str) -> str:
    """Strip all XML/HTML tags from text."""
    return re.sub(r"<[^>]+>", "", text)


def extract_headword(raw: str) -> str:
    """Extract the headword from the leading <k>...</k> tag."""
    m = re.match(r"^<k>([^<]*)</k>", raw)
    return html.unescape(m.group(1)) if m else ""


def render_definition(raw: str) -> Text:
    """Convert XDXF/HTML-tagged definition to rich Text."""
    text = raw

    # Remove outer <k>word</k> at start (headword shown separately)
    text = re.sub(r"^<k>[^<]*</k>\s*", "", text, count=1)

    # Audio reference tags — remove entirely (not displayable in terminal)
    text = re.sub(r"<rref>[^<]*</rref>", "", text)

    # <c c="color">content</c> — strip color attribute, keep content
    text = re.sub(r'<c\s+[^>]*>(.*?)</c>', r"\1", text, flags=re.DOTALL)

    # Paired semantic tags
    TAG_MAP = {
        "k": ("[bold underline]", "[/bold underline]"),
        "b": ("[bold]", "[/bold]"),
        "i": ("[italic]", "[/italic]"),
        "tr": ("[italic cyan]/", "/[/italic cyan]"),
        "ex": ("[dim]", "[/dim]"),
        "kref": ("[cyan]", "[/cyan]"),
        "abr": ("[dim]", "[/dim]"),
    }
    # Tags whose content should pass through unchanged
    PASSTHROUGH = {"dtrn", "c", "co", "pos", "u", "s", "gr"}

    for _ in range(6):  # iterate to handle nested tags
        for tag, (open_m, close_m) in TAG_MAP.items():
            text = re.sub(
                rf"<{tag}>(.*?)</{tag}>",
                lambda m, o=open_m, c=close_m: f"{o}{m.group(1)}{c}",
                text,
                flags=re.DOTALL,
            )
        for tag in PASSTHROUGH:
            text = re.sub(
                rf"<{tag}>(.*?)</{tag}>",
                r"\1",
                text,
                flags=re.DOTALL,
            )

    # Strip any remaining unknown tags
    text = re.sub(r"<[^>]+>", "", text)

    # Decode HTML entities
    text = html.unescape(text)

    # Clean up excessive whitespace / blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    try:
        return Text.from_markup(text)
    except Exception:
        # Fallback: strip markup if rich can't parse it
        return Text(_strip_tags(raw))


def draw_header(mode: str = "search"):
    console.print(
        Panel(
            f"[bold white]StarDict[/bold white]\n[white]Multi-dictionary lookup[/white]\n\n[dim white]{HEADER_HELP}[/dim white]"
        )
    )


def clear():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def wait_for_navigation(results: list[tuple[str, str]]) -> None:
    """Show results one at a time; navigate with arrow keys, exit with ESC/Enter."""
    current = 0
    total = len(results)

    while True:
        clear()
        draw_header()

        bookname, definition = results[current]
        console.print(
            f"\n[bold cyan][{current + 1}/{total}][/bold cyan]  [bold]{bookname}[/bold]"
        )
        console.rule(style="cyan")

        headword = extract_headword(definition)
        if headword:
            console.print(f"[bold white]{headword}[/bold white]")

        rendered = render_definition(definition)
        console.print(rendered)
        console.print()

        key = readchar.readkey()

        if key == readchar.key.RIGHT:
            current = min(current + 1, total - 1)
        elif key == readchar.key.LEFT:
            current = max(current - 1, 0)
        elif key in (readchar.key.ESC, readchar.key.ENTER, "\r", "\n"):
            break


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
